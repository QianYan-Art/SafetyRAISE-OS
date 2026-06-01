import json
import logging
import re
import shutil
import time
from datetime import datetime, timezone
from pathlib import Path
from threading import Event
from typing import Any, Callable

from app.adapters.input.base import BaseInputAdapter
from app.core.exceptions import InputValidationError, RequestCancelledError, WorkflowError
from app.core.json_parser import extract_json_from_text
from app.core.model_output import sanitize_markdown_output, split_markdown_sections
from app.core.settings import Settings
from app.providers.llm.base import BaseLLMProvider, LLMGenerateResult, LLMToolCall
from app.providers.retrieval.base import BaseRetriever
from app.services.database_service import DatabaseService
from app.workflow.state import WorkflowState

logger = logging.getLogger(__name__)

GUIDANCE_ACCIDENT_PLACEHOLDER = "{在这里粘贴结构化事故信息JSON}"
GUIDANCE_ACCIDENT_SECTION_PATTERN = re.compile(
    r"(<事故信息>\s*)(.*?)(\s*</事故信息>)",
    re.DOTALL,
)
REPORT_GUIDANCE_PLACEHOLDER = "{在这里粘贴指导意见JSON}"
REPORT_ACCIDENT_PLACEHOLDER = "{在这里粘贴结构化事故信息JSON}"
REPORT_ACCIDENT_ANCHOR_PLACEHOLDER = "{在这里粘贴事故信息关键锚点摘要}"
REPORT_INITIAL_SNIPPETS_PLACEHOLDER = "{在这里粘贴首轮知识库片段JSON}"
REPORT_ADDITIONAL_SNIPPETS_PLACEHOLDER = "{在这里粘贴模型追加检索获得的新知识库片段JSON}"
REPORT_AGENTIC_HISTORY_PLACEHOLDER = "{在这里粘贴模型追加检索历史摘要}"
REPORT_RETRIEVE_TOOL_NAME = "retrieve_knowledge"


