import logging
import shutil
import inspect
from datetime import datetime
from threading import Event, Lock
from typing import Callable
from typing import Any, Optional
from uuid import uuid4

from app.adapters.input.base import BaseInputAdapter
from app.adapters.input.dict_input_adapter import DictInputAdapter
from app.adapters.input.file_input_adapter import FileInputAdapter
from app.core.exceptions import ConfigurationError, InputValidationError, RequestCancelledError
from app.core.settings import ReportEndpointConnectionSettings, ReportEndpointSettings, Settings, get_api_key
from app.providers.llm.base import BaseLLMProvider
from app.providers.llm.lmstudio_compat import build_chat_completions_url
from app.providers.llm.openai_compatible_expert import OpenAICompatibleExpertProvider
from app.providers.llm.openai_report import OpenAIReportProvider
from app.providers.llm.openai_vision import OpenAIVisionProvider
from app.providers.lmstudio_residency import LMStudioResidencyManager, LMStudioResidencySpec
from app.providers.retrieval.base import BaseRetriever
from app.providers.retrieval.factory import build_retriever
from app.schemas.report import ReportArtifact, ReportResult
from app.services.input_generation_service import InputGenerationService
from app.workflow.graph import build_graph
from app.workflow.nodes import WorkflowNodes

logger = logging.getLogger(__name__)


