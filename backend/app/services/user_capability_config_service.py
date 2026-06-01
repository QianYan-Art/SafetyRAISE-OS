from __future__ import annotations

from typing import Any, Optional

from psycopg.types.json import Jsonb

from app.core.exceptions import InputValidationError
from app.core.settings import get_api_key
from app.schemas.user_config import (
    CapabilityConfigRecord,
    CapabilityConfigStateResponse,
    EmbeddingTuningParams,
    UpdateCapabilityConfigsRequest,
)
from app.services.auth_service import AuthenticatedUser
from app.services.database_service import DatabaseService

CAPABILITIES = ("vision", "embedding", "report")
CAPABILITY_LABELS = {"vision": "视觉模型", "embedding": "嵌入模型", "report": "报告生成模型"}
EMBEDDING_PARAM_KEYS = ("top_k", "dense_top_k_chunks", "dense_top_k_rules")


def mask_api_key(api_key: str | None) -> Optional[str]:
    """脱敏：仅回传尾部少量字符，绝不外泄明文。"""
    key = str(api_key or "").strip()
    if not key:
        return None
    if len(key) <= 4:
        return "•" * len(key)
    return f"{'•' * 4}{key[-4:]}"


class UserCapabilityConfigService:
    def __init__(self, settings, database_service: DatabaseService):
        self.settings = settings
        self.database_service = database_service

    # ---- 读 ----
    def get_state(self, user: AuthenticatedUser) -> CapabilityConfigStateResponse:
        self._seed_admin_defaults_if_missing(user)
        rows = self._load_rows(user.id)
        records = [self._to_masked_record(cap, rows.get(cap)) for cap in CAPABILITIES]
        return CapabilityConfigStateResponse(
            role=user.role,
            capabilities=records,
            system_defaults={"embedding": self._system_embedding_defaults()},
        )

    # ---- 写 ----
    def update_state(
        self,
        *,
        user: AuthenticatedUser,
        request: UpdateCapabilityConfigsRequest,
    ) -> CapabilityConfigStateResponse:
        existing = self._load_rows(user.id)
        with self.database_service.connection() as conn:
            with conn.cursor() as cur:
                for item in request.items:
                    cap = item.capability
                    base_url = (item.base_url or "").strip() or None
                    model_name = (item.model_name or "").strip() or None
                    incoming_key = (item.api_key or "").strip()
                    prev = existing.get(cap) or {}
                    masked_prev = mask_api_key(prev.get("api_key"))

                    # api_key：留空 / 等于打码占位 → 保留原值；否则覆盖
                    if not incoming_key or incoming_key == masked_prev:
                        api_key = prev.get("api_key")
                    else:
                        api_key = incoming_key

                    params = self._normalize_params(cap, item.params, prev.get("params"))

                    # 三要素全空（且无参数）→ 删除该能力行（回退系统默认 / 置为未配置）
                    if not base_url and not model_name and not api_key and not params:
                        cur.execute(
                            "delete from user_capability_configs where user_id = %s and capability = %s",
                            (user.id, cap),
                        )
                        continue

                    cur.execute(
                        """
                        insert into user_capability_configs
                            (user_id, capability, base_url, api_key, model_name, params)
                        values (%s, %s, %s, %s, %s, %s)
                        on conflict (user_id, capability)
                        do update set
                            base_url = excluded.base_url,
                            api_key = excluded.api_key,
                            model_name = excluded.model_name,
                            params = excluded.params,
                            updated_at = now()
                        """,
                        (user.id, cap, base_url, api_key, model_name, Jsonb(params or {})),
                    )
            conn.commit()
        return self.get_state(user)

    # ---- 运行时 override 解析（按角色回退）----
    def resolve_overrides(self, user: AuthenticatedUser) -> dict[str, Any]:
        self._seed_admin_defaults_if_missing(user)
        rows = self._load_rows(user.id)
        is_admin = bool(getattr(user, "is_admin", False))
        admin_rows = rows if is_admin else self._load_admin_rows()
        overrides: dict[str, Any] = {}

        for cap in ("vision", "report"):
            row = rows.get(cap)
            if row and (row.get("base_url") or row.get("model_name") or row.get("api_key")):
                overrides[cap] = {
                    "base_url": row.get("base_url"),
                    "api_key": row.get("api_key"),
                    "model_name": row.get("model_name"),
                }
            else:
                if not is_admin:
                    raise InputValidationError(f"请先配置{CAPABILITY_LABELS[cap]}（{cap}）。")
                overrides[cap] = None  # 管理员 → 用系统默认

        # 嵌入：管理员留空回退系统默认；普通用户留空沿用管理员配置。
        emb = rows.get("embedding")
        if emb and (emb.get("base_url") or emb.get("model_name") or emb.get("api_key") or emb.get("params")):
            overrides["embedding"] = {
                "base_url": emb.get("base_url"),
                "api_key": emb.get("api_key"),
                "model_name": emb.get("model_name"),
                "params": emb.get("params") or {},
            }
        else:
            admin_embedding = admin_rows.get("embedding") if admin_rows else None
            if (
                not is_admin
                and admin_embedding
                and (
                    admin_embedding.get("base_url")
                    or admin_embedding.get("model_name")
                    or admin_embedding.get("api_key")
                    or admin_embedding.get("params")
                )
            ):
                overrides["embedding"] = {
                    "base_url": admin_embedding.get("base_url"),
                    "api_key": admin_embedding.get("api_key"),
                    "model_name": admin_embedding.get("model_name"),
                    "params": admin_embedding.get("params") or {},
                }
            else:
                overrides["embedding"] = None

        return overrides

    # ---- 管理员初始三能力预置：与项目内置默认保持一致 ----
    def _seed_admin_defaults_if_missing(self, user: AuthenticatedUser) -> None:
        if not bool(getattr(user, "is_admin", False)):
            return
        seeds = {
            "report": self._build_report_seed(),
            "vision": self._build_vision_seed(),
            "embedding": self._build_embedding_seed(),
        }
        seeds = {cap: spec for cap, spec in seeds.items() if spec is not None}
        if not seeds:
            return
        with self.database_service.connection() as conn:
            with conn.cursor() as cur:
                for capability, spec in seeds.items():
                    cur.execute(
                        """
                        insert into user_capability_configs
                            (user_id, capability, base_url, api_key, model_name, params)
                        values (%s, %s, %s, %s, %s, %s)
                        on conflict (user_id, capability) do nothing
                        """,
                        (
                            user.id,
                            capability,
                            spec["base_url"],
                            spec["api_key"],
                            spec["model_name"],
                            Jsonb(spec.get("params") or {}),
                        ),
                    )
            conn.commit()

    def _build_report_seed(self) -> dict[str, Any] | None:
        endpoint = self._find_deepseek_pro_endpoint()
        if endpoint is None:
            return None
        report_external = self.settings.models.report_external
        credential = (
            endpoint.connection.key
            if endpoint.connection and endpoint.connection.key
            else endpoint.api_key_env or report_external.api_key_env
        )
        return {
            "base_url": endpoint.url,
            "api_key": get_api_key(credential) if credential else None,
            "model_name": endpoint.model or report_external.model,
            "params": {},
        }

    def _build_vision_seed(self) -> dict[str, Any] | None:
        model_cfg = self.settings.models.accident_vision
        endpoint = model_cfg.endpoints[0] if model_cfg.endpoints else None
        if endpoint is None:
            return None
        credential = (
            endpoint.connection.key
            if endpoint.connection and endpoint.connection.key
            else endpoint.api_key_env or model_cfg.api_key_env
        )
        return {
            "base_url": endpoint.url,
            "api_key": get_api_key(credential) if credential else None,
            "model_name": endpoint.model or model_cfg.model,
            "params": {},
        }

    def _build_embedding_seed(self) -> dict[str, Any] | None:
        embedding = self.settings.models.retrieval_embedding
        api_key = embedding.api_key
        if not api_key and embedding.api_key_env:
            api_key = get_api_key(embedding.api_key_env)
        return {
            "base_url": embedding.base_url,
            "api_key": api_key,
            "model_name": embedding.model,
            "params": {},
        }

    def _find_deepseek_pro_endpoint(self):
        try:
            endpoints = self.settings.models.report_external.endpoints
        except AttributeError:
            return None
        for endpoint in endpoints:
            if str(endpoint.model or "").strip().lower() == "deepseek/deepseek-v4-pro":
                return endpoint
        return endpoints[0] if endpoints else None

    # ---- 内部 ----
    def _load_rows(self, user_id: str) -> dict[str, dict[str, Any]]:
        with self.database_service.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    select capability, base_url, api_key, model_name, params
                    from user_capability_configs
                    where user_id = %s
                    """,
                    (user_id,),
                )
                return {str(row["capability"]): dict(row) for row in cur.fetchall()}

    def _load_admin_rows(self) -> dict[str, dict[str, Any]]:
        with self.database_service.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    select u.id
                    from users u
                    where u.role = 'admin' and u.is_active = true
                    order by u.created_at asc
                    limit 1
                    """
                )
                row = cur.fetchone()
                if row is None:
                    return {}
        return self._load_rows(str(row["id"]))

    def _to_masked_record(self, capability: str, row: dict[str, Any] | None) -> CapabilityConfigRecord:
        row = row or {}
        params_raw = row.get("params") or {}
        return CapabilityConfigRecord(
            capability=capability,  # type: ignore[arg-type]
            configured=bool(row.get("base_url")),
            base_url=row.get("base_url"),
            model_name=row.get("model_name"),
            api_key_masked=mask_api_key(row.get("api_key")),
            params=EmbeddingTuningParams(
                top_k=params_raw.get("top_k"),
                dense_top_k_chunks=params_raw.get("dense_top_k_chunks"),
                dense_top_k_rules=params_raw.get("dense_top_k_rules"),
            ),
        )

    @staticmethod
    def _normalize_params(
        capability: str,
        incoming: EmbeddingTuningParams | None,
        previous: dict[str, Any] | None,
    ) -> dict[str, int]:
        if capability != "embedding":
            return {}
        source: dict[str, Any] = {}
        if previous:
            source.update({k: previous.get(k) for k in EMBEDDING_PARAM_KEYS if previous.get(k) is not None})
        if incoming is not None:
            for key in EMBEDDING_PARAM_KEYS:
                value = getattr(incoming, key, None)
                if value is not None:
                    source[key] = int(value)
        return {k: int(v) for k, v in source.items() if v is not None}

    def _system_embedding_defaults(self) -> EmbeddingTuningParams:
        retrieval = getattr(self.settings, "retrieval", None)
        hybrid = getattr(retrieval, "hybrid", None)
        return EmbeddingTuningParams(
            top_k=getattr(hybrid, "final_context_top_k", None),
            dense_top_k_chunks=getattr(hybrid, "dense_top_k_chunks", None),
            dense_top_k_rules=getattr(hybrid, "dense_top_k_rules", None),
        )
