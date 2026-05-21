from __future__ import annotations

import subprocess
import shutil
from pathlib import Path
from typing import Any

import httpx

from app.core.settings import ModelEndpointSettings, ReportModelSettings, Settings, get_api_key
from app.providers.llm.lmstudio_compat import (
    build_chat_completions_url,
    build_models_url,
    probe_lmstudio_model,
    resolve_lmstudio_compatibility,
)
from app.providers.retrieval.embedding_client import EmbeddingClient
from app.providers.retrieval.reranker_client import RerankerClient
from app.services.report_export_service import DOCX_IMPORT_ERROR, PDF_IMPORT_ERROR


class ReadinessService:
    def __init__(self, settings: Settings):
        self.settings = settings

    def check(self) -> dict[str, Any]:
        knowledge_base_check = self._check_knowledge_base()
        embedding_check = self._check_retrieval_embedding_endpoint()
        reranker_check = self._check_retrieval_reranker_endpoint()
        checks = {
            "config_loaded": self._check_config_loaded(),
            "upload_dir_writable": self._check_upload_dir_writable(),
            "ffprobe_available": self._check_command_available(self.settings.input_generation.frames.ffprobe_path),
            "yolo_runtime_available": self._check_yolo_runtime(),
            "report_export_available": self._check_report_export_dependencies(),
            "knowledge_base_available": knowledge_base_check,
            "retrieval_embedding_available": embedding_check,
            "retrieval_reranker_available": reranker_check,
            "retrieval_runtime_available": self._check_retrieval_runtime(
                knowledge_base_check,
                embedding_check,
                reranker_check,
            ),
            "expert_model_available": self._check_expert_model_endpoint(self.settings.models.expert_local),
            "report_model_available": self._check_model_endpoints(self.settings.models.report_external),
            "vision_model_available": self._check_model_endpoints(self.settings.models.accident_vision),
        }
        critical_checks = (
            "config_loaded",
            "upload_dir_writable",
            "ffprobe_available",
            "yolo_runtime_available",
            "report_export_available",
            "knowledge_base_available",
            "retrieval_runtime_available",
            "expert_model_available",
            "report_model_available",
            "vision_model_available",
        )
        overall_ready = all(checks[name]["ok"] for name in critical_checks)
        return {
            "status": "ready" if overall_ready else "degraded",
            "ready": overall_ready,
            "checks": checks,
        }

    def _check_config_loaded(self) -> dict[str, Any]:
        return {"ok": True, "message": "配置已加载。"}

    def _check_upload_dir_writable(self) -> dict[str, Any]:
        upload_root = self.settings.resolve_path("backend/data/runtime/uploads")
        upload_root.mkdir(parents=True, exist_ok=True)
        probe_file = upload_root / ".ready-check.tmp"
        try:
            probe_file.write_text("ok", encoding="utf-8")
            probe_file.unlink(missing_ok=True)
            return {"ok": True, "message": "上传目录可写。", "path": str(upload_root)}
        except OSError as exc:
            return {"ok": False, "message": "上传目录不可写。", "detail": str(exc), "path": str(upload_root)}

    def _check_command_available(self, command_name: str) -> dict[str, Any]:
        resolved = self._resolve_command(command_name)
        if not resolved:
            return {"ok": False, "message": f"命令不存在：{command_name}"}
        try:
            result = subprocess.run(
                [resolved, "-version"],
                cwd=str(self.settings.project_root),
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                check=False,
                timeout=5,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            return {"ok": False, "message": f"{command_name} 不可用。", "detail": str(exc), "path": resolved}
        if result.returncode != 0:
            return {
                "ok": False,
                "message": f"{command_name} 检查失败。",
                "detail": (result.stderr or result.stdout or "").strip(),
                "path": resolved,
            }
        return {"ok": True, "message": f"{command_name} 可用。", "path": resolved}

    def _check_yolo_runtime(self) -> dict[str, Any]:
        yolo = self.settings.input_generation.yolo
        checks = {
            "python": self._check_existing_path(self._resolve_runtime_path(yolo.python_executable)),
            "runner": self._check_existing_path(self.settings.resolve_path(yolo.runner_script)),
            "model": self._check_existing_path(self.settings.resolve_path(yolo.model_path)),
        }
        ok = all(item["ok"] for item in checks.values())
        return {
            "ok": ok,
            "message": "YOLO 运行依赖已就绪。" if ok else "YOLO 运行依赖不完整。",
            "checks": checks,
        }

    def _check_report_export_dependencies(self) -> dict[str, Any]:
        details: dict[str, Any] = {
            "docx": DOCX_IMPORT_ERROR is None,
            "pdf": PDF_IMPORT_ERROR is None,
        }
        if DOCX_IMPORT_ERROR is not None:
            details["docx_error"] = str(DOCX_IMPORT_ERROR)
        if PDF_IMPORT_ERROR is not None:
            details["pdf_error"] = str(PDF_IMPORT_ERROR)
        ok = DOCX_IMPORT_ERROR is None and PDF_IMPORT_ERROR is None
        return {
            "ok": ok,
            "message": "导出依赖已安装。" if ok else "导出依赖缺失，Word/PDF 导出不可用。",
            "details": details,
        }

    def _check_knowledge_base(self) -> dict[str, Any]:
        provider = self.settings.retrieval.provider.strip().lower()
        if provider == "mock":
            return {"ok": True, "message": "当前使用 Mock 检索器。", "provider": "mock"}

        local_cfg = self.settings.retrieval.local_jsonl
        manifest_path = self.settings.resolve_path(local_cfg.manifest_path)
        chunks_path = self.settings.resolve_path(local_cfg.chunks_path)
        rules_path = self.settings.resolve_path(local_cfg.rules_path)
        sparse_paths = {
            "manifest_path": str(manifest_path),
            "chunks_path": str(chunks_path),
            "rules_path": str(rules_path),
        }
        missing = [
            str(path)
            for path in (manifest_path, chunks_path, rules_path)
            if not path.exists()
        ]
        if missing:
            return {
                "ok": False,
                "message": "知识库文件缺失。",
                "provider": provider,
                "missing": missing,
                **sparse_paths,
            }
        result = {
            "ok": True,
            "message": "知识库文件可访问。",
            "provider": provider,
            **sparse_paths,
        }
        if provider == "hybrid_local":
            result["dense_index"] = self._check_dense_index_files()
        return result

    def _check_dense_index_files(self) -> dict[str, Any]:
        hybrid_cfg = self.settings.retrieval.hybrid
        manifest_path = self.settings.resolve_path(hybrid_cfg.dense_manifest_path)
        records_path = self.settings.resolve_path(hybrid_cfg.dense_records_path)
        vectors_path = self.settings.resolve_path(hybrid_cfg.dense_vectors_path)
        missing = [
            str(path)
            for path in (manifest_path, records_path, vectors_path)
            if not path.exists()
        ]
        result = {
            "ok": not missing,
            "manifest_path": str(manifest_path),
            "records_path": str(records_path),
            "vectors_path": str(vectors_path),
        }
        if missing:
            result["message"] = "稠密索引文件缺失。"
            result["missing"] = missing
            return result
        result["message"] = "稠密索引文件可访问。"
        return result

    def _check_retrieval_embedding_endpoint(self) -> dict[str, Any]:
        if self.settings.retrieval.provider.strip().lower() != "hybrid_local":
            return {"ok": True, "enabled": False, "message": "当前检索未启用 embedding 端点。"}

        config = self.settings.models.retrieval_embedding
        try:
            api_key = get_api_key(config.api_key_env) if config.api_key_env else ""
            probe = EmbeddingClient(
                base_url=config.base_url,
                model=config.model,
                api_key=api_key,
                timeout_seconds=min(config.timeout_seconds, 15),
                query_instruction=config.query_instruction,
                query_max_length=config.query_max_length,
                document_max_length=config.document_max_length,
                cache_size=1,
                cache_ttl_seconds=60,
            ).probe()
            return {
                "ok": True,
                "enabled": True,
                "message": "embedding 服务可访问。",
                **probe,
            }
        except Exception as exc:  # noqa: BLE001
            return {
                "ok": False,
                "enabled": True,
                "message": "embedding 服务不可访问。",
                "model": config.model,
                "endpoint": config.base_url,
                "detail": str(exc),
            }

    def _check_retrieval_reranker_endpoint(self) -> dict[str, Any]:
        if self.settings.retrieval.provider.strip().lower() != "hybrid_local":
            return {"ok": True, "enabled": False, "message": "当前检索未启用 reranker 端点。"}

        config = self.settings.models.retrieval_reranker
        try:
            api_key = get_api_key(config.api_key_env) if config.api_key_env else ""
            probe = RerankerClient(
                base_url=config.base_url,
                model=config.model,
                api_key=api_key,
                timeout_seconds=min(config.timeout_seconds, 10),
            ).probe()
            return {
                "ok": True,
                "enabled": True,
                "message": "reranker 服务可访问。",
                **probe,
            }
        except Exception as exc:  # noqa: BLE001
            return {
                "ok": False,
                "enabled": True,
                "message": "reranker 服务不可访问。",
                "model": config.model,
                "endpoint": config.base_url,
                "detail": str(exc),
            }

    def _check_retrieval_runtime(
        self,
        knowledge_base_check: dict[str, Any],
        embedding_check: dict[str, Any],
        reranker_check: dict[str, Any],
    ) -> dict[str, Any]:
        provider = self.settings.retrieval.provider.strip().lower()
        if provider != "hybrid_local":
            return {"ok": True, "mode": provider, "message": "当前检索不需要 hybrid 运行状态判断。"}

        if not knowledge_base_check["ok"]:
            return {
                "ok": False,
                "mode": "unavailable",
                "message": "基础知识库不可用，无法执行检索。",
            }

        dense_index_ok = bool((knowledge_base_check.get("dense_index") or {}).get("ok"))
        embedding_ok = embedding_check["ok"]
        reranker_ok = reranker_check["ok"]
        if dense_index_ok and embedding_ok and reranker_ok:
            return {
                "ok": True,
                "mode": "hybrid",
                "message": "hybrid 检索链路已就绪。",
            }
        reasons: list[str] = []
        if not dense_index_ok:
            reasons.append("稠密索引不可用")
        if not embedding_ok:
            reasons.append("embedding 服务不可用")
        if not reranker_ok:
            reasons.append("reranker 服务不可用")
        return {
            "ok": True,
            "mode": "sparse_only_fallback",
            "message": "hybrid 检索未完全就绪，将降级到基础模式。",
            "fallback_reason": "；".join(reasons),
        }

    def _check_expert_model_endpoint(self, model_settings: ModelEndpointSettings) -> dict[str, Any]:
        try:
            api_key = get_api_key(model_settings.api_key_env) if model_settings.api_key_env else ""
        except Exception as exc:  # noqa: BLE001
            return {
                "ok": False,
                "message": "模型 API Key 不可用。",
                "model": model_settings.model,
                "endpoint": "expert_local",
                "detail": str(exc),
            }

        return self._probe_model_endpoint(
            endpoint_name="expert_local",
            endpoint_url=model_settings.base_url,
            provider_name=model_settings.provider,
            model_name=model_settings.model,
            api_key=api_key,
            ttl_seconds=model_settings.lmstudio_ttl_seconds,
        )

    def _check_model_endpoints(self, model_settings: ReportModelSettings) -> dict[str, Any]:
        endpoint_results: list[dict[str, Any]] = []
        for endpoint in model_settings.iter_endpoints_by_priority():
            model_name = endpoint.model or model_settings.model or ""
            try:
                if endpoint.connection and endpoint.connection.key:
                    api_key = endpoint.connection.key
                else:
                    api_key_env = endpoint.api_key_env or model_settings.api_key_env
                    api_key = get_api_key(api_key_env) if api_key_env else ""
            except Exception as exc:  # noqa: BLE001
                endpoint_results.append(
                    {
                        "ok": False,
                        "message": "模型 API Key 不可用。",
                        "model": model_name,
                        "endpoint": endpoint.name,
                        "detail": str(exc),
                    }
                )
                continue

            endpoint_results.append(
                self._probe_model_endpoint(
                    endpoint_name=endpoint.name,
                    endpoint_url=endpoint.url,
                    provider_name=model_settings.provider,
                    model_name=model_name,
                    api_key=api_key,
                    ttl_seconds=endpoint.lmstudio_ttl_seconds,
                )
            )

        if not endpoint_results:
            return {"ok": False, "message": "未配置任何模型端点。"}

        first_ok = next((item for item in endpoint_results if item.get("ok")), None)
        summary = dict(first_ok or endpoint_results[0])
        summary["checked_endpoints"] = endpoint_results
        if first_ok is None:
            summary["message"] = "模型端点不可访问。"
        return summary

    def _probe_model_endpoint(
        self,
        *,
        endpoint_name: str,
        endpoint_url: str,
        provider_name: str,
        model_name: str,
        api_key: str,
        ttl_seconds: int | None,
    ) -> dict[str, Any]:
        try:
            lmstudio = resolve_lmstudio_compatibility(
                base_url_or_endpoint=endpoint_url,
                provider_name=provider_name,
                host_allowlist=self.settings.app.lmstudio_host_allowlist,
                ttl_seconds=ttl_seconds,
            )
        except Exception as exc:  # noqa: BLE001
            return {
                "ok": False,
                "message": "模型端点地址无效。",
                "model": model_name,
                "endpoint": endpoint_name,
                "detail": str(exc),
            }

        headers = self._build_auth_headers(api_key)
        if lmstudio.enabled:
            try:
                with httpx.Client(timeout=5) as client:
                    probe = probe_lmstudio_model(
                        client=client,
                        headers=headers,
                        chat_completions_url=lmstudio.chat_completions_url,
                        model_name=model_name,
                    )
            except Exception as exc:  # noqa: BLE001
                return {
                    "ok": False,
                    "message": "LM Studio 模型列表探测失败。",
                    "model": model_name,
                    "endpoint": endpoint_name,
                    "detail": str(exc),
                    "lmstudio": {
                        "enabled": True,
                        "detected_by": lmstudio.detected_by,
                        "host": lmstudio.host,
                        "ttl_seconds": lmstudio.ttl_seconds,
                    },
                }

            ok = probe.model_exists is not False
            message = "LM Studio 兼容模型端点可访问。"
            if not probe.models_endpoint_accessible:
                message = "LM Studio 模型列表暂不可访问，运行时将继续直接发起 chat 请求。"
                ok = True
            elif probe.model_exists is False:
                message = "LM Studio 模型列表可访问，但未找到配置的模型 id。"

            return {
                "ok": ok,
                "message": message,
                "model": model_name,
                "endpoint": endpoint_name,
                "lmstudio": {
                    "enabled": True,
                    "detected_by": lmstudio.detected_by,
                    "host": lmstudio.host,
                    "ttl_seconds": lmstudio.ttl_seconds,
                    "models_endpoint_accessible": probe.models_endpoint_accessible,
                    "models_url": probe.selected_url,
                    "model_exists": probe.model_exists,
                    "loaded": probe.loaded,
                    "probe_errors": probe.errors,
                },
            }

        health_url = build_models_url(build_chat_completions_url(endpoint_url))
        try:
            response = httpx.get(health_url, headers=headers, timeout=5)
            response.raise_for_status()
            return {
                "ok": True,
                "message": "模型端点可访问。",
                "model": model_name,
                "endpoint": endpoint_name,
                "lmstudio": {
                    "enabled": False,
                    "host": lmstudio.host,
                    "models_url": health_url,
                },
            }
        except Exception as exc:  # noqa: BLE001
            return {
                "ok": False,
                "message": "模型端点不可访问。",
                "model": model_name,
                "endpoint": endpoint_name,
                "detail": str(exc),
                "lmstudio": {
                    "enabled": False,
                    "host": lmstudio.host,
                    "models_url": health_url,
                },
            }

    @staticmethod
    def _build_auth_headers(api_key: str) -> dict[str, str]:
        headers: dict[str, str] = {}
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        return headers

    @staticmethod
    def _check_existing_path(path: Path) -> dict[str, Any]:
        return {
            "ok": path.exists(),
            "path": str(path),
        }

    def _resolve_runtime_path(self, raw_path: str) -> Path:
        path = Path(raw_path)
        if path.is_absolute():
            return path.resolve()
        if "\\" in raw_path or "/" in raw_path or raw_path.startswith("."):
            return (self.settings.project_root / path).resolve()
        resolved = shutil.which(raw_path)
        if resolved:
            return Path(resolved)
        return path

    def _resolve_command(self, raw_command: str) -> str | None:
        candidate = Path(raw_command)
        if candidate.is_absolute():
            return str(candidate.resolve()) if candidate.exists() else None

        if "\\" in raw_command or "/" in raw_command or raw_command.startswith("."):
            project_candidate = (self.settings.project_root / raw_command).resolve()
            if project_candidate.exists():
                return str(project_candidate)

        return raw_command
