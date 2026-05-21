import type {
  ChatSessionApiRecord,
  ChatSessionLinkedArtifact,
  ChatSessionUpsertPayload,
  GenerateInputFromUploadResponse,
  GenerateReportResponse,
  LinkedArtifactDetailResponse,
  PdfExportOptions,
  PublicAppConfig,
  ReportModelLabel,
  ReportExportFormat,
} from "./types";

const API_BASE = import.meta.env.VITE_API_BASE || "";

export interface ApiErrorPayload {
  code: string;
  message: string;
  retryable: boolean;
  trace_id: string;
  details?: Record<string, unknown> | null;
}

export class ApiError extends Error {
  status: number;
  code?: string;
  retryable: boolean;
  traceId?: string;
  details?: Record<string, unknown> | null;

  constructor(
    status: number,
    message: string,
    options: {
      code?: string;
      retryable?: boolean;
      traceId?: string;
      details?: Record<string, unknown> | null;
    } = {},
  ) {
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.code = options.code;
    this.retryable = options.retryable ?? false;
    this.traceId = options.traceId;
    this.details = options.details ?? null;
  }
}

export interface ReportStreamEvent {
  event: string;
  [key: string]: unknown;
}

export interface DownloadReportExportResult {
  blob: Blob;
  fileName: string;
}

export interface GenerateInputUploadRequest {
  files: File[];
  uploadManifest: {
    groups: Array<{
      category_id: string;
      category_label: string;
      category_subtitle?: string;
      sequence: number;
    }>;
    items: Array<{
      category_id: string;
      original_name: string;
      media_type: "image" | "video";
      sequence: number;
      group_sequence: number;
    }>;
  };
}

async function parseJsonResponse<T>(response: Response): Promise<T> {
  if (!response.ok) {
    throw await buildApiError(response);
  }
  return response.json() as Promise<T>;
}

export async function generateInputFromUpload(file: File): Promise<GenerateInputFromUploadResponse> {
  const formData = new FormData();
  formData.append("file", file);
  formData.append(
    "upload_manifest",
    JSON.stringify({
      groups: [
        {
          category_id: "default_upload",
          category_label: "事故材料",
          sequence: 1,
        },
      ],
      items: [
        {
          category_id: "default_upload",
          original_name: file.name,
          media_type: file.type.startsWith("video/") ? "video" : "image",
          sequence: 1,
          group_sequence: 1,
        },
      ],
    }),
  );

  const response = await fetch(`${API_BASE}/api/v1/inputs/generate-from-upload`, {
    method: "POST",
    body: formData,
  });
  return parseJsonResponse<GenerateInputFromUploadResponse>(response);
}

export async function generateInputFromUploads(
  payload: GenerateInputUploadRequest,
): Promise<GenerateInputFromUploadResponse> {
  const formData = new FormData();
  for (const file of payload.files) {
    formData.append("files", file);
  }
  formData.append("upload_manifest", JSON.stringify(payload.uploadManifest));

  const response = await fetch(`${API_BASE}/api/v1/inputs/generate-from-upload`, {
    method: "POST",
    body: formData,
  });
  return parseJsonResponse<GenerateInputFromUploadResponse>(response);
}

export async function generateReportFromConfirmedInput(
  accidentData: Record<string, unknown>,
  sessionId?: string,
): Promise<GenerateReportResponse> {
  const response = await fetch(`${API_BASE}/api/v1/reports/generate`, {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
    },
    body: JSON.stringify({
      session_id: sessionId,
      accident_data: accidentData,
      persist_accident_data: true,
      persist_generated_input: false,
    }),
  });
  return parseJsonResponse<GenerateReportResponse>(response);
}

