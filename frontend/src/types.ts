export type ChatRole = "assistant" | "user" | "system";
export type ChatMessageKind = "text" | "markdown" | "json" | "progress";

export interface ChatProgressStage {
  label: string;
  state: "pending" | "running" | "done";
}

export interface ChatMessageMeta {
  badge?: string;
  title?: string;
  status?: "running" | "success" | "error";
  stages?: ChatProgressStage[];
}

export interface ChatMessage {
  id: string;
  role: ChatRole;
  kind: ChatMessageKind;
  content: string;
  meta?: ChatMessageMeta | null;
}

export interface UploadGroupSummary {
  category_id: string;
  category_label: string;
  category_subtitle?: string | null;
  sequence: number;
  image_count: number;
  video_count: number;
  total_bytes: number;
  files: Array<{
    original_name: string;
    stored_name: string;
    media_type: "image" | "video";
    size_bytes: number;
    sequence: number;
    global_sequence?: number;
    path: string;
  }>;
}

export interface FrameManifestItem {
  frame: number;
  timestamp_seconds: number;
  reason: string;
  path: string;
  source_name?: string;
  media_type?: "image" | "video";
  category_id?: string;
  category_label?: string;
  category_subtitle?: string;
  category_sequence?: number;
  sequence?: number;
  group_sequence?: number;
  event_score?: number;
}

export interface YoloSummaryPreviewVideo {
  source_name: string;
  category_id: string;
  category_label: string;
  category_subtitle: string;
  category_sequence?: number;
  sequence: number;
  duration_seconds: number;
  frame_count: number;
  fps: number;
  unique_track_count: number;
  total_detections: number;
  class_counts: Record<string, number>;
  track_highlights: Array<{
    track_id: number;
    class_name: string;
    mean_speed_px_s: number;
    max_speed_px_s: number;
    mean_abs_acceleration_px_s2: number;
    max_abs_acceleration_px_s2: number;
    path_length_px: number;
  }>;
  event_highlights: Array<{
    frame: number;
    timestamp_seconds: number;
    event_score: number;
    reason: string;
    object_count: number;
  }>;
}

export interface GenerateInputFromUploadResponse {
  status: string;
  media_type: "image" | "video" | "mixed";
  file_name?: string | null;
  file_names: string[];
  source_count: number;
  input_path: string;
  generated_input: Record<string, string>;
  backup_path?: string | null;
  workspace_dir: string;
  yolo_summary_path?: string | null;
  yolo_summary_preview?: {
    source_type: "video" | "mixed";
    image_source_count: number;
    video_source_count: number;
    videos: YoloSummaryPreviewVideo[];
  } | null;
  frames_dir?: string | null;
  frame_manifest: FrameManifestItem[];
  upload_groups: UploadGroupSummary[];
  raw_response_path?: string | null;
}

export interface GenerateReportResponse {
  trace_id: string;
  status: string;
  output_dir: string;
  guidance: Record<string, unknown>;
  report: {
    report_markdown: string;
    sections: Array<{ title: string; content: string }>;
    citations: string[];
    meta: Record<string, unknown>;
  };
  initial_knowledge_snippets: Array<{
    id?: string;
    title?: string;
    content?: string;
    source?: string;
    score?: number;
    record_type?: string;
    citation?: string;
    url?: string;
    category?: string;
    authority?: string;
  }>;
  knowledge_snippets: Array<{
    id?: string;
    title?: string;
    content?: string;
    source?: string;
    score?: number;
    record_type?: string;
    citation?: string;
    url?: string;
    category?: string;
    authority?: string;
  }>;
  retrieval_meta: Record<string, unknown>;
  agentic_retrieval_rounds: Array<{
    round: number;
    query: string;
    reason: string;
    requested_top_k: number;
    returned_count: number;
    snippets: Array<{
      id?: string;
      title?: string;
      content?: string;
      score?: number;
      source?: string;
      citation?: string;
      category?: string;
      authority?: string;
    }>;
  }>;
  input_generation?: Record<string, unknown> | null;
}

export type ReportExportFormat = "md" | "docx" | "pdf";
export type PdfCoverDateMode = "today" | "custom" | "hide";

export interface PdfExportOptions {
  coverTitle?: string;
  coverSubtitle?: string;
  coverCompiledBy?: string;
  coverDateMode?: PdfCoverDateMode;
  coverDateText?: string;
}

export interface ChatSessionLinkedFile {
  label: string;
  path: string;
  category: string;
  path_type: "file" | "dir";
  exists: boolean;
}

export interface ChatSessionLinkedArtifact {
  label: string;
  category: string;
  kind: string;
  item_count: number;
  summary: string;
}

export interface LinkedArtifactAsset {
  asset_id: string;
  kind: string;
  media_type: string;
  file_name: string;
  path: string;
  mime_type?: string | null;
  category_id?: string | null;
  category_label?: string | null;
  source_name?: string | null;
  reason?: string | null;
  sequence?: number | null;
  timestamp_seconds?: number | null;
  annotation_label?: string | null;
}

export interface LinkedArtifactDetailResponse {
  category: string;
  label: string;
  kind: string;
  summary: string;
  meta: Record<string, unknown>;
  content: Array<Record<string, unknown>>;
  assets: LinkedArtifactAsset[];
}

export interface ChatSessionApiRecord {
  id: string;
  title: string;
  created_at: number;
  updated_at: number;
  session_state?: "draft" | "input_ready" | "report_running" | "report_ready" | "export_ready" | "cancelled" | "failed";
  sort_order?: number | null;
  source_type?: "image" | "video" | "mixed" | null;
  source_name?: string | null;
  messages: ChatMessage[];
  draft_json: string;
  draft_meta?: GenerateInputFromUploadResponse | null;
  report_result?: GenerateReportResponse | null;
  linked_files: ChatSessionLinkedFile[];
  linked_artifacts: ChatSessionLinkedArtifact[];
}

export interface ChatSessionUpsertPayload {
  id?: string;
  title: string;
  created_at?: number;
  updated_at?: number;
  sort_order?: number | null;
  source_type?: "image" | "video" | "mixed" | null;
  source_name?: string | null;
  messages: ChatMessage[];
  draft_json: string;
  draft_meta?: GenerateInputFromUploadResponse | null;
  report_result?: GenerateReportResponse | null;
}

export interface PublicAppConfig {
  upload_limits: {
    max_total_bytes: number;
    max_image_bytes: number;
    max_video_bytes: number;
    max_model_images: number;
    max_images_per_group: number;
    max_videos_per_group: number;
    max_total_images: number;
    max_total_videos: number;
  };
  report_model: {
    current_label: ReportModelLabel;
    updated_at?: string | null;
    options: PublicReportModelOption[];
  };
}

export type ReportModelLabel = "max" | "pro" | "lite";

export interface PublicReportModelOption {
  label: ReportModelLabel;
  active: boolean;
}
