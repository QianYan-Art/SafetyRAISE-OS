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
    current_label: ReportModelLabel | null;
    updated_at?: string | null;
    options: PublicReportModelOption[];
  };
}

export type ReportModelLabel = "max" | "pro" | "lite";
export type UserRole = "admin" | "user";

export interface PublicReportModelOption {
  label: ReportModelLabel;
  active: boolean;
  display_name?: string | null;
}

export interface UserSummary {
  id: string;
  username: string;
  display_name?: string | null;
  role: UserRole;
  is_active: boolean;
  created_at: string;
  updated_at: string;
}

export interface AuthTokenResponse {
  access_token: string;
  token_type: "bearer";
  user: UserSummary;
}

export interface UserModelConfigRecord {
  label: ReportModelLabel;
  display_name: string;
  base_url: string;
  api_key: string;
  configured: boolean;
  provider_name: string;
  updated_at?: string | null;
}

export interface UserModelConfigState {
  current_label: ReportModelLabel | null;
  updated_at?: string | null;
  options: UserModelConfigRecord[];
}

export interface UpdateUserModelConfigItem {
  label: ReportModelLabel;
  display_name?: string | null;
  base_url?: string | null;
  api_key?: string | null;
}

export type ModelCapability = "vision" | "embedding" | "report";

export interface EmbeddingTuningParams {
  top_k?: number | null;
  dense_top_k_chunks?: number | null;
  dense_top_k_rules?: number | null;
}

export interface CapabilityConfigRecord {
  capability: ModelCapability;
  configured: boolean;
  base_url?: string | null;
  model_name?: string | null;
  api_key_masked?: string | null;
  params: EmbeddingTuningParams;
}

export interface CapabilityConfigState {
  role: string;
  capabilities: CapabilityConfigRecord[];
  system_defaults: Record<string, EmbeddingTuningParams>;
}

export interface UpdateCapabilityConfigItem {
  capability: ModelCapability;
  base_url?: string | null;
  model_name?: string | null;
  api_key?: string | null;
  params?: EmbeddingTuningParams | null;
}

export interface UpdateCapabilityConfigsPayload {
  items: UpdateCapabilityConfigItem[];
}

export interface UpdateUserModelConfigsPayload {
  items: UpdateUserModelConfigItem[];
  active_label?: ReportModelLabel | null;
}

export interface AdminUserRecord {
  id: string;
  username: string;
  display_name?: string | null;
  role: UserRole;
  is_active: boolean;
  created_at: string;
  updated_at: string;
}

export interface AdminCreateUserPayload {
  username: string;
  password: string;
  display_name?: string | null;
  role?: UserRole;
  is_active?: boolean;
}

export interface AdminUpdateUserPayload {
  display_name?: string | null;
  password?: string | null;
  role?: UserRole;
  is_active?: boolean;
}

export interface AdminSpaceRecord {
  session_id: string;
  owner_user_id?: string | null;
  owner_username?: string | null;
  title: string;
  created_at: number;
  updated_at: number;
  session_state: string;
  source_type?: string | null;
  source_name?: string | null;
  message_count: number;
  linked_artifact_count: number;
  redacted: boolean;
}

export interface AdminUpdateSpacePayload {
  title?: string | null;
  owner_user_id?: string | null;
  sort_order?: number | null;
}

export interface AdminCleanupSpacesResponse {
  status: string;
  deleted_count: number;
}
