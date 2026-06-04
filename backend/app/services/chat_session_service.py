import hashlib
import json
import logging
import mimetypes
import shutil
from json import JSONDecodeError
from pathlib import Path
from threading import Lock, RLock
from typing import Any
from uuid import uuid4

from pydantic import ValidationError
from psycopg.types.json import Jsonb

from app.core.exceptions import SessionNotFoundError
from app.core.settings import Settings
from app.providers.retrieval.factory import build_retriever
from app.providers.retrieval.mock_retriever import MockRetriever
from app.services.auth_service import AuthenticatedUser
from app.services.database_service import DatabaseService
from app.schemas.chat_session import (
    ChatMessageRecord,
    ChatSessionLinkedArtifact,
    ChatSessionLinkedFile,
    ChatSessionRecord,
    CreateChatSessionRequest,
    LinkedArtifactAsset,
    LinkedArtifactDetailResponse,
    UpdateChatSessionRequest,
)

WELCOME_MESSAGE = "欢迎进入“交通事故分析报告生成系统”。请先上传一组事故图片或相关事故视频；本系统将优先生成事故信息草稿，经您确认后再生成最终分析报告。"
LEGACY_WELCOME_MESSAGES = {
    "欢迎进入“交通事故分析报告生成模式”。请先上传一张事故图片或一个事故视频；系统会先生成事故信息草稿，再由你确认后生成最终分析报告。",
    "欢迎进入“交通事故分析报告生成模式”。请先上传照片或视频。",
}
LEGACY_MESSAGE_PREFIXES_TO_DROP = (
    "### 专家指导意见速览",
    "### 本轮新增关键产物",
)
REPORT_PROGRESS_BADGE = "报告生成"
REPORT_PROGRESS_DONE_TEXT = "报告文件已写入输出目录。"
KNOWLEDGE_MESSAGE_PREFIX = "### 首轮知识库片段（节选）"
AGENTIC_MESSAGE_PREFIX = "### Agentic RAG 新增片段（节选）"
logger = logging.getLogger(__name__)
_SESSION_LOCKS: dict[str, RLock] = {}
_SESSION_LOCKS_GUARD = Lock()
_LEGACY_SESSION_MIGRATED_DATA_ROOTS: set[str] = set()
_LEGACY_SESSION_MIGRATED_GUARD = Lock()
_OUTPUT_DIR_METADATA_CACHE: dict[str, dict[str, Any]] = {}
_OUTPUT_DIR_METADATA_GUARD = Lock()
_YOLO_DIR_MANIFEST_CACHE: dict[str, dict[str, Any]] = {}
_YOLO_DIR_MANIFEST_GUARD = Lock()


def _get_session_lock(session_id: str) -> RLock:
    with _SESSION_LOCKS_GUARD:
        lock = _SESSION_LOCKS.get(session_id)
        if lock is None:
            lock = RLock()
            _SESSION_LOCKS[session_id] = lock
        return lock