export async function generateReportFromConfirmedInputStream(
  accidentData: Record<string, unknown>,
  handlers: {
    onEvent?: (event: ReportStreamEvent) => void;
  } = {},
  sessionId?: string,
  signal?: AbortSignal,
): Promise<GenerateReportResponse> {
  const response = await fetch(`${API_BASE}/api/v1/reports/generate/stream`, {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      "Accept": "text/event-stream",
    },
    signal,
    body: JSON.stringify({
      session_id: sessionId,
      accident_data: accidentData,
      persist_accident_data: true,
      persist_generated_input: false,
    }),
  });

  if (!response.ok) {
    throw await buildApiError(response);
  }

  const reader = response.body?.getReader();
  if (!reader) {
    throw new Error("报告流未返回可读取的数据流。");
  }

  const decoder = new TextDecoder("utf-8");
  let buffer = "";
  let finalPayload: GenerateReportResponse | null = null;

  while (true) {
    const { value, done } = await reader.read();
    if (done) {
      break;
    }
    buffer += decoder.decode(value, { stream: true });
    const chunks = buffer.split("\n\n");
    buffer = chunks.pop() ?? "";

    for (const chunk of chunks) {
      const parsed = parseSseChunk(chunk);
      if (!parsed) {
        continue;
      }
      handlers.onEvent?.(parsed);
      if (parsed.event === "error") {
        throw createApiErrorFromStreamEvent(parsed);
      }
      if (parsed.event === "final") {
        finalPayload = parsed.payload as GenerateReportResponse;
      }
    }
  }

  if (!finalPayload) {
    throw new Error("报告流提前结束，未收到最终结果。");
  }
  return finalPayload;
}

export async function downloadReportExport(
  traceId: string,
  exportFormat: ReportExportFormat,
  options?: PdfExportOptions,
): Promise<DownloadReportExportResult> {
  const query = new URLSearchParams();
  if (exportFormat === "pdf" && options) {
    if (options.coverTitle?.trim()) {
      query.set("cover_title", options.coverTitle.trim());
    }
    if (options.coverSubtitle?.trim()) {
      query.set("cover_subtitle", options.coverSubtitle.trim());
    }
    if (options.coverCompiledBy?.trim()) {
      query.set("cover_compiled_by", options.coverCompiledBy.trim());
    }
    if (options.coverDateMode) {
      query.set("cover_date_mode", options.coverDateMode);
    }
    if (options.coverDateText?.trim()) {
      query.set("cover_date_text", options.coverDateText.trim());
    }
  }

  const response = await fetch(`${API_BASE}/api/v1/reports/${encodeURIComponent(traceId)}/exports/${exportFormat}${query.size ? `?${query.toString()}` : ""}`, {
    method: "GET",
  });

  if (!response.ok) {
    throw await buildApiError(response);
  }

  return {
    blob: await response.blob(),
    fileName: parseDownloadFileName(
      response.headers.get("content-disposition"),
      `traffic-accident-report-${traceId}.${exportFormat}`,
    ),
  };
}

export async function listChatSessions(): Promise<ChatSessionApiRecord[]> {
  const response = await fetch(`${API_BASE}/api/v1/chat-sessions`, {
    method: "GET",
  });
  return parseJsonResponse<ChatSessionApiRecord[]>(response);
}

export async function fetchChatSession(sessionId: string): Promise<ChatSessionApiRecord> {
  const response = await fetch(`${API_BASE}/api/v1/chat-sessions/${sessionId}`, {
    method: "GET",
  });
  return parseJsonResponse<ChatSessionApiRecord>(response);
}

export async function listChatSessionLinkedArtifacts(sessionId: string): Promise<ChatSessionLinkedArtifact[]> {
  const response = await fetch(`${API_BASE}/api/v1/chat-sessions/${sessionId}/linked-artifacts`, {
    method: "GET",
  });
  return parseJsonResponse<ChatSessionLinkedArtifact[]>(response);
}

export async function fetchChatSessionLinkedArtifactDetail(
  sessionId: string,
  category: string,
): Promise<LinkedArtifactDetailResponse> {
  const response = await fetch(`${API_BASE}/api/v1/chat-sessions/${sessionId}/linked-artifacts/${encodeURIComponent(category)}`, {
    method: "GET",
  });
  return parseJsonResponse<LinkedArtifactDetailResponse>(response);
}