class WorkflowNodes:
    def __init__(
        self,
        settings: Settings,
        input_adapter: BaseInputAdapter,
        expert_provider: BaseLLMProvider,
        report_provider: BaseLLMProvider,
        retriever: BaseRetriever,
        progress_callback: Callable[[dict[str, Any]], None] | None = None,
        cancel_event: Event | None = None,
    ):
        self.settings = settings
        self.input_adapter = input_adapter
        self.expert_provider = expert_provider
        self.report_provider = report_provider
        self.retriever = retriever
        self.progress_callback = progress_callback
        self.cancel_event = cancel_event
        self.database_service = DatabaseService(settings)

    def load_input_node(self, state: WorkflowState) -> WorkflowState:
        self._raise_if_cancelled()
        payload = self.input_adapter.load()
        return {"accident_raw": payload}

    def validate_input_node(self, state: WorkflowState) -> WorkflowState:
        self._raise_if_cancelled()
        data = state.get("accident_raw") or {}
        if not isinstance(data, dict) or not data:
            raise InputValidationError("事故输入为空或结构错误。")

        validated = dict(data)
        validated["_meta_source"] = "input_adapter"
        validated["_meta_ingest_time"] = datetime.now(timezone.utc).isoformat()
        return {"accident_validated": validated}

    def generate_guidance_node(self, state: WorkflowState) -> WorkflowState:
        self._raise_if_cancelled()
        self._emit_stage("generate_guidance", "started", "正在生成专家指导意见")
        accident_data = state.get("accident_validated") or {}
        guidance_prompt = self._render_guidance_prompt(accident_data)
        user_prompt = "请严格按照上述要求，只输出 JSON 结果。"

        raw = self._retry_generate(
            provider=self.expert_provider,
            system_prompt=guidance_prompt,
            user_prompt=user_prompt,
            stage_name="guidance",
        )
        parsed, raw = self._parse_guidance_json(raw=raw, guidance_prompt=guidance_prompt)
        self._emit_event(
            "guidance",
            guidance=parsed,
        )
        self._emit_stage("generate_guidance", "completed", "专家指导意见生成完成")
        return {
            "guidance_prompt": guidance_prompt,
            "guidance_raw": raw,
            "guidance_json": parsed,
        }

    def retrieve_knowledge_node(self, state: WorkflowState) -> WorkflowState:
        self._raise_if_cancelled()
        self._emit_stage("retrieve_knowledge", "started", "正在检索首轮知识片段")
        accident_data = state.get("accident_validated") or {}
        snippets = self.retriever.retrieve(
            accident_data=accident_data,
            top_k=self.settings.retrieval.top_k,
        )
        retrieval_meta = dict(getattr(self.retriever, "metadata", {}))
        retrieval_meta["initial_snippets_count"] = len(snippets)
        self._emit_event(
            "knowledge",
            snippets=snippets,
            retrieval_meta=retrieval_meta,
        )
        if retrieval_meta.get("mode") == "sparse_only_fallback":
            self._emit_stage(
                "retrieve_knowledge",
                "degraded",
                "本次知识检索已降级到基础模式",
                fallback_reason=retrieval_meta.get("fallback_reason"),
            )
        self._emit_stage(
            "retrieve_knowledge",
            "completed",
            f"首轮知识片段检索完成，共命中 {len(snippets)} 条",
        )
        return {
            "initial_knowledge_snippets": snippets,
            "knowledge_snippets": snippets,
            "retrieval_meta": retrieval_meta,
        }

    def generate_report_node(self, state: WorkflowState) -> WorkflowState:
        self._raise_if_cancelled()
        self._emit_stage("generate_report", "started", "正在整理报告正文与责任分析")
        accident_data = state.get("accident_validated") or {}
        guidance = state.get("guidance_json") or {}
        initial_snippets = state.get("initial_knowledge_snippets") or state.get("knowledge_snippets") or []
        retrieval_meta = dict(state.get("retrieval_meta") or getattr(self.retriever, "metadata", {}))
        report_template = self._load_report_prompt()

        rendered_prompt, report_raw, all_snippets, agentic_rounds = self._generate_report_with_agentic_rag(
            report_template=report_template,
            accident_data=accident_data,
            guidance=guidance,
            initial_snippets=initial_snippets,
        )

        retrieval_meta.update(
            {
                "knowledge_snippets_count": len(all_snippets),
                "agentic_enabled": self._agentic_rag_enabled(),
                "agentic_round_count": len(agentic_rounds),
                "agentic_rounds": self._summarize_agentic_rounds(agentic_rounds),
            }
        )

        normalized_report, sections = self._normalize_report_output(report_raw)
        result = {
            "report_markdown": normalized_report,
            "sections": sections,
            "citations": [item.get("id", "") for item in all_snippets if item.get("id")],
                "meta": {
                    "expert_model": self.settings.models.expert_local.model,
                    "report_model": getattr(
                        self.report_provider,
                        "last_used_model",
                    self.settings.models.report_external.model,
                ),
                "report_endpoint_name": getattr(
                    self.report_provider,
                    "last_used_endpoint_name",
                    None,
                ),
                "report_endpoint_priority": getattr(
                    self.report_provider,
                    "last_used_endpoint_priority",
                    None,
                ),
                    "report_endpoint_url": getattr(
                        self.report_provider,
                        "last_used_endpoint_url",
                        None,
                    ),
                    "report_finish_reason": getattr(
                        self.report_provider,
                        "last_finish_reason",
                        None,
                    ),
                    "report_reasoning_observed": getattr(
                        self.report_provider,
                        "last_reasoning_observed",
                        False,
                    ),
                    "report_reasoning_content_length": getattr(
                        self.report_provider,
                        "last_reasoning_content_length",
                        0,
                    ),
                    "generated_at": datetime.now(timezone.utc).isoformat(),
                    "agentic_retrieval_rounds": len(agentic_rounds),
                },
            }
        self._emit_stage(
            "generate_report",
            "completed",
            f"报告正文整理完成，共生成 {len(sections)} 个章节",
        )
        return {
            "report_prompt": rendered_prompt,
            "report_raw": normalized_report,
            "report_output": result,
            "knowledge_snippets": all_snippets,
            "retrieval_meta": retrieval_meta,
            "agentic_retrieval_rounds": agentic_rounds,
        }

    def postprocess_node(self, state: WorkflowState) -> WorkflowState:
        self._raise_if_cancelled()
        self._emit_stage("postprocess", "started", "正在写入报告文件与运行日志")
        trace_id = state.get("trace_id") or f"trace-{int(time.time() * 1000)}"
        run_dir = self.settings.output_dir_path / trace_id
        run_dir.mkdir(parents=True, exist_ok=True)

        guidance = state.get("guidance_json") or {}
        report = state.get("report_output") or {}
        report_raw = state.get("report_raw") or ""
        accident = state.get("accident_validated") or {}
        snippets = state.get("knowledge_snippets") or []
        retrieval_meta = state.get("retrieval_meta") or getattr(self.retriever, "metadata", {})
        agentic_rounds = state.get("agentic_retrieval_rounds") or []
        session_id = str(state.get("session_id") or "").strip()

        self._write_json(run_dir / "input_validated.json", accident)
        self._write_json(run_dir / "guidance.json", guidance)
        self._write_json(run_dir / "report.json", report)
        self._write_json(
            run_dir / "run_log.json",
            {
                "trace_id": trace_id,
                "session_id": session_id or None,
                "models": {
                    "expert_local": self.settings.models.expert_local.model,
                    "report_external": getattr(
                        self.report_provider,
                        "last_used_model",
                        self.settings.models.report_external.model,
                    ),
                },
                "report_provider": {
                    "model": getattr(
                        self.report_provider,
                        "last_used_model",
                        self.settings.models.report_external.model,
                    ),
                    "endpoint_name": getattr(
                        self.report_provider,
                        "last_used_endpoint_name",
                        None,
                    ),
                    "endpoint_priority": getattr(
                        self.report_provider,
                        "last_used_endpoint_priority",
                        None,
                    ),
                    "endpoint_url": getattr(
                        self.report_provider,
                        "last_used_endpoint_url",
                        None,
                    ),
                    "finish_reason": getattr(
                        self.report_provider,
                        "last_finish_reason",
                        None,
                    ),
                    "reasoning_observed": getattr(
                        self.report_provider,
                        "last_reasoning_observed",
                        False,
                    ),
                    "reasoning_content_length": getattr(
                        self.report_provider,
                        "last_reasoning_content_length",
                        0,
                    ),
                    "usage": getattr(
                        self.report_provider,
                        "last_usage",
                        None,
                    ),
                },
                "retrieval": retrieval_meta,
                "initial_knowledge_snippets": state.get("initial_knowledge_snippets") or [],
                "knowledge_snippets": snippets,
                "knowledge_snippets_count": len(snippets),
                "agentic_retrieval_rounds": agentic_rounds,
                "generated_at": datetime.now(timezone.utc).isoformat(),
            },
        )
        (run_dir / "report.md").write_text(report_raw, encoding="utf-8")
        self._prune_old_output_dirs(current_run_dir=run_dir)

        self._emit_stage(
            "postprocess",
            "completed",
            "报告文件已写入输出目录",
            output_dir=str(run_dir.resolve()),
        )
        return {"trace_id": trace_id, "output_dir": str(run_dir.resolve())}

    def _prune_old_output_dirs(self, current_run_dir: Path) -> None:
        retain_count = self.settings.app.output_retain_count
        if retain_count <= 0 or not self.settings.output_dir_path.exists():
            return

        protected_paths = {current_run_dir.resolve()}
        try:
            with self.database_service.connection() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        select report_result->>'output_dir' as output_dir
                        from chat_sessions
                        where report_result is not null
                        """
                    )
                    rows = list(cur.fetchall())
        except Exception as exc:
            logger.warning("查询活跃报告输出目录失败，跳过旧输出清理：%s", exc)
            return

        for row in rows:
            raw_path = str((row or {}).get("output_dir") or "").strip()
            if not raw_path:
                continue
            try:
                resolved = Path(raw_path).resolve()
            except OSError:
                continue
            if resolved.exists():
                protected_paths.add(resolved)

        candidates = sorted(
            (item for item in self.settings.output_dir_path.iterdir() if item.is_dir()),
            key=lambda item: item.stat().st_mtime,
            reverse=True,
        )

        retained_unprotected = 0
        for output_dir in candidates:
            resolved = output_dir.resolve()
            if resolved in protected_paths:
                continue
            retained_unprotected += 1
            if retained_unprotected <= retain_count:
                continue
            shutil.rmtree(output_dir, ignore_errors=True)

    def _generate_report_with_agentic_rag(
        self,
        report_template: str,
        accident_data: dict[str, Any],
        guidance: dict[str, Any],
        initial_snippets: list[dict[str, Any]],
    ) -> tuple[str, str, list[dict[str, Any]], list[dict[str, Any]]]:
        self._raise_if_cancelled()
        if not self._agentic_rag_enabled():
            rendered_prompt = self._render_report_prompt(
                template=report_template,
                accident_data=accident_data,
                guidance=guidance,
                initial_snippets=initial_snippets,
                additional_snippets=[],
                agentic_rounds=[],
            )
            report_raw = self._retry_generate(
                provider=self.report_provider,
                system_prompt=rendered_prompt,
                user_prompt=self._build_report_execution_prompt(force_finalize=False),
                stage_name="report",
            )
            return rendered_prompt, self._resolve_final_report(report_raw), list(initial_snippets), []

        if self.report_provider.supports_tool_calling:
            try:
                return self._generate_report_with_native_tool_calling(
                    report_template=report_template,
                    accident_data=accident_data,
                    guidance=guidance,
                    initial_snippets=initial_snippets,
                )
            except RequestCancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                logger.warning("报告模型原生 tool calling 失败，回退到 JSON 检索协议: %s", exc)

        return self._generate_report_with_legacy_agentic_rag(
            report_template=report_template,
            accident_data=accident_data,
            guidance=guidance,
            initial_snippets=initial_snippets,
        )

    def _generate_report_with_native_tool_calling(
        self,
        report_template: str,
        accident_data: dict[str, Any],
        guidance: dict[str, Any],
        initial_snippets: list[dict[str, Any]],
    ) -> tuple[str, str, list[dict[str, Any]], list[dict[str, Any]]]:
        combined_snippets = list(initial_snippets)
        additional_snippets: list[dict[str, Any]] = []
        agentic_rounds: list[dict[str, Any]] = []
        rendered_prompt = self._render_report_prompt(
            template=report_template,
            accident_data=accident_data,
            guidance=guidance,
            initial_snippets=initial_snippets,
            additional_snippets=additional_snippets,
            agentic_rounds=agentic_rounds,
        )
        user_prompt = self._build_report_execution_prompt(force_finalize=False)
        tools = self._build_report_retrieval_tools()
        agentic_cfg = self.settings.retrieval.agentic

        for round_index in range(agentic_cfg.max_rounds + 1):
            self._raise_if_cancelled()
            result = self._retry_generate_with_tools(
                provider=self.report_provider,
                system_prompt=rendered_prompt,
                user_prompt=user_prompt,
                tools=tools,
                stage_name="report_tool_call",
            )
            tool_call = self._extract_report_tool_call(result)
            if tool_call is None:
                if not result.content.strip():
                    raise WorkflowError("报告模型既未返回工具调用，也未返回报告正文。")
                return rendered_prompt, self._resolve_final_report(result.content), combined_snippets, agentic_rounds

            if round_index >= agentic_cfg.max_rounds:
                break

            query = str(tool_call.arguments.get("query", "")).strip()
            if not query:
                break
            query = query[: agentic_cfg.max_query_chars]

            self._raise_if_cancelled()
            self._emit_stage(
                "agentic_retrieval",
                "started",
                f"正在执行第 {len(agentic_rounds) + 1} 轮补充检索",
                round=len(agentic_rounds) + 1,
                query=query,
            )
            requested_top_k = self._coerce_agentic_top_k(tool_call.arguments.get("top_k"))
            new_snippets = self.retriever.search(query=query, top_k=requested_top_k)
            combined_snippets = self._merge_snippets(
                current=combined_snippets,
                incoming=new_snippets,
                max_total=agentic_cfg.max_total_snippets,
            )
            additional_snippets = self._select_additional_snippets(initial_snippets, combined_snippets)
            agentic_rounds.append(
                {
                    "round": len(agentic_rounds) + 1,
                    "query": query,
                    "reason": str(tool_call.arguments.get("reason", "")).strip(),
                    "requested_top_k": requested_top_k,
                    "returned_count": len(new_snippets),
                    "snippets": new_snippets,
                }
            )
            self._emit_event(
                "agentic_round",
                round=agentic_rounds[-1],
            )
            self._emit_stage(
                "agentic_retrieval",
                "completed",
                f"第 {agentic_rounds[-1]['round']} 轮补充检索完成，新增 {len(new_snippets)} 条片段",
                round=agentic_rounds[-1]["round"],
                query=query,
                returned_count=len(new_snippets),
            )
            rendered_prompt = self._render_report_prompt(
                template=report_template,
                accident_data=accident_data,
                guidance=guidance,
                initial_snippets=initial_snippets,
                additional_snippets=additional_snippets,
                agentic_rounds=agentic_rounds,
            )
            user_prompt = self._build_report_execution_prompt(force_finalize=False)

        rendered_prompt = self._render_report_prompt(
            template=report_template,
            accident_data=accident_data,
            guidance=guidance,
            initial_snippets=initial_snippets,
            additional_snippets=additional_snippets,
            agentic_rounds=agentic_rounds,
        )
        final_raw = self._retry_generate(
            provider=self.report_provider,
            system_prompt=rendered_prompt,
            user_prompt=self._build_report_execution_prompt(force_finalize=True),
            stage_name="report",
        )
        return rendered_prompt, self._resolve_final_report(final_raw), combined_snippets, agentic_rounds

    def _generate_report_with_legacy_agentic_rag(
        self,
        report_template: str,
        accident_data: dict[str, Any],
        guidance: dict[str, Any],
        initial_snippets: list[dict[str, Any]],
    ) -> tuple[str, str, list[dict[str, Any]], list[dict[str, Any]]]:
        combined_snippets = list(initial_snippets)
        additional_snippets: list[dict[str, Any]] = []
        agentic_rounds: list[dict[str, Any]] = []
        rendered_prompt = self._render_report_prompt(
            template=report_template,
            accident_data=accident_data,
            guidance=guidance,
            initial_snippets=initial_snippets,
            additional_snippets=additional_snippets,
            agentic_rounds=agentic_rounds,
        )
        user_prompt = self._build_report_execution_prompt(force_finalize=False)
        agentic_cfg = self.settings.retrieval.agentic

        for round_index in range(agentic_cfg.max_rounds + 1):
            self._raise_if_cancelled()
            report_raw = self._retry_generate(
                provider=self.report_provider,
                system_prompt=rendered_prompt,
                user_prompt=user_prompt,
                stage_name="report",
            )
            action = self._parse_agentic_action(report_raw)
            if not action:
                return rendered_prompt, self._resolve_final_report(report_raw), combined_snippets, agentic_rounds

            if action["action"] == "final":
                return rendered_prompt, self._resolve_final_report(report_raw), combined_snippets, agentic_rounds

            if round_index >= agentic_cfg.max_rounds:
                break

            query = str(action.get("query", "")).strip()
            if not query:
                break
            query = query[: agentic_cfg.max_query_chars]

            self._raise_if_cancelled()
            self._emit_stage(
                "agentic_retrieval",
                "started",
                f"正在执行第 {len(agentic_rounds) + 1} 轮补充检索",
                round=len(agentic_rounds) + 1,
                query=query,
            )
            requested_top_k = self._coerce_agentic_top_k(action.get("top_k"))
            new_snippets = self.retriever.search(query=query, top_k=requested_top_k)
            combined_snippets = self._merge_snippets(
                current=combined_snippets,
                incoming=new_snippets,
                max_total=agentic_cfg.max_total_snippets,
            )
            additional_snippets = self._select_additional_snippets(initial_snippets, combined_snippets)
            agentic_rounds.append(
                {
                    "round": len(agentic_rounds) + 1,
                    "query": query,
                    "reason": str(action.get("reason", "")).strip(),
                    "requested_top_k": requested_top_k,
                    "returned_count": len(new_snippets),
                    "snippets": new_snippets,
                }
            )
            self._emit_event(
                "agentic_round",
                round=agentic_rounds[-1],
            )
            self._emit_stage(
                "agentic_retrieval",
                "completed",
                f"第 {agentic_rounds[-1]['round']} 轮补充检索完成，新增 {len(new_snippets)} 条片段",
                round=agentic_rounds[-1]["round"],
                query=query,
                returned_count=len(new_snippets),
            )
            rendered_prompt = self._render_report_prompt(
                template=report_template,
                accident_data=accident_data,
                guidance=guidance,
                initial_snippets=initial_snippets,
                additional_snippets=additional_snippets,
                agentic_rounds=agentic_rounds,
            )
            user_prompt = self._build_report_execution_prompt(force_finalize=False)

        rendered_prompt = self._render_report_prompt(
            template=report_template,
            accident_data=accident_data,
            guidance=guidance,
            initial_snippets=initial_snippets,
            additional_snippets=additional_snippets,
            agentic_rounds=agentic_rounds,
        )
        final_raw = self._retry_generate(
            provider=self.report_provider,
            system_prompt=rendered_prompt,
            user_prompt=self._build_report_execution_prompt(force_finalize=True),
            stage_name="report",
        )
        return rendered_prompt, self._resolve_final_report(final_raw), combined_snippets, agentic_rounds

    def _build_report_execution_prompt(self, force_finalize: bool) -> str:
        if force_finalize:
            return "不要再申请知识库检索，也不要调用 retrieve_knowledge 工具，直接输出最终 Markdown 报告正文；请充分展开核心分析，不要写成摘要。"
        if self._agentic_rag_enabled():
            return (
                "请先判断现有材料是否足够；若不足且系统提供了 retrieve_knowledge 工具，请直接调用该工具补充依据；"
                "只有在当前环境不提供工具时，才按模板约定仅输出检索 JSON；若材料已经足够，直接输出最终 Markdown 报告正文，"
                "并尽可能充分展开事故经过、致因分析与责任分析。"
            )
        return "请直接输出最终 Markdown 报告正文，并尽可能充分展开事故经过、致因分析与责任分析，不要写成摘要。"

    def _build_report_retrieval_tools(self) -> list[dict[str, Any]]:
        max_top_k = self.settings.retrieval.agentic.top_k_per_round
        return [
            {
                "type": "function",
                "function": {
                    "name": REPORT_RETRIEVE_TOOL_NAME,
                    "description": (
                        "当首轮知识片段不足以支撑责任分析、违法依据、道路控制信息、特殊天气路况解释时，"
                        "检索本地交通事故知识库并返回补充片段。"
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "query": {
                                "type": "string",
                                "description": "本轮最关键的检索词，尽量短而准。",
                            },
                            "reason": {
                                "type": "string",
                                "description": "本轮检索要补充的依据点。",
                            },
                            "top_k": {
                                "type": "integer",
                                "minimum": 1,
                                "maximum": max_top_k,
                                "description": f"本轮希望返回的片段数量，范围 1 到 {max_top_k}。",
                            },
                        },
                        "required": ["query"],
                        "additionalProperties": False,
                    },
                },
            }
        ]

    def _retry_generate(
        self,
        provider: BaseLLMProvider,
        system_prompt: str,
        user_prompt: str,
        stage_name: str,
    ) -> str:
        attempts = self.settings.workflow.retry.max_attempts
        backoff = self.settings.workflow.retry.backoff_seconds
        last_exc: Exception | None = None

        for i in range(1, attempts + 1):
            self._raise_if_cancelled()
            try:
                result = provider.generate(system_prompt=system_prompt, user_prompt=user_prompt)
                self._raise_if_cancelled()
                return result
            except Exception as exc:  # noqa: BLE001
                if self._is_cancelled():
                    raise RequestCancelledError("客户端连接已断开，报告生成已取消。") from exc
                logger.warning("%s 失败（第 %s/%s 次）: %s", stage_name, i, attempts, exc)
                last_exc = exc
                if i < attempts:
                    time.sleep(backoff * i)

        raise WorkflowError(f"{stage_name} 阶段失败: {last_exc}") from last_exc

    def _retry_generate_with_tools(
        self,
        provider: BaseLLMProvider,
        system_prompt: str,
        user_prompt: str,
        tools: list[dict[str, Any]],
        stage_name: str,
    ) -> LLMGenerateResult:
        attempts = self.settings.workflow.retry.max_attempts
        backoff = self.settings.workflow.retry.backoff_seconds
        last_exc: Exception | None = None

        for i in range(1, attempts + 1):
            self._raise_if_cancelled()
            try:
                result = provider.generate_with_tools(
                    system_prompt=system_prompt,
                    user_prompt=user_prompt,
                    tools=tools,
                )
                self._raise_if_cancelled()
                return result
            except Exception as exc:  # noqa: BLE001
                if self._is_cancelled():
                    raise RequestCancelledError("客户端连接已断开，报告生成已取消。") from exc
                logger.warning("%s 失败（第 %s/%s 次）: %s", stage_name, i, attempts, exc)
                last_exc = exc
                if i < attempts:
                    time.sleep(backoff * i)

        raise WorkflowError(f"{stage_name} 阶段失败: {last_exc}") from last_exc

    def _load_guidance_prompt(self) -> str:
        path: Path = self.settings.guidance_prompt_file
        if not path.exists():
            raise InputValidationError(f"指导意见提示词文件不存在: {path}")
        return path.read_text(encoding="utf-8")

    def _parse_guidance_json(
        self,
        raw: str,
        guidance_prompt: str,
    ) -> tuple[dict[str, Any], str]:
        try:
            return extract_json_from_text(raw), raw
        except InputValidationError as first_exc:
            logger.warning("指导意见 JSON 解析失败，准备发起一次修复重试: %s", first_exc)

        repaired_raw = self._retry_generate(
            provider=self.expert_provider,
            system_prompt=guidance_prompt,
            user_prompt=(
                "你刚才的输出不是合法 JSON。"
                "请删除所有思考过程、解释、代码块和多余文字，"
                "只重新输出一个可解析的 JSON 对象，键名必须完全保持不变。"
            ),
            stage_name="guidance_repair",
        )
        return extract_json_from_text(repaired_raw), repaired_raw

    def _render_guidance_prompt(self, accident_data: dict[str, Any]) -> str:
        template = self._load_guidance_prompt()
        prompt_accident_data = self._strip_internal_fields(accident_data)
        accident_json = json.dumps(prompt_accident_data, ensure_ascii=False, indent=2)

        if GUIDANCE_ACCIDENT_PLACEHOLDER in template:
            return template.replace(GUIDANCE_ACCIDENT_PLACEHOLDER, accident_json, 1)

        if GUIDANCE_ACCIDENT_SECTION_PATTERN.search(template):
            return GUIDANCE_ACCIDENT_SECTION_PATTERN.sub(
                lambda match: f"{match.group(1)}{accident_json}{match.group(3)}",
                template,
                count=1,
            )

        raise InputValidationError("指导意见提示词模板缺少事故信息粘贴位置。")

    def _load_report_prompt(self) -> str:
        path: Path = self.settings.report_prompt_file
        if not path.exists():
            raise InputValidationError(f"分析报告提示词文件不存在: {path}")
        return path.read_text(encoding="utf-8")

    def _render_report_prompt(
        self,
        template: str,
        accident_data: dict[str, Any],
        guidance: dict[str, Any],
        initial_snippets: list[dict[str, Any]],
        additional_snippets: list[dict[str, Any]],
        agentic_rounds: list[dict[str, Any]],
    ) -> str:
        prompt_accident_data = self._strip_internal_fields(accident_data)
        replacements = {
            REPORT_GUIDANCE_PLACEHOLDER: json.dumps(guidance, ensure_ascii=False, indent=2),
            REPORT_ACCIDENT_PLACEHOLDER: json.dumps(prompt_accident_data, ensure_ascii=False, indent=2),
            REPORT_ACCIDENT_ANCHOR_PLACEHOLDER: self._build_accident_anchor_summary(prompt_accident_data),
            REPORT_INITIAL_SNIPPETS_PLACEHOLDER: json.dumps(initial_snippets, ensure_ascii=False, indent=2),
            REPORT_ADDITIONAL_SNIPPETS_PLACEHOLDER: json.dumps(
                additional_snippets,
                ensure_ascii=False,
                indent=2,
            ),
            REPORT_AGENTIC_HISTORY_PLACEHOLDER: json.dumps(
                self._summarize_agentic_rounds(agentic_rounds),
                ensure_ascii=False,
                indent=2,
            ),
        }

        rendered = template
        for placeholder, value in replacements.items():
            if placeholder not in rendered:
                raise InputValidationError(f"分析报告提示词模板缺少占位内容: {placeholder}")
            rendered = rendered.replace(placeholder, value, 1)
        return rendered

    def _build_accident_anchor_summary(self, accident_data: dict[str, Any]) -> str:
        preferred_fields = [
            ("事故标题", ["事故标题"]),
            ("事故类型", ["事故类型"]),
            ("事故形态", ["事故形态"]),
            ("事故发生时间", ["事故发生时间", "事故时间"]),
            ("地点（包括路名，路号）", ["地点（包括路名，路号）"]),
            ("路口路段类型", ["路口路段类型"]),
            ("主要违法行为", ["主要违法行为"]),
            ("事故认定原因", ["事故认定原因"]),
            ("车辆类型", ["车辆类型"]),
            ("伤害程度", ["伤害程度", "伤亡情况"]),
        ]
        summary: dict[str, Any] = {}
        for summary_key, candidate_keys in preferred_fields:
            for key in candidate_keys:
                value = accident_data.get(key)
                if isinstance(value, str) and value.strip():
                    summary[summary_key] = value.strip()
                    break

        if not summary:
            for key, value in accident_data.items():
                if not isinstance(value, str) or not value.strip():
                    continue
                summary[key] = value.strip()
                if len(summary) >= 8:
                    break

        if not summary:
            summary = {"提示": "事故信息中暂无可提取的关键锚点"}
        return json.dumps(summary, ensure_ascii=False, indent=2)

    def _strip_internal_fields(self, value: Any) -> Any:
        if isinstance(value, dict):
            return {
                key: self._strip_internal_fields(item)
                for key, item in value.items()
                if not str(key).startswith("_")
            }
        if isinstance(value, list):
            return [self._strip_internal_fields(item) for item in value]
        return value

    def _resolve_final_report(self, raw_text: str) -> str:
        action = self._parse_agentic_action(raw_text)
        if action is None:
            content = sanitize_markdown_output(raw_text)
            if not content:
                raise WorkflowError("报告模型返回为空。")
            return content

        if action["action"] != "final":
            raise WorkflowError("报告模型未产出最终报告正文。")

        markdown = sanitize_markdown_output(str(action.get("report_markdown") or action.get("content") or ""))
        if not markdown:
            raise WorkflowError("报告模型 final 响应缺少 report_markdown。")
        return markdown

    def _parse_agentic_action(self, raw_text: str) -> dict[str, Any] | None:
        try:
            payload = extract_json_from_text(raw_text)
        except InputValidationError:
            return None

        action = str(payload.get("action", "")).strip().lower()
        if action in {"retrieve", "final"}:
            return payload
        return None

    def _extract_report_tool_call(self, result: LLMGenerateResult) -> LLMToolCall | None:
        if not result.tool_calls:
            return None

        if len(result.tool_calls) > 1:
            logger.warning("报告模型一次返回了多个工具调用，仅处理第一个。")

        tool_call = result.tool_calls[0]
        if tool_call.name != REPORT_RETRIEVE_TOOL_NAME:
            raise WorkflowError(f"报告模型调用了未知工具: {tool_call.name}")
        return tool_call

    def _normalize_report_output(self, report_markdown: str) -> tuple[str, list[dict[str, str]]]:
        normalized = sanitize_markdown_output(report_markdown)
        if not normalized:
            raise WorkflowError("报告正文清洗后为空。")
        sections = split_markdown_sections(normalized)
        return normalized, sections

    def _merge_snippets(
        self,
        current: list[dict[str, Any]],
        incoming: list[dict[str, Any]],
        max_total: int,
    ) -> list[dict[str, Any]]:
        merged: list[dict[str, Any]] = []
        seen: set[str] = set()
        for item in current + incoming:
            item_id = str(item.get("id", ""))
            if not item_id or item_id in seen:
                continue
            seen.add(item_id)
            merged.append(item)
            if len(merged) >= max_total:
                break
        return merged

    def _select_additional_snippets(
        self,
        initial_snippets: list[dict[str, Any]],
        all_snippets: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        initial_ids = {str(item.get("id", "")) for item in initial_snippets}
        return [item for item in all_snippets if str(item.get("id", "")) not in initial_ids]

    def _summarize_agentic_rounds(self, agentic_rounds: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return [
            {
                "round": item["round"],
                "query": item["query"],
                "reason": item["reason"],
                "requested_top_k": item["requested_top_k"],
                "returned_count": item["returned_count"],
                "snippet_ids": [snippet.get("id", "") for snippet in item["snippets"]],
            }
            for item in agentic_rounds
        ]

    def _coerce_agentic_top_k(self, value: Any) -> int:
        max_top_k = self.settings.retrieval.agentic.top_k_per_round
        try:
            number = int(value)
        except (TypeError, ValueError):
            number = max_top_k
        return min(max(number, 1), max_top_k)

    def _agentic_rag_enabled(self) -> bool:
        return self.settings.retrieval.agentic.enabled and self.retriever.supports_query_search

    @staticmethod
    def _write_json(path: Path, data: Any) -> None:
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

    def _emit_stage(
        self,
        stage: str,
        status: str,
        label: str,
        **payload: Any,
    ) -> None:
        self._emit_event(
            "stage",
            stage=stage,
            status=status,
            label=label,
            **payload,
        )

    def _emit_event(self, event: str, **payload: Any) -> None:
        if self.progress_callback is None:
            return
        self.progress_callback(
            {
                "event": event,
                **payload,
            }
        )

    def _is_cancelled(self) -> bool:
        return self.cancel_event is not None and self.cancel_event.is_set()

    def _raise_if_cancelled(self) -> None:
        if self._is_cancelled():
            raise RequestCancelledError("客户端连接已断开，报告生成已取消。")