class ChatSessionService:
    def __init__(self, settings: Settings, current_user: AuthenticatedUser | None = None):
        self.settings = settings
        self.current_user = current_user
        self.database_service = DatabaseService(settings)
        self._history_retriever = None
        self._history_retriever_ready = False

    def list_sessions(self, *, recover_unlinked_outputs: bool = True) -> list[ChatSessionRecord]:
        self._migrate_legacy_file_sessions()
        sessions: list[ChatSessionRecord] = []
        with self.database_service.connection() as conn:
            with conn.cursor() as cur:
                if self.current_user is None:
                    cur.execute(
                        """
                        select id, title, owner_user_id::text as owner_user_id, owner_username,
                               created_at, updated_at, sort_order, source_type, source_name,
                               messages, draft_json, draft_meta, report_result, session_state
                        from chat_sessions
                        """
                    )
                else:
                    cur.execute(
                        """
                        select id, title, owner_user_id::text as owner_user_id, owner_username,
                               created_at, updated_at, sort_order, source_type, source_name,
                               messages, draft_json, draft_meta, report_result, session_state
                        from chat_sessions
                        where owner_user_id = %s or owner_username = %s
                        """,
                        (self.current_user.id, self.current_user.username),
                    )
                rows = list(cur.fetchall())
        for row in rows:
            try:
                with _get_session_lock(str(row["id"])):
                    sessions.append(self._load_session_row(row, include_linked_files=False))
            except (OSError, UnicodeDecodeError, ValidationError, TypeError, ValueError) as exc:
                logger.warning("跳过损坏的会话记录：%s；错误：%s", row.get("id"), exc)
                continue
        sessions = [session for session in sessions if self._can_access_session(session)]
        if recover_unlinked_outputs:
            sessions = self._attach_latest_unlinked_report_to_recent_session(sessions)
        sessions.sort(key=self._session_sort_key)
        return sessions

    def get_session(
        self,
        session_id: str,
        *,
        include_linked_files: bool = True,
        include_linked_artifacts: bool = True,
    ) -> ChatSessionRecord:
        with _get_session_lock(session_id):
            record = self._load_session_by_id(
                session_id,
                include_linked_files=include_linked_files,
                include_linked_artifacts=include_linked_artifacts,
            )
        if not self._can_access_session(record):
            raise SessionNotFoundError(f"会话不存在: {session_id}")
        return record

    def create_session(self, request: CreateChatSessionRequest) -> ChatSessionRecord:
        timestamp = request.created_at or request.updated_at or self._now_ms()
        session_id = request.id or f"session-{uuid4().hex[:12]}"
        with _get_session_lock(session_id):
            record = ChatSessionRecord(
                id=session_id,
                title=request.title,
                owner_user_id=request.owner_user_id or getattr(self.current_user, "id", None),
                owner_username=request.owner_username or getattr(self.current_user, "username", None),
                created_at=timestamp,
                updated_at=request.updated_at or timestamp,
                sort_order=request.sort_order,
                source_type=request.source_type,
                source_name=request.source_name,
                messages=request.messages,
                draft_json=request.draft_json,
                draft_meta=request.draft_meta,
                report_result=request.report_result,
                linked_files=[],
                session_state="draft",
            )
            record = self._sync_draft_artifacts(record)
            record = self._refresh_linked_views(record)
            record = self._apply_session_state(record)
            self._write_session(record)
            return record

    def update_session(self, session_id: str, request: UpdateChatSessionRequest) -> ChatSessionRecord:
        with _get_session_lock(session_id):
            current = self.get_session(session_id)
            updates = request.model_dump(exclude_unset=True)
            merged_payload = current.model_dump()
            merged_payload.update(updates)
            merged_payload["updated_at"] = updates.get("updated_at", self._now_ms())
            merged = ChatSessionRecord.model_validate(merged_payload)
            merged = self._sync_draft_artifacts(merged)
            merged = self._refresh_linked_views(merged)
            merged = self._apply_session_state(merged)
            self._write_session(merged)
            return merged

    def delete_session(self, session_id: str) -> None:
        with _get_session_lock(session_id):
            record = self.get_session(session_id)
            seen: set[Path] = set()
            for linked_file in record.linked_files:
                raw_path = Path(linked_file.path).resolve()
                if raw_path in seen:
                    continue
                seen.add(raw_path)
                if self._is_shared_input_path(raw_path):
                    continue
                if not self._is_safe_data_path(raw_path):
                    continue
                if raw_path.is_dir():
                    shutil.rmtree(raw_path, ignore_errors=True)
                elif raw_path.exists():
                    raw_path.unlink(missing_ok=True)

            session_dir = self._session_dir(session_id)
            if session_dir.exists():
                shutil.rmtree(session_dir, ignore_errors=True)
            with self.database_service.connection() as conn:
                with conn.cursor() as cur:
                    cur.execute("delete from chat_sessions where id = %s", (session_id,))
                conn.commit()

    def list_linked_artifacts(self, session_id: str) -> list[ChatSessionLinkedArtifact]:
        return list(self.get_session(session_id, include_linked_files=False).linked_artifacts)

    def get_linked_artifact_detail(
        self,
        session_id: str,
        category: str,
    ) -> LinkedArtifactDetailResponse:
        record = self.get_session(
            session_id,
            include_linked_files=False,
            include_linked_artifacts=False,
        )
        detail = self._build_linked_artifact_detail(record, category)
        if detail is None:
            raise SessionNotFoundError(f"会话产物不存在: {session_id}/{category}")
        return detail

    def resolve_linked_artifact_asset(
        self,
        session_id: str,
        category: str,
        asset_id: str,
    ) -> LinkedArtifactAsset:
        if category == "images_and_keyframes":
            # 画廊解析走轻量枚举：asset_id 为路径派生稳定哈希，枚举阶段不做
            # per-asset exists()，只对命中的那一个资源做存在性与安全校验，
            # 避免一次画廊渲染里 N 次 asset 请求各自触发 O(N) 次远端 stat。
            record = self.get_session(
                session_id,
                include_linked_files=False,
                include_linked_artifacts=False,
            )
            ordered_assets, _ = self._enumerate_images_and_keyframes_assets(record)
            return self._match_linked_artifact_asset(
                ordered_assets, session_id, category, asset_id
            )

        detail = self.get_linked_artifact_detail(session_id, category)
        return self._match_linked_artifact_asset(
            detail.assets, session_id, category, asset_id
        )

    def _match_linked_artifact_asset(
        self,
        assets: list[LinkedArtifactAsset],
        session_id: str,
        category: str,
        asset_id: str,
    ) -> LinkedArtifactAsset:
        for asset in assets:
            if asset.asset_id != asset_id:
                continue
            asset_path = Path(asset.path).resolve()
            if not asset_path.exists():
                raise SessionNotFoundError(f"会话产物资源不存在: {session_id}/{category}/{asset_id}")
            if not self._is_safe_data_path(asset_path):
                raise SessionNotFoundError(f"会话产物资源不在允许目录内: {session_id}/{category}/{asset_id}")
            return asset
        raise SessionNotFoundError(f"会话产物资源不存在: {session_id}/{category}/{asset_id}")

    def _refresh_linked_views(
        self,
        record: ChatSessionRecord,
        *,
        include_linked_files: bool = True,
        include_linked_artifacts: bool = True,
    ) -> ChatSessionRecord:
        linked_files = self._collect_linked_files(record) if include_linked_files else []
        linked_artifacts = self._collect_linked_artifacts(record) if include_linked_artifacts else []
        return record.model_copy(
            update={
                "linked_files": linked_files,
                "linked_artifacts": linked_artifacts,
            }
        )

    def _can_access_session(self, record: ChatSessionRecord) -> bool:
        if self.current_user is None:
            return True
        owner_user_id = str(record.owner_user_id or "").strip()
        owner_username = str(record.owner_username or "").strip()
        return owner_user_id == self.current_user.id or owner_username == self.current_user.username

    def _collect_linked_files(self, record: ChatSessionRecord) -> list[ChatSessionLinkedFile]:
        collected: list[ChatSessionLinkedFile] = []
        seen: set[str] = set()

        def add_path(path_value: str | None, label: str, category: str, path_type: str = "file") -> None:
            if not path_value:
                return
            path = Path(path_value).resolve()
            if self._is_shared_input_path(path):
                return
            if not self._is_safe_data_path(path):
                return
            key = str(path)
            if key in seen:
                return
            seen.add(key)
            collected.append(
                ChatSessionLinkedFile(
                    label=label,
                    path=key,
                    category=category,
                    path_type=path_type,
                    exists=path.exists(),
                )
            )

        draft_meta = record.draft_meta or {}
        add_path(draft_meta.get("workspace_dir"), "输入工作区", "input_workspace", "dir")
        add_path(draft_meta.get("input_path"), "事故信息草稿", "generated_input")
        add_path(draft_meta.get("yolo_summary_path"), "YOLO 摘要", "yolo_summary")
        add_path(draft_meta.get("frames_dir"), "图片与关键帧目录", "frames", "dir")
        add_path(draft_meta.get("backup_path"), "输入备份", "backup")
        add_path(draft_meta.get("raw_response_path"), "视觉模型原始响应", "vision_raw")

        report_result = record.report_result or {}
        output_dir = report_result.get("output_dir")
        add_path(output_dir, "报告输出目录", "report_output", "dir")
        if output_dir:
            output_path = Path(output_dir).resolve()
            add_path(str(output_path / "guidance.json"), "指导意见 JSON", "guidance")
            add_path(str(output_path / "report.json"), "报告 JSON", "report_json")
            add_path(str(output_path / "report.md"), "报告 Markdown", "report_markdown")
            add_path(str(output_path / "report.docx"), "报告 Word", "report_docx")
            add_path(str(output_path / "report.pdf"), "报告 PDF", "report_pdf")
            add_path(str(output_path / "input_validated.json"), "校验后事故信息", "input_validated")
            add_path(str(output_path / "run_log.json"), "运行日志", "run_log")

        input_generation = report_result.get("input_generation") or {}
        add_path(input_generation.get("workspace_dir"), "输入工作区", "input_workspace", "dir")
        add_path(input_generation.get("input_path"), "事故信息草稿", "generated_input")
        add_path(input_generation.get("yolo_summary_path"), "YOLO 摘要", "yolo_summary")
        add_path(input_generation.get("frames_dir"), "图片与关键帧目录", "frames", "dir")
        return collected

    def _collect_linked_artifacts(self, record: ChatSessionRecord) -> list[ChatSessionLinkedArtifact]:
        categories = [
            "knowledge_snippets",
            "agentic_queries",
            "yolo_full_output",
            "structured_accident_info",
            "images_and_keyframes",
        ]
        artifacts: list[ChatSessionLinkedArtifact] = []
        for category in categories:
            artifact = self._build_linked_artifact_summary(record, category)
            if artifact is None:
                continue
            artifacts.append(artifact)
        return artifacts

    def _build_linked_artifact_summary(
        self,
        record: ChatSessionRecord,
        category: str,
    ) -> ChatSessionLinkedArtifact | None:
        builders = {
            "knowledge_snippets": self._build_knowledge_snippets_summary,
            "agentic_queries": self._build_agentic_queries_summary,
            "yolo_full_output": self._build_yolo_full_output_summary,
            "structured_accident_info": self._build_structured_accident_info_summary,
            "images_and_keyframes": self._build_images_and_keyframes_summary,
        }
        builder = builders.get(category)
        if builder is None:
            return None
        return builder(record)

    def _build_linked_artifact_detail(
        self,
        record: ChatSessionRecord,
        category: str,
    ) -> LinkedArtifactDetailResponse | None:
        builders = {
            "knowledge_snippets": self._build_knowledge_snippets_detail,
            "agentic_queries": self._build_agentic_queries_detail,
            "yolo_full_output": self._build_yolo_full_output_detail,
            "structured_accident_info": self._build_structured_accident_info_detail,
            "images_and_keyframes": self._build_images_and_keyframes_detail,
        }
        builder = builders.get(category)
        if builder is None:
            return None
        return builder(record)

    def _build_knowledge_snippets_summary(
        self,
        record: ChatSessionRecord,
    ) -> ChatSessionLinkedArtifact | None:
        report_result = record.report_result or {}
        initial_count = len(self._coerce_snippet_list(report_result.get("initial_knowledge_snippets")))
        final_count = len(self._coerce_snippet_list(report_result.get("knowledge_snippets")))
        if initial_count <= 0 and final_count <= 0:
            return None
        item_count = final_count or initial_count
        return ChatSessionLinkedArtifact(
            label="检索到的知识库片段",
            category="knowledge_snippets",
            kind="document",
            item_count=item_count,
            summary=f"首轮 {initial_count} 条，最终 {item_count} 条",
        )

    def _build_agentic_queries_summary(
        self,
        record: ChatSessionRecord,
    ) -> ChatSessionLinkedArtifact | None:
        report_result = record.report_result or {}
        rounds = self._coerce_round_list(report_result.get("agentic_retrieval_rounds"))
        if not rounds:
            return None
        return ChatSessionLinkedArtifact(
            label="模型自主搜索关键词",
            category="agentic_queries",
            kind="log",
            item_count=len(rounds),
            summary=f"共 {len(rounds)} 轮自主检索",
        )

    def _build_yolo_full_output_summary(
        self,
        record: ChatSessionRecord,
    ) -> ChatSessionLinkedArtifact | None:
        generation_payload = self._resolve_generation_payload(record)
        preview_payload = generation_payload.get("yolo_summary_preview")
        summary_path = str(generation_payload.get("yolo_summary_path") or "").strip()
        if not isinstance(preview_payload, dict) and not summary_path:
            return None

        video_source_count = 0
        if isinstance(preview_payload, dict):
            video_source_count = int(preview_payload.get("video_source_count", 0) or 0)
            if video_source_count <= 0 and isinstance(preview_payload.get("videos"), list):
                video_source_count = len(preview_payload.get("videos") or [])
        if video_source_count <= 0:
            video_source_count = 1

        return ChatSessionLinkedArtifact(
            label="YOLO 输出的完整内容",
            category="yolo_full_output",
            kind="json",
            item_count=video_source_count,
            summary=f"共 {video_source_count} 个视频源的轨迹与事件摘要",
        )

    def _build_structured_accident_info_summary(
        self,
        record: ChatSessionRecord,
    ) -> ChatSessionLinkedArtifact | None:
        generation_payload = self._resolve_generation_payload(record)
        has_structured_payload = bool(self._parse_json_object(record.draft_json))
        has_structured_payload = has_structured_payload or isinstance(generation_payload.get("generated_input"), dict)
        has_structured_payload = has_structured_payload or bool(generation_payload.get("input_path"))
        if not has_structured_payload:
            return None
        return ChatSessionLinkedArtifact(
            label="结构化事故信息",
            category="structured_accident_info",
            kind="json",
            item_count=1,
            summary="包含事故信息草稿或校验后的结构化结果",
        )

    def _build_images_and_keyframes_summary(
        self,
        record: ChatSessionRecord,
    ) -> ChatSessionLinkedArtifact | None:
        generation_payload = self._resolve_generation_payload(record)
        upload_groups = generation_payload.get("upload_groups")
        frame_manifest = generation_payload.get("frame_manifest")
        preview_payload = generation_payload.get("yolo_summary_preview")

        upload_image_count = 0
        upload_video_count = 0
        if isinstance(upload_groups, list):
            for item in upload_groups:
                if not isinstance(item, dict):
                    continue
                upload_image_count += int(item.get("image_count", 0) or 0)
                upload_video_count += int(item.get("video_count", 0) or 0)

        key_frame_count = len(frame_manifest) if isinstance(frame_manifest, list) else 0
        yolo_preview_count = 0
        if isinstance(preview_payload, dict):
            yolo_preview_count = int(preview_payload.get("video_source_count", 0) or 0)

        total_assets = upload_image_count + upload_video_count + key_frame_count + yolo_preview_count
        if total_assets <= 0:
            workspace_dir = str(generation_payload.get("workspace_dir") or "").strip()
            frames_dir = str(generation_payload.get("frames_dir") or "").strip()
            if not workspace_dir and not frames_dir:
                return None

        summary = (
            f"共 {total_assets} 项素材，"
            f"其中原始图片 {upload_image_count} 张、视频 {upload_video_count} 个、关键帧 {key_frame_count} 张"
        )
        return ChatSessionLinkedArtifact(
            label="图片与关键帧",
            category="images_and_keyframes",
            kind="gallery",
            item_count=max(total_assets, 1),
            summary=summary,
        )

    def _build_knowledge_snippets_detail(
        self,
        record: ChatSessionRecord,
    ) -> LinkedArtifactDetailResponse | None:
        report_result = record.report_result or {}
        initial_snippets = self._coerce_snippet_list(report_result.get("initial_knowledge_snippets"))
        combined_snippets = self._coerce_snippet_list(report_result.get("knowledge_snippets"))
        if not initial_snippets and not combined_snippets:
            return None

        retrieval_meta = dict(report_result.get("retrieval_meta") or {})
        content: list[dict[str, Any]] = []
        for section_label, snippets in (
            ("首轮检索片段", initial_snippets),
            ("最终汇总片段（首轮 + 自主检索）", combined_snippets),
        ):
            for index, snippet in enumerate(snippets, start=1):
                content.append(
                    {
                        "section": section_label,
                        "index": index,
                        "title": str(snippet.get("title") or snippet.get("id") or f"{section_label} {index}"),
                        "content": str(snippet.get("content") or ""),
                        "source": str(snippet.get("source") or ""),
                        "citation": str(snippet.get("citation") or ""),
                        "score": snippet.get("score"),
                        "category": str(snippet.get("category") or ""),
                        "authority": str(snippet.get("authority") or ""),
                        "url": str(snippet.get("url") or ""),
                    }
                )

        final_count = len(combined_snippets) if combined_snippets else len(initial_snippets)
        return LinkedArtifactDetailResponse(
            category="knowledge_snippets",
            label="检索到的知识库片段",
            kind="document",
            summary=f"首轮 {len(initial_snippets)} 条，最终 {final_count} 条",
            meta={
                "item_count": final_count,
                "initial_count": len(initial_snippets),
                "final_count": final_count,
                "initial_query": str(retrieval_meta.get("initial_query") or ""),
                "last_query": str(retrieval_meta.get("last_query") or ""),
                "retrieval_meta": retrieval_meta,
            },
            content=content,
        )

    def _build_agentic_queries_detail(
        self,
        record: ChatSessionRecord,
    ) -> LinkedArtifactDetailResponse | None:
        report_result = record.report_result or {}
        rounds = self._coerce_round_list(report_result.get("agentic_retrieval_rounds"))
        if not rounds:
            return None

        content = [
            {
                "round": int(round_item.get("round", 0) or 0),
                "query": str(round_item.get("query") or ""),
                "reason": str(round_item.get("reason") or ""),
                "requested_top_k": int(round_item.get("requested_top_k", 0) or 0),
                "returned_count": int(round_item.get("returned_count", 0) or 0),
                "snippets": self._coerce_snippet_list(round_item.get("snippets")),
            }
            for round_item in rounds
        ]
        return LinkedArtifactDetailResponse(
            category="agentic_queries",
            label="模型自主搜索关键词",
            kind="log",
            summary=f"共 {len(rounds)} 轮自主检索",
            meta={"total_rounds": len(rounds)},
            content=content,
        )

    def _build_yolo_full_output_detail(
        self,
        record: ChatSessionRecord,
    ) -> LinkedArtifactDetailResponse | None:
        generation_payload = self._resolve_generation_payload(record)
        yolo_summary_path = self._resolve_safe_path(generation_payload.get("yolo_summary_path"))
        yolo_payload = self._read_json_if_exists(yolo_summary_path) if yolo_summary_path else None
        preview_payload = generation_payload.get("yolo_summary_preview")
        if not isinstance(yolo_payload, dict) and not isinstance(preview_payload, dict):
            return None

        source_type = str((yolo_payload or {}).get("source_type") or (preview_payload or {}).get("source_type") or "video")
        if isinstance(yolo_payload, dict) and isinstance(yolo_payload.get("video_sources"), list):
            video_sources = [
                item
                for item in yolo_payload.get("video_sources", [])
                if isinstance(item, dict)
            ]
            prompt_sources = [
                item
                for item in yolo_payload.get("prompt_sources", [])
                if isinstance(item, dict)
            ]
        elif isinstance(yolo_payload, dict):
            video_sources = [{"source_name": "事故视频", "summary": yolo_payload}]
            prompt_sources = []
        else:
            video_sources = []
            prompt_sources = []

        content = []
        for index, item in enumerate(video_sources, start=1):
            summary = dict(item.get("summary") or {})
            content.append(
                {
                    "index": index,
                    "source_name": str(item.get("source_name") or f"视频源 {index}"),
                    "category_id": str(item.get("category_id") or ""),
                    "category_label": str(item.get("category_label") or ""),
                    "category_subtitle": str(item.get("category_subtitle") or ""),
                    "video": dict(summary.get("video") or {}),
                    "detection": dict(summary.get("detection") or {}),
                    "track_summaries": list(summary.get("track_summaries") or []),
                    "event_candidates": list(summary.get("event_candidates") or []),
                }
            )

        if not content and isinstance(preview_payload, dict):
            content.append(
                {
                    "index": 1,
                    "source_name": "YOLO 摘要预览",
                    "preview": preview_payload,
                }
            )

        return LinkedArtifactDetailResponse(
            category="yolo_full_output",
            label="YOLO 输出的完整内容",
            kind="json",
            summary=f"共 {len(content)} 个视频源的轨迹与事件摘要",
            meta={
                "source_type": source_type,
                "summary_path": str(yolo_summary_path.resolve()) if yolo_summary_path else "",
                "prompt_sources": prompt_sources,
            },
            content=content,
        )

    def _build_structured_accident_info_detail(
        self,
        record: ChatSessionRecord,
    ) -> LinkedArtifactDetailResponse | None:
        draft_payload = self._parse_json_object(record.draft_json)
        if isinstance(draft_payload, dict) and draft_payload:
            output_dir = self._resolve_report_output_dir(record)
            validated_path = output_dir / "input_validated.json" if output_dir else None
            return LinkedArtifactDetailResponse(
                category="structured_accident_info",
                label="结构化事故信息",
                kind="json",
                summary="当前展示的是前端确认并编辑后的结构化事故信息",
                meta={
                    "stage": "confirmed_draft",
                    "source_path": "chat_session.draft_json",
                    "validated_source_path": str(validated_path.resolve()) if validated_path and validated_path.exists() else "",
                },
                content=[
                    {
                        "stage": "confirmed_draft",
                        "payload": draft_payload,
                    }
                ],
            )

        output_dir = self._resolve_report_output_dir(record)
        validated_path = output_dir / "input_validated.json" if output_dir else None
        validated_payload = self._read_json_if_exists(validated_path) if validated_path else None
        if isinstance(validated_payload, dict):
            return LinkedArtifactDetailResponse(
                category="structured_accident_info",
                label="结构化事故信息",
                kind="json",
                summary="当前展示的是校验后的结构化事故信息",
                meta={
                    "stage": "validated",
                    "source_path": str(validated_path.resolve()),
                },
                content=[
                    {
                        "stage": "validated",
                        "payload": validated_payload,
                    }
                ],
            )

        generation_payload = self._resolve_generation_payload(record)
        input_path = self._resolve_safe_path(generation_payload.get("input_path"))
        draft_payload = self._read_json_if_exists(input_path) if input_path else None
        if not isinstance(draft_payload, dict):
            draft_payload = None
        if not draft_payload and isinstance(generation_payload.get("generated_input"), dict):
            draft_payload = dict(generation_payload.get("generated_input") or {})
        if not isinstance(draft_payload, dict) or not draft_payload:
            return None

        return LinkedArtifactDetailResponse(
            category="structured_accident_info",
            label="结构化事故信息",
            kind="json",
            summary="当前展示的是事故信息草稿",
            meta={
                "stage": "draft",
                "source_path": str(input_path.resolve()) if input_path else "",
            },
            content=[
                {
                    "stage": "draft",
                    "payload": draft_payload,
                }
            ],
        )

    @staticmethod
    def _stable_asset_id(path_value: str) -> str:
        digest = hashlib.sha1(str(path_value).encode("utf-8")).hexdigest()
        return f"img-{digest[:16]}"

    @staticmethod
    def _asset_path_exists(path_value: str) -> bool:
        try:
            return Path(path_value).resolve().exists()
        except OSError:
            return False

    def _enumerate_images_and_keyframes_assets(
        self,
        record: ChatSessionRecord,
    ) -> tuple[list[LinkedArtifactAsset], dict[str, dict[str, Any]]]:
        """枚举图片/关键帧/YOLO 资产候选，asset_id 为路径派生稳定哈希。

        这里刻意不做 per-asset exists() 过滤：是否存在交由调用方按场景决定
        （详情页用于展示过滤，资产解析只校验命中的那一个）。这样画廊渲染时
        N 次 asset 请求不会各自触发 O(N) 次远端 stat。
        """
        workspace_dir = self._resolve_generation_workspace(record)
        generation_payload = self._resolve_generation_payload(record)
        upload_groups_payload = generation_payload.get("upload_groups")
        category_meta = self._build_category_meta_map(
            upload_groups_payload,
            workspace_dir,
        )
        asset_rows: list[dict[str, Any]] = []
        seen_paths: set[str] = set()

        if workspace_dir:
            manifest_items = self._extract_upload_manifest_items_from_payload(upload_groups_payload)
            if not manifest_items:
                upload_manifest = self._read_json_if_exists(workspace_dir / "upload_manifest.json")
                manifest_items = (
                    upload_manifest.get("items", [])
                    if isinstance(upload_manifest, dict)
                    else []
                )
            for item in manifest_items:
                if not isinstance(item, dict):
                    continue
                asset = self._build_asset_payload(
                    path_value=item.get("path"),
                    kind="upload_video" if str(item.get("media_type") or "") == "video" else "upload_image",
                    media_type=str(item.get("media_type") or ""),
                    file_name=str(item.get("original_name") or Path(str(item.get("path") or "")).name),
                    category_id=str(item.get("category_id") or ""),
                    category_label=str(item.get("category_label") or ""),
                    source_name=str(item.get("original_name") or ""),
                    reason="原始上传材料",
                    sequence=int(item.get("sequence", item.get("group_sequence", 0)) or 0),
                    annotation_label="原始上传",
                    require_exists=False,
                )
                if asset is None:
                    continue
                normalized_path = str(Path(asset["path"]).resolve())
                if normalized_path in seen_paths:
                    continue
                seen_paths.add(normalized_path)
                category_sequence = self._resolve_category_sequence(category_meta, asset["category_id"])
                asset_rows.append(
                    {
                        "sort_key": (
                            category_sequence,
                            0,
                            int(item.get("group_sequence", 0) or 0),
                            int(item.get("sequence", 0) or 0),
                            0.0,
                        ),
                        "payload": asset,
                    }
                )

            key_frame_items = self._load_key_frame_manifest(workspace_dir, generation_payload)
            for item in key_frame_items:
                if not isinstance(item, dict):
                    continue
                asset = self._build_asset_payload(
                    path_value=item.get("path"),
                    kind="key_frame",
                    media_type="image",
                    file_name=Path(str(item.get("path") or "")).name,
                    category_id=str(item.get("category_id") or ""),
                    category_label=str(item.get("category_label") or ""),
                    source_name=str(item.get("source_name") or ""),
                    reason=str(item.get("reason") or ""),
                    sequence=int(item.get("sequence", 0) or 0),
                    timestamp_seconds=float(item.get("timestamp_seconds", 0.0) or 0.0),
                    annotation_label="关键帧",
                    require_exists=False,
                )
                if asset is None:
                    continue
                normalized_path = str(Path(asset["path"]).resolve())
                if normalized_path in seen_paths:
                    continue
                seen_paths.add(normalized_path)
                category_sequence = self._resolve_category_sequence(category_meta, asset["category_id"])
                asset_rows.append(
                    {
                        "sort_key": (
                            category_sequence,
                            1,
                            int(item.get("sequence", 0) or 0),
                            int(item.get("group_sequence", 0) or 0),
                            float(item.get("timestamp_seconds", 0.0) or 0.0),
                        ),
                        "payload": asset,
                    }
                )

            yolo_manifest = self._extract_yolo_asset_manifest_from_payload(
                generation_payload.get("yolo_asset_manifest")
            )
            if not yolo_manifest:
                yolo_dir = workspace_dir / "yolo"
                if yolo_dir.exists():
                    yolo_manifest = self._build_legacy_yolo_asset_manifest(yolo_dir)
            for item in yolo_manifest:
                if not isinstance(item, dict):
                    continue
                normalized_path = str(Path(str(item.get("path") or "")).resolve())
                if normalized_path in seen_paths:
                    continue
                category_id = str(item.get("category_id") or "")
                category_label = str(item.get("category_label") or category_meta.get(category_id, {}).get("category_label", ""))
                asset = self._build_asset_payload(
                    path_value=item.get("path"),
                    kind="yolo_annotated",
                    media_type="image",
                    file_name=str(item.get("file_name") or Path(str(item.get("path") or "")).name),
                    category_id=category_id,
                    category_label=category_label,
                    source_name=str(item.get("source_name") or ""),
                    reason="YOLO 标注输出",
                    annotation_label="YOLO 标注图",
                    require_exists=False,
                )
                if asset is None:
                    continue
                seen_paths.add(normalized_path)
                category_sequence = self._resolve_category_sequence(category_meta, asset["category_id"])
                asset_rows.append(
                    {
                        "sort_key": (
                            category_sequence,
                            2,
                            0,
                            0,
                            float(len(asset_rows)),
                        ),
                        "payload": asset,
                    }
                )

        ordered_rows = sorted(asset_rows, key=lambda item: item["sort_key"])
        ordered_assets: list[LinkedArtifactAsset] = []
        for row in ordered_rows:
            payload = dict(row["payload"])
            ordered_assets.append(
                LinkedArtifactAsset(
                    asset_id=self._stable_asset_id(str(payload["path"])),
                    kind=str(payload["kind"]),
                    media_type=str(payload["media_type"]),
                    file_name=str(payload["file_name"]),
                    path=str(payload["path"]),
                    mime_type=payload.get("mime_type"),
                    category_id=payload.get("category_id"),
                    category_label=payload.get("category_label"),
                    source_name=payload.get("source_name"),
                    reason=payload.get("reason"),
                    sequence=payload.get("sequence"),
                    timestamp_seconds=payload.get("timestamp_seconds"),
                    annotation_label=payload.get("annotation_label"),
                )
            )
        return ordered_assets, category_meta

    def _build_images_and_keyframes_detail(
        self,
        record: ChatSessionRecord,
    ) -> LinkedArtifactDetailResponse | None:
        ordered_assets, category_meta = self._enumerate_images_and_keyframes_assets(record)
        assets = [asset for asset in ordered_assets if self._asset_path_exists(asset.path)]
        if not assets:
            return None

        workspace_dir = self._resolve_generation_workspace(record)
        grouped_content: dict[str, dict[str, Any]] = {}
        upload_image_count = 0
        upload_video_count = 0
        key_frame_count = 0

        for asset in assets:
            if asset.kind == "upload_image":
                upload_image_count += 1
            elif asset.kind == "upload_video":
                upload_video_count += 1
            elif asset.kind == "key_frame":
                key_frame_count += 1

            group_key = str(asset.category_id or "ungrouped")
            category_info = category_meta.get(group_key, {})
            bucket = grouped_content.setdefault(
                group_key,
                {
                    "category_id": asset.category_id or "",
                    "category_label": asset.category_label or category_info.get("category_label") or "未分组材料",
                    "category_subtitle": category_info.get("category_subtitle") or "",
                    "sequence": category_info.get("sequence") or 999,
                    "items": [],
                },
            )
            bucket["items"].append(
                {
                    "asset_id": asset.asset_id,
                    "kind": asset.kind,
                    "media_type": asset.media_type,
                    "file_name": asset.file_name,
                    "source_name": asset.source_name,
                    "reason": asset.reason,
                    "sequence": asset.sequence,
                    "timestamp_seconds": asset.timestamp_seconds,
                    "annotation_label": asset.annotation_label,
                }
            )

        content = [
            grouped_content[key]
            for key in sorted(
                grouped_content,
                key=lambda item: (
                    int(grouped_content[item].get("sequence", 999) or 999),
                    str(grouped_content[item].get("category_label") or ""),
                ),
            )
        ]
        summary = (
            f"共 {len(assets)} 项素材，"
            f"其中原始图片 {upload_image_count} 张、视频 {upload_video_count} 个、关键帧 {key_frame_count} 张"
        )
        return LinkedArtifactDetailResponse(
            category="images_and_keyframes",
            label="图片与关键帧",
            kind="gallery",
            summary=summary,
            meta={
                "workspace_dir": str(workspace_dir.resolve()) if workspace_dir else "",
                "total_assets": len(assets),
            },
            content=content,
            assets=assets,
        )

    def _build_category_meta_map(
        self,
        upload_groups_payload: Any,
        workspace_dir: Path | None,
    ) -> dict[str, dict[str, Any]]:
        category_meta: dict[str, dict[str, Any]] = {}
        if isinstance(upload_groups_payload, list):
            for index, item in enumerate(upload_groups_payload, start=1):
                if not isinstance(item, dict):
                    continue
                category_id = str(item.get("category_id") or "").strip()
                if not category_id:
                    continue
                category_meta[category_id] = {
                    "category_label": str(item.get("category_label") or "").strip(),
                    "category_subtitle": str(item.get("category_subtitle") or "").strip(),
                    "sequence": int(item.get("sequence", index) or index),
                }

        if workspace_dir:
            upload_manifest = self._read_json_if_exists(workspace_dir / "upload_manifest.json")
            manifest_groups = (
                upload_manifest.get("groups", [])
                if isinstance(upload_manifest, dict)
                else []
            )
            for index, item in enumerate(manifest_groups, start=1):
                if not isinstance(item, dict):
                    continue
                category_id = str(item.get("category_id") or "").strip()
                if not category_id or category_id in category_meta:
                    continue
                category_meta[category_id] = {
                    "category_label": str(item.get("category_label") or "").strip(),
                    "category_subtitle": str(item.get("category_subtitle") or "").strip(),
                    "sequence": int(item.get("sequence", index) or index),
                }
        return category_meta

    def _load_key_frame_manifest(
        self,
        workspace_dir: Path,
        generation_payload: dict[str, Any],
    ) -> list[dict[str, Any]]:
        payload_manifest = generation_payload.get("frame_manifest")
        if isinstance(payload_manifest, list):
            return [item for item in payload_manifest if isinstance(item, dict)]

        key_frame_manifest = self._read_json_if_exists(workspace_dir / "key_frame_manifest.json")
        if isinstance(key_frame_manifest, list):
            return [item for item in key_frame_manifest if isinstance(item, dict)]

        frame_manifest = self._read_json_if_exists(workspace_dir / "frame_manifest.json")
        if isinstance(frame_manifest, list):
            return [item for item in frame_manifest if isinstance(item, dict)]
        return []

    def _extract_upload_manifest_items_from_payload(self, upload_groups_payload: Any) -> list[dict[str, Any]]:
        if not isinstance(upload_groups_payload, list):
            return []

        items: list[dict[str, Any]] = []
        for group in upload_groups_payload:
            if not isinstance(group, dict):
                continue
            category_id = str(group.get("category_id") or "").strip()
            category_label = str(group.get("category_label") or "").strip()
            category_subtitle = str(group.get("category_subtitle") or "").strip()
            category_sequence = int(group.get("sequence", 0) or 0)
            for file_item in group.get("files", []) or []:
                if not isinstance(file_item, dict):
                    continue
                media_type = str(file_item.get("media_type") or "").strip()
                path_value = str(file_item.get("path") or "").strip()
                if not media_type or not path_value:
                    continue
                items.append(
                    {
                        "path": path_value,
                        "media_type": media_type,
                        "original_name": str(file_item.get("original_name") or Path(path_value).name),
                        "category_id": category_id,
                        "category_label": category_label,
                        "category_subtitle": category_subtitle,
                        "category_sequence": category_sequence,
                        "group_sequence": int(file_item.get("sequence", 0) or 0),
                        "sequence": int(file_item.get("sequence", 0) or 0),
                    }
                )
        return items

    def _extract_yolo_asset_manifest_from_payload(self, payload: Any) -> list[dict[str, Any]]:
        if not isinstance(payload, list):
            return []
        return [item for item in payload if isinstance(item, dict) and str(item.get("path") or "").strip()]

    def _build_legacy_yolo_asset_manifest(self, yolo_dir: Path) -> list[dict[str, Any]]:
        resolved_dir = yolo_dir.resolve()
        cache_key = str(resolved_dir)
        current_mtime_ns = self._get_output_dir_mtime_ns(resolved_dir)
        with _YOLO_DIR_MANIFEST_GUARD:
            cached = _YOLO_DIR_MANIFEST_CACHE.get(cache_key)
        if cached and cached.get("mtime_ns") == current_mtime_ns:
            return [dict(item) for item in cached.get("items", [])]

        items: list[dict[str, Any]] = []
        for path in sorted(resolved_dir.rglob("*")):
            if not path.is_file() or path.suffix.lower() not in {".jpg", ".jpeg", ".png", ".webp"}:
                continue
            relative_parts = path.relative_to(resolved_dir).parts
            items.append(
                {
                    "path": str(path.resolve()),
                    "file_name": path.name,
                    "category_id": relative_parts[0] if len(relative_parts) > 1 else "",
                    "category_label": "",
                    "source_name": path.parent.name,
                }
            )

        payload = {
            "mtime_ns": current_mtime_ns,
            "items": items,
        }
        with _YOLO_DIR_MANIFEST_GUARD:
            _YOLO_DIR_MANIFEST_CACHE[cache_key] = payload
        return [dict(item) for item in items]

    def _build_asset_payload(
        self,
        path_value: Any,
        kind: str,
        media_type: str,
        file_name: str,
        category_id: str,
        category_label: str,
        source_name: str = "",
        reason: str = "",
        sequence: int | None = None,
        timestamp_seconds: float | None = None,
        annotation_label: str | None = None,
        require_exists: bool = True,
    ) -> dict[str, Any] | None:
        path = self._resolve_safe_path(path_value)
        if path is None:
            return None
        if require_exists and not path.exists():
            return None
        normalized_media_type = media_type or ("video" if path.suffix.lower() in {".mp4", ".mov", ".avi", ".mkv", ".webm"} else "image")
        guessed_mime_type = mimetypes.guess_type(path.name)[0]
        return {
            "kind": kind,
            "media_type": normalized_media_type,
            "file_name": file_name or path.name,
            "path": str(path.resolve()),
            "mime_type": guessed_mime_type,
            "category_id": category_id or None,
            "category_label": category_label or None,
            "source_name": source_name or None,
            "reason": reason or None,
            "sequence": sequence,
            "timestamp_seconds": timestamp_seconds,
            "annotation_label": annotation_label or None,
        }

    def _resolve_generation_payload(self, record: ChatSessionRecord) -> dict[str, Any]:
        report_result = record.report_result or {}
        input_generation = report_result.get("input_generation")
        if isinstance(input_generation, dict):
            return input_generation
        draft_meta = record.draft_meta or {}
        return draft_meta if isinstance(draft_meta, dict) else {}

    def _resolve_generation_workspace(self, record: ChatSessionRecord) -> Path | None:
        generation_payload = self._resolve_generation_payload(record)
        return self._resolve_safe_path(generation_payload.get("workspace_dir"))

    def _resolve_report_output_dir(self, record: ChatSessionRecord) -> Path | None:
        report_result = record.report_result or {}
        return self._resolve_safe_path(report_result.get("output_dir"))

    def _resolve_safe_path(self, path_value: Any) -> Path | None:
        if not path_value:
            return None
        try:
            path = Path(str(path_value)).resolve()
        except OSError:
            return None
        if not self._is_safe_data_path(path):
            return None
        return path

    @staticmethod
    def _resolve_category_sequence(
        category_meta: dict[str, dict[str, Any]],
        category_id: str | None,
    ) -> int:
        if category_id and category_id in category_meta:
            return int(category_meta[category_id].get("sequence", 999) or 999)
        return 999

    def _write_session(self, record: ChatSessionRecord) -> None:
        payload = record.model_dump()
        with self.database_service.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    insert into chat_sessions (
                        id, title, owner_user_id, owner_username, created_at, updated_at,
                        sort_order, source_type, source_name, messages, draft_json,
                        draft_meta, report_result, session_state
                    )
                    values (
                        %s, %s, nullif(%s, '')::uuid, %s, %s, %s,
                        %s, %s, %s, %s::jsonb, %s,
                        %s::jsonb, %s::jsonb, %s
                    )
                    on conflict (id) do update
                    set title = excluded.title,
                        owner_user_id = excluded.owner_user_id,
                        owner_username = excluded.owner_username,
                        created_at = excluded.created_at,
                        updated_at = excluded.updated_at,
                        sort_order = excluded.sort_order,
                        source_type = excluded.source_type,
                        source_name = excluded.source_name,
                        messages = excluded.messages,
                        draft_json = excluded.draft_json,
                        draft_meta = excluded.draft_meta,
                        report_result = excluded.report_result,
                        session_state = excluded.session_state
                    """,
                    (
                        record.id,
                        record.title,
                        record.owner_user_id or "",
                        record.owner_username,
                        record.created_at,
                        record.updated_at,
                        record.sort_order,
                        record.source_type,
                        record.source_name,
                        Jsonb(payload.get("messages") or []),
                        record.draft_json,
                        Jsonb(payload.get("draft_meta")),
                        Jsonb(payload.get("report_result")),
                        record.session_state,
                    ),
                )
            conn.commit()

    def _load_session_row(
        self,
        row: dict[str, Any],
        *,
        include_linked_files: bool = True,
        include_linked_artifacts: bool = True,
    ) -> ChatSessionRecord:
        record = ChatSessionRecord.model_validate(
            {
                "id": row["id"],
                "title": row["title"],
                "owner_user_id": row.get("owner_user_id"),
                "owner_username": row.get("owner_username"),
                "created_at": row["created_at"],
                "updated_at": row["updated_at"],
                "sort_order": row.get("sort_order"),
                "source_type": row.get("source_type"),
                "source_name": row.get("source_name"),
                "messages": row.get("messages") or [],
                "draft_json": row.get("draft_json") or "",
                "draft_meta": row.get("draft_meta"),
                "report_result": row.get("report_result"),
                "session_state": row.get("session_state") or "draft",
            }
        )
        migrated_record = self._apply_session_state(self._migrate_session_record(record))
        if self._has_persisted_changes(record, migrated_record):
            self._write_session(migrated_record)
        return self._refresh_linked_views(
            migrated_record,
            include_linked_files=include_linked_files,
            include_linked_artifacts=include_linked_artifacts,
        )

    def _load_session_by_id(
        self,
        session_id: str,
        *,
        include_linked_files: bool = True,
        include_linked_artifacts: bool = True,
    ) -> ChatSessionRecord:
        with self.database_service.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    select id, title, owner_user_id::text as owner_user_id, owner_username,
                           created_at, updated_at, sort_order, source_type, source_name,
                           messages, draft_json, draft_meta, report_result, session_state
                    from chat_sessions
                    where id = %s
                    """,
                    (session_id,),
                )
                row = cur.fetchone()
        if row is not None:
            return self._load_session_row(
                row,
                include_linked_files=include_linked_files,
                include_linked_artifacts=include_linked_artifacts,
            )

        legacy_file = self._session_file(session_id)
        if legacy_file.exists():
            record = self._load_session_file(
                legacy_file,
                include_linked_files=include_linked_files,
                include_linked_artifacts=include_linked_artifacts,
            )
            self._write_session(record)
            return record
        raise SessionNotFoundError(f"会话不存在: {session_id}")

    def _load_session_file(
        self,
        session_file: Path,
        *,
        include_linked_files: bool = True,
        include_linked_artifacts: bool = True,
    ) -> ChatSessionRecord:
        record = ChatSessionRecord.model_validate(json.loads(session_file.read_text(encoding="utf-8")))
        migrated_record = self._apply_session_state(self._migrate_session_record(record))
        return self._refresh_linked_views(
            migrated_record,
            include_linked_files=include_linked_files,
            include_linked_artifacts=include_linked_artifacts,
        )

    @staticmethod
    def _has_persisted_changes(current: ChatSessionRecord, candidate: ChatSessionRecord) -> bool:
        return ChatSessionService._build_persisted_payload(current) != ChatSessionService._build_persisted_payload(
            candidate
        )

    @staticmethod
    def _build_persisted_payload(record: ChatSessionRecord) -> dict[str, Any]:
        payload = record.model_dump(exclude={"linked_files", "linked_artifacts"})
        for field in ("draft_meta", "report_result"):
            if payload.get(field) == {}:
                payload[field] = None
        return payload

    def _migrate_legacy_file_sessions(self) -> None:
        root = self.settings.chat_sessions_dir_path
        if not root.exists():
            return
        data_root_key = str(self.settings.backend_data_dir_path)
        with _LEGACY_SESSION_MIGRATED_GUARD:
            if data_root_key in _LEGACY_SESSION_MIGRATED_DATA_ROOTS:
                return
        with self.database_service.connection() as conn:
            with conn.cursor() as cur:
                cur.execute("select id from chat_sessions")
                existing_ids = {str(row["id"]) for row in cur.fetchall()}
        migrated_any = False
        for session_dir in root.iterdir():
            if not session_dir.is_dir():
                continue
            session_file = session_dir / "session.json"
            if not session_file.exists() or session_dir.name in existing_ids:
                continue
            try:
                with _get_session_lock(session_dir.name):
                    record = self._load_session_file(session_file)
                    self._write_session(record)
                    migrated_any = True
            except (JSONDecodeError, OSError, UnicodeDecodeError, ValidationError) as exc:
                logger.warning("迁移旧会话文件失败：%s；错误：%s", session_file, exc)
        if migrated_any:
            logger.info("已把旧会话文件迁移到 PostgreSQL。")
        with _LEGACY_SESSION_MIGRATED_GUARD:
            _LEGACY_SESSION_MIGRATED_DATA_ROOTS.add(data_root_key)

    def _apply_session_state(self, record: ChatSessionRecord) -> ChatSessionRecord:
        return record.model_copy(update={"session_state": self._resolve_session_state(record)})

    def _resolve_session_state(self, record: ChatSessionRecord) -> str:
        report_result = record.report_result or {}
        report = report_result.get("report") if isinstance(report_result, dict) else None
        report_markdown = str((report or {}).get("report_markdown") or "").strip() if isinstance(report, dict) else ""

        if self._has_export_artifact(record):
            return "export_ready"
        if report_markdown:
            return "report_ready"
        if self._has_running_report_progress(record):
            return "report_running"
        if self._has_cancelled_report_progress(record):
            return "cancelled"
        if self._has_failed_report_progress(record):
            return "failed"
        if record.draft_json.strip() or record.draft_meta:
            return "input_ready"
        return "draft"

    def _has_running_report_progress(self, record: ChatSessionRecord) -> bool:
        for message in record.messages:
            meta = message.meta or {}
            if meta.get("badge") == REPORT_PROGRESS_BADGE and meta.get("status") == "running":
                return True
        return False

    def _has_failed_report_progress(self, record: ChatSessionRecord) -> bool:
        for message in record.messages:
            meta = message.meta or {}
            if meta.get("badge") == REPORT_PROGRESS_BADGE and meta.get("status") == "error":
                return True
        return False

    def _has_cancelled_report_progress(self, record: ChatSessionRecord) -> bool:
        for message in record.messages:
            meta = message.meta or {}
            title = str(meta.get("title") or "").strip()
            if (
                "已停止当前报告生成" in message.content
                or "报告生成已取消" in message.content
                or "报告生成中断" in message.content
                or (meta.get("badge") == REPORT_PROGRESS_BADGE and any(flag in title for flag in ("已停止", "已取消", "已中断")))
            ):
                return True
        return False

    def _has_export_artifact(self, record: ChatSessionRecord) -> bool:
        for linked_file in record.linked_files:
            if linked_file.category in {"report_docx", "report_pdf"} and linked_file.exists:
                return True
        return False

    def _migrate_session_record(self, record: ChatSessionRecord) -> ChatSessionRecord:
        recovered_report_result = self._load_latest_report_result(record)
        history_messages: list[ChatMessageRecord] = []
        if recovered_report_result:
            recovered_report_result, history_messages = self._ensure_history_knowledge_content(
                record=record,
                report_result=recovered_report_result,
            )
        migrated_messages = []
        report_markdown = str(
            ((recovered_report_result or {}).get("report") or {}).get("report_markdown") or ""
        ).strip()

        for message in record.messages:
            if (
                message.role == "system"
                and message.kind == "text"
                and message.content in LEGACY_WELCOME_MESSAGES
            ):
                migrated_messages.append(message.model_copy(update={"content": WELCOME_MESSAGE}))
                continue
            if (
                message.role == "assistant"
                and message.kind == "markdown"
                and (
                    message.content.startswith(LEGACY_MESSAGE_PREFIXES_TO_DROP)
                    or (report_markdown and message.content.strip() == report_markdown)
                )
            ):
                continue
            if recovered_report_result and self._is_report_progress_message(message):
                migrated_messages.append(self._mark_report_progress_completed(message))
                continue
            migrated_messages.append(message)

        migrated_messages = self._append_missing_history_messages(migrated_messages, history_messages)

        updates: dict[str, Any] = {}
        if recovered_report_result != record.report_result:
            updates["report_result"] = recovered_report_result
        if migrated_messages != record.messages:
            updates["messages"] = migrated_messages

        if not updates:
            return record
        return record.model_copy(update=updates)

    def _load_latest_report_result(self, record: ChatSessionRecord) -> dict[str, Any] | None:
        refreshed_current = self._refresh_report_result_from_output_dir(record.report_result)
        if refreshed_current:
            return refreshed_current
        return record.report_result or self._recover_report_result(record)

    def _refresh_report_result_from_output_dir(
        self,
        report_result: Any,
    ) -> dict[str, Any] | None:
        if not isinstance(report_result, dict):
            return None

        output_dir_value = str(report_result.get("output_dir") or "").strip()
        if not output_dir_value:
            return dict(report_result)

        output_dir = Path(output_dir_value).resolve()
        if not self._is_safe_data_path(output_dir):
            return dict(report_result)
        if not output_dir.exists():
            return dict(report_result)

        cached_mtime_ns = self._coerce_output_dir_mtime_ns(report_result.get("_output_dir_mtime_ns"))
        current_mtime_ns = self._get_output_dir_mtime_ns(output_dir)
        if cached_mtime_ns is not None and current_mtime_ns == cached_mtime_ns:
            return dict(report_result)

        refreshed = self._build_report_result_from_output_dir(
            output_dir,
            existing_result=report_result,
        )
        return refreshed or dict(report_result)

    def _recover_report_result(self, record: ChatSessionRecord) -> dict[str, Any] | None:
        if not self.settings.output_dir_path.exists():
            return None

        candidates: list[tuple[int, dict[str, Any]]] = []
        for entry in self._list_output_dir_metadata():
            if entry["session_id"] != record.id:
                continue
            output_dir = Path(entry["path"])

            recovered = self._build_report_result_from_output_dir(output_dir)
            if not recovered:
                continue

            mtime_ms = int(float(entry["mtime"]) * 1000)
            candidates.append((mtime_ms, recovered))

        if not candidates:
            return None

        candidates.sort(key=lambda item: item[0], reverse=True)
        return candidates[0][1]

    def _build_report_result_from_output_dir(
        self,
        output_dir: Path,
        existing_result: dict[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        guidance_payload = self._read_json_if_exists(output_dir / "guidance.json")
        report_payload = self._read_json_if_exists(output_dir / "report.json")
        if not isinstance(guidance_payload, dict) or not isinstance(report_payload, dict):
            return None

        existing_result = existing_result if isinstance(existing_result, dict) else {}
        run_log = self._read_json_if_exists(output_dir / "run_log.json")
        retrieval_meta = dict(existing_result.get("retrieval_meta") or {})
        if isinstance(run_log, dict) and isinstance(run_log.get("retrieval"), dict):
            retrieval_meta = run_log["retrieval"]

        initial_snippets = self._coerce_snippet_list(existing_result.get("initial_knowledge_snippets"))
        if isinstance(run_log, dict):
            raw_initial_snippets = run_log.get("initial_knowledge_snippets")
            if isinstance(raw_initial_snippets, list):
                initial_snippets = raw_initial_snippets

        combined_snippets = self._coerce_snippet_list(existing_result.get("knowledge_snippets"))
        if isinstance(run_log, dict):
            raw_combined_snippets = run_log.get("knowledge_snippets")
            if isinstance(raw_combined_snippets, list):
                combined_snippets = raw_combined_snippets

        agentic_rounds = self._coerce_round_list(existing_result.get("agentic_retrieval_rounds"))
        if isinstance(run_log, dict):
            raw_agentic_rounds = run_log.get("agentic_retrieval_rounds")
            if isinstance(raw_agentic_rounds, list):
                agentic_rounds = raw_agentic_rounds

        report_markdown = self._read_text_if_exists(output_dir / "report.md")
        if report_markdown is None:
            report_markdown = str(report_payload.get("report_markdown") or "")

        return {
            "trace_id": str((run_log or {}).get("trace_id") or existing_result.get("trace_id") or output_dir.name),
            "status": "success",
            "output_dir": str(output_dir.resolve()),
            "_output_dir_mtime_ns": self._get_output_dir_mtime_ns(output_dir),
            "guidance": guidance_payload,
            "report": {
                "report_markdown": report_markdown,
                "sections": report_payload.get("sections") or [],
                "citations": report_payload.get("citations") or [],
                "meta": report_payload.get("meta") or {},
            },
            "initial_knowledge_snippets": initial_snippets,
            "knowledge_snippets": combined_snippets,
            "retrieval_meta": retrieval_meta if isinstance(retrieval_meta, dict) else {},
            "agentic_retrieval_rounds": agentic_rounds,
            "input_generation": existing_result.get("input_generation"),
        }

    def _ensure_history_knowledge_content(
        self,
        record: ChatSessionRecord,
        report_result: dict[str, Any],
    ) -> tuple[dict[str, Any], list[ChatMessageRecord]]:
        result = dict(report_result)
        retrieval_meta = dict(result.get("retrieval_meta") or {})
        initial_snippets = self._coerce_snippet_list(result.get("initial_knowledge_snippets"))
        combined_snippets = self._coerce_snippet_list(result.get("knowledge_snippets"))
        agentic_rounds = self._coerce_round_list(result.get("agentic_retrieval_rounds"))

        if not initial_snippets:
            initial_snippets, recovered_meta = self._retrieve_initial_snippets(record)
            if initial_snippets:
                retrieval_meta = {**recovered_meta, **retrieval_meta}
        if not initial_snippets and combined_snippets:
            initial_snippets = combined_snippets

        if not combined_snippets:
            combined_snippets = self._merge_snippet_lists(
                initial_snippets,
                [snippet for round_item in agentic_rounds for snippet in self._coerce_snippet_list(round_item.get("snippets"))],
            )

        if initial_snippets:
            result["initial_knowledge_snippets"] = initial_snippets
        if combined_snippets:
            result["knowledge_snippets"] = combined_snippets
        if retrieval_meta:
            result["retrieval_meta"] = retrieval_meta

        history_messages: list[ChatMessageRecord] = []
        knowledge_markdown = self._format_knowledge_snippets_markdown(initial_snippets, retrieval_meta)
        if knowledge_markdown:
            history_messages.append(
                ChatMessageRecord(
                    id=f"history-knowledge-{result.get('trace_id') or record.id}",
                    role="assistant",
                    kind="markdown",
                    content=knowledge_markdown,
                )
            )

        agentic_markdown = self._format_agentic_rounds_markdown(agentic_rounds)
        if agentic_markdown:
            history_messages.append(
                ChatMessageRecord(
                    id=f"history-agentic-{result.get('trace_id') or record.id}",
                    role="assistant",
                    kind="markdown",
                    content=agentic_markdown,
                )
            )
        return result, history_messages

    def _append_missing_history_messages(
        self,
        messages: list[ChatMessageRecord],
        history_messages: list[ChatMessageRecord],
    ) -> list[ChatMessageRecord]:
        if not history_messages:
            return messages

        has_knowledge = any(
            message.kind == "markdown" and message.content.startswith(KNOWLEDGE_MESSAGE_PREFIX)
            for message in messages
        )
        has_agentic = any(
            message.kind == "markdown" and message.content.startswith(AGENTIC_MESSAGE_PREFIX)
            for message in messages
        )

        next_messages = list(messages)
        for message in history_messages:
            if message.content.startswith(KNOWLEDGE_MESSAGE_PREFIX) and has_knowledge:
                continue
            if message.content.startswith(AGENTIC_MESSAGE_PREFIX) and has_agentic:
                continue
            next_messages.append(message)
        return self._normalize_history_message_order(next_messages)

    def _normalize_history_message_order(
        self,
        messages: list[ChatMessageRecord],
    ) -> list[ChatMessageRecord]:
        knowledge_messages = [
            message
            for message in messages
            if message.kind == "markdown" and message.content.startswith(KNOWLEDGE_MESSAGE_PREFIX)
        ]
        agentic_messages = [
            message
            for message in messages
            if message.kind == "markdown" and message.content.startswith(AGENTIC_MESSAGE_PREFIX)
        ]
        if not knowledge_messages and not agentic_messages:
            return messages

        first_relevant_index = next(
            (
                index
                for index, message in enumerate(messages)
                if message.kind == "markdown"
                and (
                    message.content.startswith(KNOWLEDGE_MESSAGE_PREFIX)
                    or message.content.startswith(AGENTIC_MESSAGE_PREFIX)
                )
            ),
            -1,
        )
        if first_relevant_index < 0:
            return messages

        leading = messages[:first_relevant_index]
        trailing = [
            message
            for message in messages[first_relevant_index:]
            if not (
                message.kind == "markdown"
                and (
                    message.content.startswith(KNOWLEDGE_MESSAGE_PREFIX)
                    or message.content.startswith(AGENTIC_MESSAGE_PREFIX)
                )
            )
        ]
        return [*leading, *knowledge_messages, *agentic_messages, *trailing]

    def _retrieve_initial_snippets(
        self,
        record: ChatSessionRecord,
    ) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        accident_data = self._load_accident_data_for_history(record)
        if not accident_data:
            return [], {}

        try:
            retriever = self._build_retriever_for_history()
            snippets = retriever.retrieve(
                accident_data=accident_data,
                top_k=self.settings.retrieval.top_k,
            )
            return self._coerce_snippet_list(snippets), dict(getattr(retriever, "metadata", {}))
        except Exception:
            return [], {}

    def _load_accident_data_for_history(self, record: ChatSessionRecord) -> dict[str, Any]:
        candidates = [
            record.draft_json,
            (record.draft_meta or {}).get("generated_input"),
        ]
        report_result = record.report_result or {}
        output_dir = report_result.get("output_dir")
        if output_dir:
            candidates.append(self._read_json_if_exists(Path(output_dir) / "input_validated.json"))

        for candidate in candidates:
            payload = self._parse_json_object(candidate)
            if payload:
                return payload
        return {}

    def _build_retriever_for_history(self):
        if self._history_retriever_ready:
            return self._history_retriever
        try:
            self._history_retriever = build_retriever(self.settings)
        except Exception:
            self._history_retriever = MockRetriever(min_score=self.settings.retrieval.min_score, degraded=True)
        self._history_retriever_ready = True
        return self._history_retriever

    def _parse_json_object(self, payload: Any) -> dict[str, Any]:
        if isinstance(payload, dict):
            return payload
        if isinstance(payload, str):
            stripped = payload.strip()
            if not stripped:
                return {}
            try:
                loaded = json.loads(stripped)
            except json.JSONDecodeError:
                return {}
            if isinstance(loaded, dict):
                return loaded
        return {}

    def _coerce_snippet_list(self, payload: Any) -> list[dict[str, Any]]:
        if not isinstance(payload, list):
            return []
        return [item for item in payload if isinstance(item, dict)]

    def _coerce_round_list(self, payload: Any) -> list[dict[str, Any]]:
        if not isinstance(payload, list):
            return []
        return [item for item in payload if isinstance(item, dict)]

    def _merge_snippet_lists(self, *snippet_lists: list[dict[str, Any]]) -> list[dict[str, Any]]:
        merged: list[dict[str, Any]] = []
        seen: set[str] = set()
        for items in snippet_lists:
            for snippet in items:
                key = str(snippet.get("id") or snippet.get("citation") or snippet.get("title") or "").strip()
                if key and key in seen:
                    continue
                if key:
                    seen.add(key)
                merged.append(snippet)
        return merged

    def _format_knowledge_snippets_markdown(
        self,
        snippets: list[dict[str, Any]],
        retrieval_meta: dict[str, Any],
    ) -> str:
        if not snippets:
            return ""
        initial_query = str(retrieval_meta.get("initial_query") or retrieval_meta.get("last_query") or "").strip()
        lines = [KNOWLEDGE_MESSAGE_PREFIX]
        if initial_query:
            lines.append(f"- 检索主题：{self._truncate_text(initial_query, 90)}")
        for index, snippet in enumerate(snippets[:4], start=1):
            title = str(snippet.get("title") or snippet.get("id") or f"片段 {index}")
            excerpt = self._truncate_text(str(snippet.get("content") or "无正文片段"), 160)
            score = snippet.get("score")
            score_text = f"，匹配分 {float(score):.3f}" if isinstance(score, (int, float)) else ""
            lines.append(f"- **{title}**{score_text}")
            lines.append(f"  {excerpt}")
            meta_parts = []
            citation = str(snippet.get("citation") or "").strip()
            category = str(snippet.get("category") or "").strip()
            if citation:
                meta_parts.append(f"引用：`{citation}`")
            if category:
                meta_parts.append(f"类别：{category}")
            if meta_parts:
                lines.append(f"  {'，'.join(meta_parts)}")
        return "\n".join(lines)

    def _format_agentic_rounds_markdown(self, rounds: list[dict[str, Any]]) -> str:
        if not rounds:
            return ""
        lines = [AGENTIC_MESSAGE_PREFIX]
        for round_item in rounds[:3]:
            round_number = round_item.get("round") or "?"
            query = str(round_item.get("query") or "未记录检索语句").strip()
            reason = str(round_item.get("reason") or "").strip()
            returned_count = round_item.get("returned_count") or 0
            lines.append(f"- **第 {round_number} 轮补充检索**：{query}")
            if reason:
                lines.append(f"  触发原因：{self._truncate_text(reason, 96)}")
            lines.append(f"  返回片段：{returned_count} 条")
            snippets = self._coerce_snippet_list(round_item.get("snippets"))
            for index, snippet in enumerate(snippets[:2], start=1):
                title = str(snippet.get("title") or snippet.get("id") or f"片段 {index}")
                excerpt = self._truncate_text(str(snippet.get("content") or "无正文片段"), 140)
                score = snippet.get("score")
                score_text = f"（匹配分 {float(score):.3f}）" if isinstance(score, (int, float)) else ""
                lines.append(f"  - {title}{score_text}：{excerpt}")
        return "\n".join(lines)

    @staticmethod
    def _truncate_text(value: str, max_length: int) -> str:
        normalized = " ".join(str(value).split()).strip()
        if len(normalized) <= max_length:
            return normalized
        return f"{normalized[:max_length]}..."

    def _read_json_if_exists(self, path: Path) -> Any | None:
        if not path.exists():
            return None
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return None

    def _read_text_if_exists(self, path: Path) -> str | None:
        if not path.exists():
            return None
        return path.read_text(encoding="utf-8")

    def _sync_draft_artifacts(self, record: ChatSessionRecord) -> ChatSessionRecord:
        draft_payload = self._parse_json_object(record.draft_json)
        if not draft_payload:
            return record

        updates: dict[str, Any] = {}
        draft_meta = dict(record.draft_meta or {})
        if draft_meta.get("generated_input") != draft_payload:
            draft_meta["generated_input"] = draft_payload
            updates["draft_meta"] = draft_meta

        input_path = str(draft_meta.get("input_path") or "").strip()
        if input_path:
            resolved = Path(input_path).resolve()
            # 会话草稿只允许回写会话私有运行产物，不能覆盖共享默认输入文件。
            if self._is_shared_input_path(resolved):
                pass
            elif self._is_safe_data_path(resolved):
                resolved.parent.mkdir(parents=True, exist_ok=True)
                resolved.write_text(
                    json.dumps(draft_payload, ensure_ascii=False, indent=2),
                    encoding="utf-8",
                )

        if not updates:
            return record
        return record.model_copy(update=updates)

    def _is_report_progress_message(self, message) -> bool:  # noqa: ANN001
        if message.role != "assistant" or message.kind != "progress":
            return False
        meta = message.meta or {}
        badge = str(meta.get("badge") or "").strip()
        title = str(meta.get("title") or "").strip()
        return badge == REPORT_PROGRESS_BADGE or "报告" in title

    def _mark_report_progress_completed(self, message):  # noqa: ANN001
        meta = dict(message.meta or {})
        stages = meta.get("stages") or []
        if isinstance(stages, list):
            meta["stages"] = [
                {
                    **stage,
                    "state": "done",
                }
                for stage in stages
                if isinstance(stage, dict)
            ]
        meta["badge"] = meta.get("badge") or REPORT_PROGRESS_BADGE
        meta["status"] = "success"
        return message.model_copy(
            update={
                "content": REPORT_PROGRESS_DONE_TEXT,
                "meta": meta,
            }
        )

    def _session_dir(self, session_id: str) -> Path:
        return self.settings.chat_sessions_dir_path / session_id

    def _attach_latest_unlinked_report_to_recent_session(
        self,
        sessions: list[ChatSessionRecord],
    ) -> list[ChatSessionRecord]:
        if not sessions or not self.settings.output_dir_path.exists():
            return sessions

        known_trace_ids = {
            str((session.report_result or {}).get("trace_id") or "").strip()
            for session in sessions
            if isinstance(session.report_result, dict)
        }
        known_trace_ids.discard("")

        next_sessions = list(sessions)
        session_index = {session.id: index for index, session in enumerate(next_sessions)}
        attached = False

        for entry in self._list_output_dir_metadata():
            output_dir = Path(entry["path"])
            trace_id = entry["trace_id"]
            if trace_id in known_trace_ids:
                continue

            session_id = entry["session_id"]
            if not session_id or session_id not in session_index:
                continue

            target_index = session_index[session_id]
            target_session = next_sessions[target_index]
            current_output_mtime = self._get_report_output_mtime(target_session.report_result)
            if current_output_mtime is not None and float(entry["mtime"]) <= current_output_mtime:
                continue

            recovered_report_result = self._build_report_result_from_output_dir(
                output_dir,
                existing_result=target_session.report_result,
            )
            if not recovered_report_result:
                continue

            recovered_report_result, history_messages = self._ensure_history_knowledge_content(
                record=target_session,
                report_result=recovered_report_result,
            )
            next_messages = self._append_missing_history_messages(
                list(target_session.messages),
                history_messages,
            )
            next_session = target_session.model_copy(
                update={
                    "updated_at": self._now_ms(),
                    "report_result": recovered_report_result,
                    "messages": next_messages,
                }
            )
            next_session = self._refresh_linked_views(next_session)
            if next_session == target_session:
                continue

            self._write_session(next_session)
            next_sessions[target_index] = next_session
            attached = True
            known_trace_ids.add(trace_id)

        if not attached:
            return sessions
        return next_sessions

    def _get_output_trace_id(self, output_dir: Path) -> str:
        run_log = self._read_json_if_exists(output_dir / "run_log.json")
        return str((run_log or {}).get("trace_id") or output_dir.name).strip()

    def _get_output_session_id(self, output_dir: Path) -> str:
        run_log = self._read_json_if_exists(output_dir / "run_log.json")
        return str((run_log or {}).get("session_id") or "").strip()

    def _list_output_dir_metadata(self) -> list[dict[str, Any]]:
        if not self.settings.output_dir_path.exists():
            return []

        entries: list[dict[str, Any]] = []
        for output_dir in self.settings.output_dir_path.iterdir():
            if not output_dir.is_dir():
                continue

            resolved = output_dir.resolve()
            mtime_ns = self._get_output_dir_mtime_ns(resolved)
            cache_key = str(resolved)
            cached: dict[str, Any] | None = None
            with _OUTPUT_DIR_METADATA_GUARD:
                cached = _OUTPUT_DIR_METADATA_CACHE.get(cache_key)

            if cached and cached.get("mtime_ns") == mtime_ns:
                entries.append(cached)
                continue

            run_log = self._read_json_if_exists(resolved / "run_log.json")
            payload = {
                "path": cache_key,
                "mtime": float(resolved.stat().st_mtime),
                "mtime_ns": mtime_ns,
                "trace_id": str((run_log or {}).get("trace_id") or resolved.name).strip(),
                "session_id": str((run_log or {}).get("session_id") or "").strip(),
            }
            with _OUTPUT_DIR_METADATA_GUARD:
                _OUTPUT_DIR_METADATA_CACHE[cache_key] = payload
            entries.append(payload)

        entries.sort(key=lambda item: float(item["mtime"]), reverse=True)
        return entries

    def _get_report_output_mtime(self, report_result: Any) -> float | None:
        if not isinstance(report_result, dict):
            return None
        output_dir_value = str(report_result.get("output_dir") or "").strip()
        if not output_dir_value:
            return None

        output_dir = Path(output_dir_value).resolve()
        if not output_dir.exists() or not self._is_safe_data_path(output_dir):
            return None
        return output_dir.stat().st_mtime

    @staticmethod
    def _coerce_output_dir_mtime_ns(value: Any) -> int | None:
        try:
            if value is None or value == "":
                return None
            return int(value)
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _get_output_dir_mtime_ns(output_dir: Path) -> int | None:
        try:
            return int(output_dir.stat().st_mtime_ns)
        except OSError:
            return None

    @staticmethod
    def _session_sort_key(record: ChatSessionRecord) -> tuple[int, int, int, str]:
        if record.sort_order is not None:
            return (0, int(record.sort_order), 0, record.id)
        return (1, 0, -int(record.created_at), record.id)

    def _session_file(self, session_id: str) -> Path:
        return self._session_dir(session_id) / "session.json"

    def _is_safe_data_path(self, path: Path) -> bool:
        try:
            return path.resolve().is_relative_to(self.settings.backend_data_dir_path)
        except ValueError:
            return False

    def _is_shared_input_path(self, path: Path) -> bool:
        candidate = path.resolve()
        shared_paths = {
            self.settings.resolve_path(self.settings.input.file_path),
            self.settings.input_generation_output_file,
        }
        return candidate in shared_paths

    @staticmethod
    def _now_ms() -> int:
        import time

        return int(time.time() * 1000)