export function buildChatSessionLinkedArtifactAssetUrl(
  sessionId: string,
  category: string,
  assetId: string,
): string {
  return `${API_BASE}/api/v1/chat-sessions/${sessionId}/linked-artifacts/${encodeURIComponent(category)}/assets/${encodeURIComponent(assetId)}`;
}

export async function fetchPublicAppConfig(): Promise<PublicAppConfig> {
  const response = await fetch(`${API_BASE}/api/v1/app-config`, {
    method: "GET",
  });
  return parseJsonResponse<PublicAppConfig>(response);
}

export async function updateReportModelSelection(
  label: ReportModelLabel,
): Promise<PublicAppConfig["report_model"]> {
  const response = await fetch(`${API_BASE}/api/v1/app-config/report-model`, {
    method: "PUT",
    headers: {
      "Content-Type": "application/json",
    },
    body: JSON.stringify({ label }),
  });
  return parseJsonResponse<PublicAppConfig["report_model"]>(response);
}

export async function createChatSession(
  payload: ChatSessionUpsertPayload,
  options: { keepalive?: boolean } = {},
): Promise<ChatSessionApiRecord> {
  const response = await fetch(`${API_BASE}/api/v1/chat-sessions`, {
    method: "POST",
    keepalive: options.keepalive,
    headers: {
      "Content-Type": "application/json",
    },
    body: JSON.stringify(payload),
  });
  return parseJsonResponse<ChatSessionApiRecord>(response);
}

export async function updateChatSession(
  sessionId: string,
  payload: ChatSessionUpsertPayload,
  options: { keepalive?: boolean } = {},
): Promise<ChatSessionApiRecord> {
  const response = await fetch(`${API_BASE}/api/v1/chat-sessions/${sessionId}`, {
    method: "PUT",
    keepalive: options.keepalive,
    headers: {
      "Content-Type": "application/json",
    },
    body: JSON.stringify(payload),
  });
  return parseJsonResponse<ChatSessionApiRecord>(response);
}

export async function deleteChatSession(sessionId: string): Promise<void> {
  const response = await fetch(`${API_BASE}/api/v1/chat-sessions/${sessionId}`, {
    method: "DELETE",
  });
  if (!response.ok) {
    await parseJsonResponse<{ status: string }>(response);
  }
}

function parseSseChunk(chunk: string): ReportStreamEvent | null {
  const lines = chunk.split(/\r?\n/);
  let eventName = "message";
  const dataLines: string[] = [];

  for (const line of lines) {
    if (!line.trim()) {
      continue;
    }
    if (line.startsWith("event:")) {
      eventName = line.slice("event:".length).trim() || "message";
      continue;
    }
    if (line.startsWith("data:")) {
      dataLines.push(line.slice("data:".length).trimStart());
    }
  }

  if (!dataLines.length) {
    return null;
  }

  const payload = JSON.parse(dataLines.join("\n")) as ReportStreamEvent;
  if (!payload.event) {
    payload.event = eventName;
  }
  return payload;
}

export function sendChatSessionSnapshot(payload: ChatSessionUpsertPayload): boolean {
  const serializedPayload = JSON.stringify(payload);
  const targetUrl = `${API_BASE}/api/v1/chat-sessions`;

  if (typeof navigator !== "undefined" && typeof navigator.sendBeacon === "function") {
    try {
      const requestBody = new Blob([serializedPayload], { type: "application/json" });
      if (navigator.sendBeacon(targetUrl, requestBody)) {
        return true;
      }
    } catch (error) {
      console.warn("sendBeacon 同步会话快照失败，将回退到 keepalive fetch。", error);
    }
  }

  void fetch(targetUrl, {
    method: "POST",
    keepalive: true,
    headers: {
      "Content-Type": "application/json",
    },
    body: serializedPayload,
  }).catch((error) => {
    console.warn("keepalive 会话快照同步失败。", error);
  });
  return false;
}