class ReportService:
    def __init__(self, settings: Settings):
        self.settings = settings
        self._active_resources: list[object] = []
        self._active_resources_lock = Lock()

    def generate(
        self,
        session_id: Optional[str] = None,
        input_path: Optional[str] = None,
        accident_data: Optional[dict[str, Any]] = None,
        video_path: Optional[str] = None,
        persist_generated_input: bool = True,
        persist_accident_data: bool = False,
        capability_overrides: Optional[dict[str, Any]] = None,
        progress_callback: Optional[Callable[[dict[str, Any]], None]] = None,
        cancel_event: Event | None = None,
    ) -> ReportArtifact:
        capability_overrides = capability_overrides or {}
        vision_override = capability_overrides.get("vision")
        embedding_override = capability_overrides.get("embedding")
        report_override = capability_overrides.get("report")
        input_generation_service: InputGenerationService | None = None
        expert_provider: BaseLLMProvider | None = None
        report_provider: BaseLLMProvider | None = None
        lmstudio_residency_manager: LMStudioResidencyManager | None = None
        input_generation_artifact = None
        try:
            self._validate_generate_request(
                input_path=input_path,
                accident_data=accident_data,
                video_path=video_path,
            )
            self._raise_if_cancelled(cancel_event)
            if video_path:
                input_generation_service = self._build_input_generation_service(vision_override=vision_override)
                self._register_active_resource(input_generation_service)
                input_generation_artifact = input_generation_service.generate(
                    video_path=video_path,
                    persist_generated_input=persist_generated_input,
                )
                accident_data = input_generation_artifact.generated_input
            elif accident_data and persist_accident_data:
                self._persist_confirmed_accident_data(accident_data)

            self._raise_if_cancelled(cancel_event)
            input_adapter = self._build_input_adapter(input_path=input_path, accident_data=accident_data)
            selected_endpoint, switchable_labels = self._resolve_selected_report_endpoint(
                report_override=report_override,
            )
            lmstudio_residency_manager = self._build_lmstudio_residency_manager()
            expert_provider = self._call_builder(
                self._build_expert_provider,
                lmstudio_residency_manager=lmstudio_residency_manager,
                lmstudio_residency_companions=self._build_expert_residency_companions(),
            )
            report_provider = self._call_builder(
                self._build_report_provider,
                selected_endpoint=selected_endpoint,
                switchable_labels=switchable_labels,
                lmstudio_residency_manager=lmstudio_residency_manager,
                lmstudio_residency_companions=self._build_report_residency_companions(selected_endpoint=selected_endpoint),
            )
            self._register_active_resource(expert_provider)
            self._register_active_resource(report_provider)
            self._register_active_resource(lmstudio_residency_manager)
            nodes = WorkflowNodes(
                settings=self.settings,
                input_adapter=input_adapter,
                expert_provider=expert_provider,
                report_provider=report_provider,
                retriever=self._call_builder(
                    self._build_retriever,
                    selected_endpoint=selected_endpoint,
                    embedding_override=embedding_override,
                    lmstudio_residency_manager=lmstudio_residency_manager,
                ),
                progress_callback=progress_callback,
                cancel_event=cancel_event,
            )
            app = build_graph(nodes)
            trace_id = f"report-{uuid4().hex[:12]}"
            final_state = app.invoke({
                "trace_id": trace_id,
                **({"session_id": session_id} if session_id else {}),
            })

            report = ReportResult.model_validate(final_state["report_output"])
            return ReportArtifact(
                trace_id=final_state["trace_id"],
                guidance=final_state["guidance_json"],
                report=report,
                output_dir=final_state["output_dir"],
                input_generation=input_generation_artifact,
                initial_knowledge_snippets=final_state.get("initial_knowledge_snippets") or [],
                knowledge_snippets=final_state.get("knowledge_snippets") or [],
                retrieval_meta=final_state.get("retrieval_meta") or {},
                agentic_retrieval_rounds=final_state.get("agentic_retrieval_rounds") or [],
            )
        finally:
            self._close_resource(input_generation_service)
            self._close_resource(report_provider)
            self._close_resource(expert_provider)
            self._close_resource(lmstudio_residency_manager)
            self._clear_active_resources()

    def _build_input_adapter(
        self,
        input_path: Optional[str],
        accident_data: Optional[dict[str, Any]],
    ) -> BaseInputAdapter:
        if accident_data:
            return DictInputAdapter(accident_data)

        raw_path = input_path or self.settings.input.file_path
        resolved_path = self.settings.resolve_path(raw_path)
        return FileInputAdapter(str(resolved_path))

    @staticmethod
    def _validate_generate_request(
        *,
        input_path: Optional[str],
        accident_data: Optional[dict[str, Any]],
        video_path: Optional[str],
    ) -> None:
        source_count = sum(
            1
            for value in (input_path, accident_data, video_path)
            if value is not None
        )
        if source_count == 0:
            raise InputValidationError("生成报告时必须提供事故 JSON、输入文件路径或视频路径。")
        if source_count > 1:
            raise InputValidationError("生成报告时 accident_data、input_path 与 video_path 只能提供一种。")
        if accident_data is not None and not accident_data:
            raise InputValidationError("事故信息 JSON 不能为空对象。")

    def _build_expert_provider(
        self,
        *,
        lmstudio_residency_manager: LMStudioResidencyManager | None = None,
        lmstudio_residency_companions: list[LMStudioResidencySpec] | None = None,
    ) -> BaseLLMProvider:
        model_cfg = self.settings.models.expert_local
        provider = model_cfg.provider.strip().lower()

        # 专家模型已统一收敛为 OpenAI 兼容协议（含 LM Studio / vLLM 等本地承载）。
        # 旧的 Ollama 原生协议（provider=ollama/local）已下线，本地模型请改用 OpenAI 兼容服务承载。
        if provider in {"openai", "openai_compatible", "openai-compatible", "lmstudio", "local"}:
            api_key = get_api_key(model_cfg.api_key_env) if model_cfg.api_key_env else None
            return OpenAICompatibleExpertProvider(
                config=model_cfg,
                api_key=api_key,
                lmstudio_host_allowlist=self.settings.app.lmstudio_host_allowlist,
                lmstudio_residency_manager=lmstudio_residency_manager,
                lmstudio_residency_companions=lmstudio_residency_companions,
            )

        raise ConfigurationError(
            f"不支持的专家模型提供器: {self.settings.models.expert_local.provider}（已仅支持 OpenAI 兼容协议）"
        )

    def _default_report_endpoint(self) -> ReportEndpointSettings:
        """报告端点 3→1：始终解析为唯一的默认报告端点（按优先级取第一个）。"""
        endpoints = self.settings.models.report_external.iter_endpoints_by_priority()
        if not endpoints:
            raise ConfigurationError("report_external.endpoints 至少需要配置一个端点。")
        return endpoints[0]

    def _build_report_provider(
        self,
        *,
        selected_endpoint: ReportEndpointSettings,
        switchable_labels: list[str] | None = None,
        lmstudio_residency_manager: LMStudioResidencyManager | None = None,
        lmstudio_residency_companions: list[LMStudioResidencySpec] | None = None,
    ) -> BaseLLMProvider:
        model_cfg = self.settings.models.report_external
        switchable_labels = switchable_labels or []
        credential = (
            selected_endpoint.connection.key
            if selected_endpoint.connection and selected_endpoint.connection.key
            else selected_endpoint.api_key_env or model_cfg.api_key_env
        )
        endpoint_api_keys = {
            selected_endpoint.name: get_api_key(credential)
        }
        provider_config = model_cfg.model_copy(update={"endpoints": [selected_endpoint]})
        return OpenAIReportProvider(
            config=provider_config,
            endpoint_api_keys=endpoint_api_keys,
            lmstudio_host_allowlist=self.settings.app.lmstudio_host_allowlist,
            selected_endpoint_label=None,
            switchable_labels=switchable_labels,
            lmstudio_residency_manager=lmstudio_residency_manager,
            lmstudio_residency_companions=lmstudio_residency_companions,
        )

    def _resolve_selected_report_endpoint(
        self,
        *,
        report_override: Optional[dict[str, Any]],
    ):
        """报告端点 3→1：用户配置了报告能力 → 用其单端点 override；否则用系统默认端点。"""
        selected_endpoint = self._default_report_endpoint()
        if not report_override or not (
            report_override.get("base_url") or report_override.get("model_name")
        ):
            return selected_endpoint, []

        raw_base_url = report_override.get("base_url")
        # 用户只需填到 /v1，这里自动补全为 /v1/chat/completions；留空则沿用系统默认端点。
        resolved_url = build_chat_completions_url(raw_base_url) if raw_base_url else selected_endpoint.url
        override_endpoint = selected_endpoint.model_copy(
            update={
                "name": "user_report",
                "url": resolved_url,
                "model": report_override.get("model_name") or selected_endpoint.model,
                "api_key_env": None,
                "connection": ReportEndpointConnectionSettings(
                    connection_type="inline",
                    key=report_override.get("api_key") or "",
                    url=resolved_url,
                ),
            }
        )
        # 单端点模式：无可切换档位
        return override_endpoint, []

    def _build_vision_provider(self, *, vision_override: Optional[dict[str, Any]] = None) -> OpenAIVisionProvider:
        model_cfg = self.settings.models.accident_vision
        if vision_override and (vision_override.get("base_url") or vision_override.get("model_name")):
            model_cfg, endpoint_api_keys = self._synthesize_override_endpoint(
                model_cfg, vision_override, name="user_vision"
            )
        else:
            endpoint_api_keys = {}
            for endpoint in model_cfg.endpoints:
                env_name = endpoint.api_key_env or model_cfg.api_key_env
                endpoint_api_keys[endpoint.name] = get_api_key(env_name)
        return OpenAIVisionProvider(
            config=model_cfg,
            endpoint_api_keys=endpoint_api_keys,
            lmstudio_host_allowlist=self.settings.app.lmstudio_host_allowlist,
        )

    @staticmethod
    def _synthesize_override_endpoint(model_cfg, override: dict[str, Any], *, name: str):
        """把用户 override(base_url/api_key/model)收敛为单端点配置 + 内联 key。仅 OpenAI 兼容格式。"""
        base = model_cfg.endpoints[0] if model_cfg.endpoints else ReportEndpointSettings(name=name, url="")
        raw_base_url = override.get("base_url")
        # 用户只需填到 /v1，这里自动补全为 /v1/chat/completions；留空则沿用系统默认端点。
        new_url = build_chat_completions_url(raw_base_url) if raw_base_url else base.url
        new_model = override.get("model_name") or base.model or model_cfg.model
        inline_key = override.get("api_key") or ""
        endpoint = base.model_copy(
            update={
                "name": name,
                "url": new_url,
                "model": new_model,
                "api_key_env": None,
                "connection": ReportEndpointConnectionSettings(
                    connection_type="inline", key=inline_key, url=new_url
                ),
            }
        )
        config = model_cfg.model_copy(update={"endpoints": [endpoint], "model": new_model})
        return config, {name: inline_key}

    def _build_input_generation_service(
        self, *, vision_override: Optional[dict[str, Any]] = None
    ) -> InputGenerationService:
        return InputGenerationService(
            settings=self.settings,
            vision_provider=self._build_vision_provider(vision_override=vision_override),
        )

    def _build_retriever(
        self,
        *,
        selected_endpoint=None,
        embedding_override: Optional[dict[str, Any]] = None,
        lmstudio_residency_manager: LMStudioResidencyManager | None = None,
    ) -> BaseRetriever:
        if selected_endpoint is None:
            try:
                selected_endpoint = self._default_report_endpoint()
            except Exception:  # noqa: BLE001
                selected_endpoint = None
        effective_settings = self._apply_embedding_override(self.settings, embedding_override)
        return build_retriever(
            effective_settings,
            lmstudio_residency_manager=lmstudio_residency_manager,
            embedding_residency_companions=(
                self._build_embedding_residency_companions(selected_endpoint=selected_endpoint)
                if selected_endpoint is not None
                else None
            ),
        )

    @staticmethod
    def _apply_embedding_override(settings: Settings, embedding_override: Optional[dict[str, Any]]) -> Settings:
        """把嵌入 override（端点 + top_k/dense 调参）应用到一份 per-request settings 副本。

        留空回退系统默认。注：用户若改 embedding 模型与 dense 索引模型不一致时，
        既有 build_retriever 会捕获并降级到 sparse，系统不崩。
        """
        if not embedding_override:
            return settings
        emb_update: dict[str, Any] = {}
        if embedding_override.get("base_url"):
            emb_update["base_url"] = embedding_override["base_url"]
        if embedding_override.get("model_name"):
            emb_update["model"] = embedding_override["model_name"]
        if embedding_override.get("api_key"):
            emb_update["api_key"] = embedding_override["api_key"]

        params = embedding_override.get("params") or {}
        hybrid_update: dict[str, Any] = {}
        if params.get("top_k") is not None:
            hybrid_update["final_context_top_k"] = max(1, min(20, int(params["top_k"])))
        if params.get("dense_top_k_chunks") is not None:
            hybrid_update["dense_top_k_chunks"] = max(1, min(50, int(params["dense_top_k_chunks"])))
        if params.get("dense_top_k_rules") is not None:
            hybrid_update["dense_top_k_rules"] = max(1, min(50, int(params["dense_top_k_rules"])))

        if not emb_update and not hybrid_update:
            return settings

        models = settings.models
        retrieval = settings.retrieval
        if emb_update:
            models = models.model_copy(
                update={"retrieval_embedding": models.retrieval_embedding.model_copy(update=emb_update)}
            )
        if hybrid_update:
            retrieval = retrieval.model_copy(
                update={"hybrid": retrieval.hybrid.model_copy(update=hybrid_update)}
            )
        return settings.model_copy(update={"models": models, "retrieval": retrieval})

    def _build_lmstudio_residency_manager(self) -> LMStudioResidencyManager:
        return LMStudioResidencyManager(
            host_allowlist=self.settings.app.lmstudio_host_allowlist,
            resident_limit=self.settings.app.lmstudio_resident_limit,
        )

    def _build_expert_residency_companions(self) -> list[LMStudioResidencySpec]:
        embedding_spec = self._build_embedding_residency_spec()
        return [embedding_spec] if embedding_spec is not None else []

    def _build_report_residency_companions(self, *, selected_endpoint) -> list[LMStudioResidencySpec]:
        # 单一远端报告端点，无需常驻伴随模型。
        return []

    def _build_embedding_residency_companions(self, *, selected_endpoint) -> list[LMStudioResidencySpec]:
        expert_spec = self._build_expert_residency_spec()
        return [expert_spec] if expert_spec is not None else []

    def _build_expert_residency_spec(self) -> LMStudioResidencySpec | None:
        model_cfg = self.settings.models.expert_local
        if not str(model_cfg.model or "").strip():
            return None
        api_key = get_api_key(model_cfg.api_key_env) if model_cfg.api_key_env else ""
        return LMStudioResidencySpec(
            model=model_cfg.model,
            base_url_or_endpoint=model_cfg.base_url,
            api_key=api_key,
            provider_name=model_cfg.provider,
            ttl_seconds=model_cfg.lmstudio_ttl_seconds,
        )

    def _build_embedding_residency_spec(self) -> LMStudioResidencySpec | None:
        model_cfg = self.settings.models.retrieval_embedding
        if not str(model_cfg.model or "").strip():
            return None
        api_key = get_api_key(model_cfg.api_key_env) if model_cfg.api_key_env else ""
        return LMStudioResidencySpec(
            model=model_cfg.model,
            base_url_or_endpoint=model_cfg.base_url,
            api_key=api_key,
            provider_name=model_cfg.provider,
            ttl_seconds=model_cfg.lmstudio_ttl_seconds,
        )

    def _build_report_endpoint_residency_spec(self, *, selected_endpoint) -> LMStudioResidencySpec | None:
        model_name = str(selected_endpoint.model or "").strip()
        if not model_name:
            return None
        credential = (
            selected_endpoint.connection.key
            if selected_endpoint.connection and selected_endpoint.connection.key
            else selected_endpoint.api_key_env or self.settings.models.report_external.api_key_env
        )
        api_key = get_api_key(credential) if credential else ""
        return LMStudioResidencySpec(
            model=model_name,
            base_url_or_endpoint=selected_endpoint.url,
            api_key=api_key,
            provider_name=self.settings.models.report_external.provider,
            ttl_seconds=selected_endpoint.lmstudio_ttl_seconds,
        )

    def _persist_confirmed_accident_data(self, accident_data: dict[str, Any]) -> None:
        target_path = self.settings.input_generation_output_file
        backup_path = self._backup_existing_input(target_path)
        if backup_path:
            logger.info("已备份旧事故输入文件: %s", backup_path)
        target_path.parent.mkdir(parents=True, exist_ok=True)
        target_path.write_text(
            self._serialize_json(accident_data),
            encoding="utf-8",
        )

    def _backup_existing_input(self, target_path) -> Optional[str]:  # noqa: ANN001
        if not target_path.exists():
            return None
        content = target_path.read_text(encoding="utf-8").strip()
        if not content:
            return None

        timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        backup_path = self.settings.input_generation_backup_dir_path / f"input_accident-{timestamp}.json"
        shutil.copy2(target_path, backup_path)
        return str(backup_path.resolve())

    @staticmethod
    def _serialize_json(payload: dict[str, Any]) -> str:
        import json

        return json.dumps(payload, ensure_ascii=False, indent=2)

    @staticmethod
    def _close_resource(resource: object | None) -> None:
        close = getattr(resource, "close", None)
        if callable(close):
            close()

    @staticmethod
    def _call_builder(builder, **kwargs):  # noqa: ANN001, ANN206
        parameters = inspect.signature(builder).parameters
        supported_kwargs = {
            key: value
            for key, value in kwargs.items()
            if key in parameters
        }
        return builder(**supported_kwargs)

    def cancel_active_run(self) -> None:
        with self._active_resources_lock:
            resources = list(self._active_resources)

        for resource in reversed(resources):
            self._close_resource(resource)

    def _register_active_resource(self, resource: object | None) -> None:
        if resource is None:
            return
        with self._active_resources_lock:
            self._active_resources.append(resource)

    def _clear_active_resources(self) -> None:
        with self._active_resources_lock:
            self._active_resources.clear()

    @staticmethod
    def _raise_if_cancelled(cancel_event: Event | None) -> None:
        if cancel_event is not None and cancel_event.is_set():
            raise RequestCancelledError("客户端连接已断开，报告生成已取消。")