async function buildApiError(response: Response): Promise<ApiError> {
  const traceId = response.headers.get("X-Trace-Id") || undefined;
  try {
    const errorPayload = await response.json();
    return createApiErrorFromPayload(response.status, errorPayload, traceId);
  } catch {
    return new ApiError(response.status, `请求失败：${response.status}`, {
      retryable: response.status >= 500,
      traceId,
    });
  }
}

export function createApiErrorFromStreamEvent(event: ReportStreamEvent): ApiError {
  const normalized = parseApiErrorPayload(event);
  if (normalized) {
    return new ApiError(500, normalized.message, {
      code: normalized.code,
      retryable: normalized.retryable,
      traceId: normalized.trace_id,
      details: normalized.details ?? null,
    });
  }

  return new ApiError(500, String(event.message || "报告流处理失败。"));
}

export function createApiErrorFromPayload(
  status: number,
  payload: unknown,
  headerTraceId?: string,
): ApiError {
  let message = `请求失败：${status}`;
  let code: string | undefined;
  let retryable = status >= 500;
  let traceId = headerTraceId;
  let details: Record<string, unknown> | null | undefined;
  const normalized = parseApiErrorPayload(payload);
  if (normalized) {
    message = normalized.message;
    code = normalized.code;
    retryable = normalized.retryable;
    traceId = normalized.trace_id || traceId;
    details = normalized.details ?? null;
  } else if (payload && typeof payload === "object" && typeof (payload as { detail?: unknown }).detail === "string") {
    message = String((payload as { detail: string }).detail);
  }
  return new ApiError(status, message, {
    code,
    retryable,
    traceId,
    details,
  });
}

export function parseApiErrorPayload(payload: unknown): ApiErrorPayload | null {
  const direct = coerceApiErrorPayload(payload);
  if (direct) {
    return direct;
  }
  if (payload && typeof payload === "object" && "error" in payload) {
    return coerceApiErrorPayload((payload as { error?: unknown }).error);
  }
  return null;
}

function coerceApiErrorPayload(payload: unknown): ApiErrorPayload | null {
  if (!payload || typeof payload !== "object") {
    return null;
  }
  const candidate = payload as Record<string, unknown>;
  if (
    typeof candidate.code !== "string"
    || typeof candidate.message !== "string"
    || typeof candidate.retryable !== "boolean"
    || typeof candidate.trace_id !== "string"
  ) {
    return null;
  }
  return {
    code: candidate.code,
    message: candidate.message,
    retryable: candidate.retryable,
    trace_id: candidate.trace_id,
    details: typeof candidate.details === "object" && candidate.details !== null
      ? candidate.details as Record<string, unknown>
      : null,
  };
}

export function formatApiErrorMessage(error: unknown, fallbackMessage: string): string {
  let message = error instanceof Error ? error.message : fallbackMessage;
  if (error instanceof ApiError && error.retryable && !/重试|稍后/.test(message)) {
    message = `${message} 可稍后重试。`;
  }
  if (error instanceof ApiError && error.traceId) {
    return `${message}\n错误追踪号：${error.traceId}`;
  }
  return message;
}

function parseDownloadFileName(contentDisposition: string | null, fallback: string): string {
  if (!contentDisposition) {
    return fallback;
  }

  const encodedMatch = contentDisposition.match(/filename\*=UTF-8''([^;]+)/i);
  if (encodedMatch?.[1]) {
    try {
      return decodeURIComponent(encodedMatch[1]);
    } catch {
      return encodedMatch[1];
    }
  }

  const plainMatch = contentDisposition.match(/filename="([^"]+)"/i) || contentDisposition.match(/filename=([^;]+)/i);
  if (plainMatch?.[1]) {
    return plainMatch[1].trim();
  }

  return fallback;
}
