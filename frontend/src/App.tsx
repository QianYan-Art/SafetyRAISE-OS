import { ChangeEvent, DragEvent, KeyboardEvent as ReactKeyboardEvent, MouseEvent, TouchEvent, useEffect, useRef, useState } from "react";
import ReactMarkdown, { type Components } from "react-markdown";
import remarkGfm from "remark-gfm";

import {
  ApiError,
  buildChatSessionLinkedArtifactAssetUrl,
  clearAuthToken,
  hasAuthToken,
  fetchCurrentUser,
  fetchChatSessionLinkedArtifactDetail,
  downloadReportExport,
  fetchPublicAppConfig,
  fetchUserModelConfigs,
  formatApiErrorMessage,
  generateInputFromUploads,
  generateReportFromConfirmedInputStream,
  login,
  listChatSessionLinkedArtifacts,
  register,
  updateUserModelConfigs,
} from "./api";
import { AdminConsole } from "./AdminConsole";
import { AuthScreen } from "./AuthScreen";
import type {
  ChatMessage,
  ChatMessageMeta,
  ChatSessionLinkedArtifact,
  GenerateReportResponse,
  LinkedArtifactDetailResponse,
  PdfCoverDateMode,
  PdfExportOptions,
  PublicAppConfig,
  PublicReportModelOption,
  ReportModelLabel,
  ReportExportFormat,
  CapabilityConfigState,
  UpdateCapabilityConfigsPayload,
  UserSummary,
} from "./types";
import { useChatHistory, ChatSession } from "./useChatHistory";
import { JsonTableEditor } from "./JsonTableEditor";
import { normalizeMarkdownForDisplay } from "./markdown";
import { REPORT_PROGRESS_LABELS, buildProgressStages, createMessage } from "./chatProgress";
import {
  formatAgenticRoundsMarkdown,
  formatKnowledgeSnippetsMarkdown,
  formatYoloPreviewMarkdown,
  getInputProgressLabels,
} from "./chatInsights";
import {
  appendPendingFiles,
  buildGroupedUploadPayload,
  createInitialPendingUploadGroups,
  getPendingUploadStats,
  hasPendingUploads,
  removePendingUploadItem,
  type PendingUploadGroupState,
  validatePendingUploadSelection,
} from "./uploadGroups";
import { UserModelConfigDrawer } from "./UserModelConfigDrawer";

const SidebarIcon = ({ isOpen }: { isOpen?: boolean }) => (
  <svg xmlns="http://www.w3.org/2000/svg" width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round">
    <rect x="3" y="4" width="18" height="16" rx="2" ry="2" />
    {isOpen ? (
      <>
        <path d="M15 4v16" />
        <path d="M8 9l3 3-3 3" />
      </>
    ) : (
      <>
        <path d="M9 4v16" />
        <path d="M14 9l3 3-3 3" />
      </>
    )}
  </svg>
);

const ChatPlusIcon = () => (
  <svg xmlns="http://www.w3.org/2000/svg" width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round">
    <path d="M15 4H6a2 2 0 0 0-2 2v12a2 2 0 0 0 2 2h2l4 4v-4h8a2 2 0 0 0 2-2v-5" />
    <path d="M8 10h6" />
    <path d="M8 14h4" />
    <path d="M19 4v6" />
    <path d="M16 7h6" />
  </svg>
);

const SearchIcon = () => (
  <svg xmlns="http://www.w3.org/2000/svg" width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round">
    <circle cx="11" cy="11" r="8" />
    <line x1="21" y1="21" x2="16.65" y2="16.65" />
  </svg>
);

const ShieldIcon = () => (
  <svg xmlns="http://www.w3.org/2000/svg" width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.3" strokeLinecap="round" strokeLinejoin="round">
    <path d="M12 3l7 3v5c0 4.8-2.9 8.9-7 10-4.1-1.1-7-5.2-7-10V6l7-3z" />
    <path d="m9.5 12 1.7 1.7 3.8-4.2" />
  </svg>
);

const ArrowLeftIcon = () => (
  <svg xmlns="http://www.w3.org/2000/svg" width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.4" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
    <path d="M19 12H5" />
    <path d="m12 19-7-7 7-7" />
  </svg>
);

const DragHandleIcon = () => (
  <svg xmlns="http://www.w3.org/2000/svg" width="16" height="16" viewBox="0 0 16 16" fill="currentColor" aria-hidden="true">
    <circle cx="5" cy="4" r="1.2" />
    <circle cx="5" cy="8" r="1.2" />
    <circle cx="5" cy="12" r="1.2" />
    <circle cx="11" cy="4" r="1.2" />
    <circle cx="11" cy="8" r="1.2" />
    <circle cx="11" cy="12" r="1.2" />
  </svg>
);

const SunIcon = () => (
  <svg xmlns="http://www.w3.org/2000/svg" width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
    <circle cx="12" cy="12" r="4" />
    <path d="M12 2v2.5" />
    <path d="M12 19.5V22" />
    <path d="M4.93 4.93l1.77 1.77" />
    <path d="M17.3 17.3l1.77 1.77" />
    <path d="M2 12h2.5" />
    <path d="M19.5 12H22" />
    <path d="M4.93 19.07l1.77-1.77" />
    <path d="M17.3 6.7l1.77-1.77" />
  </svg>
);

const MoonIcon = () => (
  <svg xmlns="http://www.w3.org/2000/svg" width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
    <path d="M21 12.8A9 9 0 1 1 11.2 3 7 7 0 0 0 21 12.8z" />
  </svg>
);

const MenuIcon = () => (
  <svg xmlns="http://www.w3.org/2000/svg" width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round">
    <line x1="3" y1="12" x2="21" y2="12"></line>
    <line x1="3" y1="6" x2="21" y2="6"></line>
    <line x1="3" y1="18" x2="21" y2="18"></line>
  </svg>
);

const MoreVertIcon = () => (
  <svg xmlns="http://www.w3.org/2000/svg" width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round">
    <circle cx="12" cy="5" r="2.5" />
    <circle cx="12" cy="12" r="2.5" />
    <circle cx="12" cy="19" r="2.5" />
  </svg>
);

const ExportRibbonIcon = () => (
  <svg xmlns="http://www.w3.org/2000/svg" width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
    <path d="M14 3H7a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2V8z" />
    <path d="M14 3v5h5" />
    <path d="M8.5 13h7" />
    <path d="M8.5 17h4.5" />
  </svg>
);

function prettyJson(payload: unknown): string {
  return JSON.stringify(payload, null, 2);
}

function formatCurrentTime() {
  const now = new Date();
  return `${now.getFullYear()}-${(now.getMonth() + 1).toString().padStart(2, '0')}-${now.getDate().toString().padStart(2, '0')} ${now.getHours().toString().padStart(2, '0')}:${now.getMinutes().toString().padStart(2, '0')}`;
}

const PROGRESS_STAGE_INTERVAL_MS = 1600;
const MOBILE_SESSION_RENAME_PRESS_MS = 520;
const MOBILE_SESSION_RENAME_MOVE_TOLERANCE = 10;
const THEME_STORAGE_KEY = "traffic-accident-theme-mode";
const REPORT_MODEL_ORDER: ReportModelLabel[] = ["max", "pro", "lite"];
const DEFAULT_PUBLIC_APP_CONFIG: PublicAppConfig = {
  upload_limits: {
    max_total_bytes: 1024 * 1024 * 1024,
    max_image_bytes: 10 * 1024 * 1024,
    max_video_bytes: 100 * 1024 * 1024,
    max_model_images: 48,
    max_images_per_group: 20,
    max_videos_per_group: 5,
    max_total_images: 120,
    max_total_videos: 20,
  },
  report_model: {
    current_label: "pro",
    updated_at: null,
    options: REPORT_MODEL_ORDER.map((label) => ({
      label,
      active: label === "pro",
    })),
  },
};
const WELCOME_MESSAGE = "欢迎进入“交通事故分析报告生成系统”。请先上传一组事故图片或相关事故视频；\n本系统将优先生成事故信息草稿，经您确认后再生成最终分析报告。";
const INPUT_REFRESH_INTERRUPT_MESSAGE = "页面刷新或离开页面已中断本次事故信息生成，请重新开始。";
const REPORT_REFRESH_INTERRUPT_MESSAGE = "页面刷新或离开页面导致当前报告生成中断，请重新点击继续生成。";
const KNOWLEDGE_MESSAGE_PREFIX = "### 首轮知识库片段（节选）";
const AGENTIC_MESSAGE_PREFIX = "### Agentic RAG 新增片段（节选）";
const REPORT_STAGE_INDEX: Record<string, number> = {
  connect: 0,
  generate_guidance: 0,
  retrieve_knowledge: 1,
  agentic_retrieval: 2,
  generate_report: 3,
  postprocess: 3,
};
const REPORT_EXPORT_ACTIONS: Array<{
  format: ReportExportFormat;
  category: string;
  kicker: string;
  label: string;
  description: string;
  pendingHint: string;
}> = [
  {
    format: "md",
    category: "report_markdown",
    kicker: "原始主稿",
    label: "下载 Markdown",
    description: "保留原始 report.md，适合继续校对与版本对比。",
    pendingHint: "即时下载",
  },
  {
    format: "docx",
    category: "report_docx",
    kicker: "办公流转",
    label: "导出 Word",
    description: "自动清洗 Markdown 符号，并输出更适合编辑流转的版式。",
    pendingHint: "首次导出会生成排版文档",
  },
  {
    format: "pdf",
    category: "report_pdf",
    kicker: "固化归档",
    label: "导出 PDF",
    description: "先打开封面编排台，再生成适合打印、传阅与归档的固定版面文件。",
    pendingHint: "可先配置封面标题与日期",
  },
];
const EXPORT_CATEGORY_BY_FORMAT: Record<ReportExportFormat, string> = {
  md: "report_markdown",
  docx: "report_docx",
  pdf: "report_pdf",
};
type ThemeMode = "light" | "dark";
type PdfCoverDraft = {
  title: string;
  subtitle: string;
  compiledBy: string;
  dateMode: PdfCoverDateMode;
  dateText: string;
};

type ArtifactPreviewState = {
  category: string;
  detail: LinkedArtifactDetailResponse | null;
  loading: boolean;
  error: string;
};

type AppView = "workspace" | "admin";
type AdminTab = "users" | "spaces";

const PDF_COVER_TITLE = "道路交通事故分析报告";
const PDF_COVER_SUBTITLE = "事故事实梳理、责任分析与研判文书";
const PDF_COMPILED_BY = "锐鉴安途道路交通事故分析系统";
const PDF_GENERIC_TITLES = new Set(["交通事故分析报告", "道路交通事故分析报告"]);

const reportMarkdownComponents: Components = {
  table: ({ node: _node, ...props }) => (
    <div className="report-table-shell">
      <div className="report-table-scroll">
        <table {...props} />
      </div>
    </div>
  ),
};

function formatPdfCoverDate(date = new Date()) {
  return `${date.getFullYear()}年${(date.getMonth() + 1).toString().padStart(2, "0")}月${date.getDate().toString().padStart(2, "0")}日`;
}

function extractFirstMarkdownHeading(markdown: string) {
  const matched = markdown.match(/^#\s+(.+)$/m);
  return matched?.[1]?.trim() ?? "";
}

function renderBrandWatermark(className: string) {
  return (
    <span className={className} aria-hidden="true">
      <span className="watermark-line">锐鉴安途道路交</span>
      <span className="watermark-line">通事故分析系统</span>
    </span>
  );
}

function buildDefaultPdfCoverDraft(markdown: string): PdfCoverDraft {
  const firstHeading = extractFirstMarkdownHeading(markdown);
  const subtitle = firstHeading && !PDF_GENERIC_TITLES.has(firstHeading) ? firstHeading : PDF_COVER_SUBTITLE;
  return {
    title: PDF_COVER_TITLE,
    subtitle,
    compiledBy: PDF_COMPILED_BY,
    dateMode: "today",
    dateText: formatPdfCoverDate(),
  };
}

function resolvePdfCoverPreviewDate(draft: PdfCoverDraft): string | null {
  if (draft.dateMode === "hide") {
    return null;
  }
  if (draft.dateMode === "custom") {
    return draft.dateText.trim() || null;
  }
  return formatPdfCoverDate();
}

function detectBatchMediaType(files: File[]): "image" | "video" | "mixed" {
  const hasImage = files.some((file) => file.type.startsWith("image/"));
  const hasVideo = files.some((file) => file.type.startsWith("video/"));
  if (hasImage && hasVideo) return "mixed";
  return hasVideo ? "video" : "image";
}

function formatBatchMediaLabel(files: File[]): string {
  const mediaType = detectBatchMediaType(files);
  if (mediaType === "mixed") return `共 ${files.length} 个图片/视频文件`;
  if (mediaType === "video") return files.length > 1 ? `共 ${files.length} 个视频文件` : "视频文件";
  return files.length > 1 ? `共 ${files.length} 张图片` : "图片文件";
}

function formatSizeLimit(bytes: number): string {
  return `${Math.round(bytes / 1024 / 1024)}MB`;
}

function buildUploadLimitHintLines(uploadLimits: PublicAppConfig["upload_limits"]): [string, string] {
  return [
    `每个分组最多 ${uploadLimits.max_images_per_group} 张图片、${uploadLimits.max_videos_per_group} 个视频；图片单张不超过 ${formatSizeLimit(uploadLimits.max_image_bytes)}，视频单个不超过 ${formatSizeLimit(uploadLimits.max_video_bytes)}。`,
    `当前会话全部分组最多 ${uploadLimits.max_total_images} 张图片、${uploadLimits.max_total_videos} 个视频，总上传大小不超过 ${formatSizeLimit(uploadLimits.max_total_bytes)}；开始生成前可随时删除缓冲区材料。`,
  ];
}

function buildUploadDropzoneHintLines(uploadLimits: PublicAppConfig["upload_limits"]): [string, string] {
  return [
    `支持上传 JPG/PNG 图片与 MP4 视频。每个分组可多次追加，但都只会先进入本会话缓冲区。`,
    `图片单张不超过 ${formatSizeLimit(uploadLimits.max_image_bytes)}，视频单个不超过 ${formatSizeLimit(uploadLimits.max_video_bytes)}；全部分组合计不超过 ${formatSizeLimit(uploadLimits.max_total_bytes)}。`,
  ];
}

function formatUploadLimitHint(uploadLimits: PublicAppConfig["upload_limits"]): string {
  return buildUploadLimitHintLines(uploadLimits).join(" ");
}

function withUploadLimitContext(message: string, uploadLimits: PublicAppConfig["upload_limits"]): string {
  if (message.includes("当前限制：")) {
    return message;
  }
  return `${message}\n当前限制：${formatUploadLimitHint(uploadLimits)}`;
}

function resolveUiErrorMessage(error: unknown, fallbackMessage: string): string {
  return formatApiErrorMessage(error, fallbackMessage);
}

function normalizeReportModelLabel(value: unknown): ReportModelLabel | null {
  return value === "max" || value === "pro" || value === "lite" ? value : null;
}

function formatSessionCardDate(value: number | string | Date) {
  const date = value instanceof Date ? value : new Date(value);
  if (Number.isNaN(date.getTime())) {
    return "--";
  }
  return date.toLocaleString("zh-CN", {
    month: "short",
    day: "numeric",
  });
}

function normalizeReportModelOptions(
  options: PublicReportModelOption[] | null | undefined,
  currentLabel: ReportModelLabel | null,
): PublicReportModelOption[] {
  const normalized = new Map<ReportModelLabel, PublicReportModelOption>();
  for (const option of options ?? []) {
    const label = normalizeReportModelLabel(option?.label);
    if (!label) {
      continue;
    }
    normalized.set(label, {
      label,
      active: Boolean(option?.active),
      display_name: typeof option?.display_name === "string" ? option.display_name : null,
    });
  }
  return REPORT_MODEL_ORDER.map((label) => {
    const candidate = normalized.get(label);
    return {
      label,
      active: candidate ? candidate.active : label === currentLabel,
      display_name: candidate?.display_name ?? null,
    };
  });
}

function normalizeReportModelConfig(
  reportModel: Partial<PublicAppConfig["report_model"]> | null | undefined,
): PublicAppConfig["report_model"] {
  const currentLabel = normalizeReportModelLabel(reportModel?.current_label);
  return {
    current_label: currentLabel,
    updated_at: typeof reportModel?.updated_at === "string" ? reportModel.updated_at : null,
    options: normalizeReportModelOptions(reportModel?.options, currentLabel),
  };
}

function resolvePdfCoverCompiledBy(draft: PdfCoverDraft): string {
  return draft.compiledBy.trim() || PDF_COMPILED_BY;
}

function describePendingUploadGroups(groups: PendingUploadGroupState[]): string {
  return groups
    .filter((group) => group.items.length > 0)
    .map((group) => `${group.label}${group.subtitle ? ` / ${group.subtitle}` : ""}（${group.items.length} 项）`)
    .join("；");
}

function formatSessionArtifactChipLabel(category: string, label: string): string {
  const compactLabels: Record<string, string> = {
    knowledge_snippets: "知识片段",
    agentic_queries: "搜索关键词",
    yolo_full_output: "YOLO 输出",
    structured_accident_info: "结构化信息",
    images_and_keyframes: "图片关键帧",
  };
  return compactLabels[category] ?? label;
}

function normalizeLinkedArtifacts(
  artifacts: ChatSessionLinkedArtifact[] | null | undefined,
): ChatSessionLinkedArtifact[] {
  if (!Array.isArray(artifacts)) {
    return [];
  }
  return artifacts.flatMap((artifact) => {
    if (!artifact || typeof artifact !== "object") {
      return [];
    }
    const category = typeof artifact.category === "string" ? artifact.category.trim() : "";
    const label = typeof artifact.label === "string" ? artifact.label.trim() : "";
    if (!category || !label) {
      return [];
    }
    return [
      {
        label,
        category,
        kind: typeof artifact.kind === "string" && artifact.kind.trim() ? artifact.kind.trim() : "collection",
        item_count: Number.isFinite(artifact.item_count) ? artifact.item_count : 0,
        summary: typeof artifact.summary === "string" ? artifact.summary : "",
      },
    ];
  });
}

function getUploadGroupTitleClassName(label: string): string {
  if (label.length >= 12) {
    return "upload-group-title is-compact";
  }
  if (label.length >= 9) {
    return "upload-group-title is-tight";
  }
  return "upload-group-title";
}

function formatPendingUploadStatLine(groups: PendingUploadGroupState[]): string {
  const stats = getPendingUploadStats(groups);
  return `已缓冲 ${stats.activeGroupCount} 组，${stats.totalImages} 张图，${stats.totalVideos} 个视频，约 ${formatSizeLimit(stats.totalBytes || 0)}`;
}

function normalizeKnowledgeMessageOrder(messages: ChatMessage[]): ChatMessage[] {
  const knowledgeMessages = messages.filter(
    (message) => message.kind === "markdown" && message.content.startsWith(KNOWLEDGE_MESSAGE_PREFIX),
  );
  const agenticMessages = messages.filter(
    (message) => message.kind === "markdown" && message.content.startsWith(AGENTIC_MESSAGE_PREFIX),
  );
  if (knowledgeMessages.length === 0 && agenticMessages.length === 0) {
    return messages;
  }

  const firstRelevantIndex = messages.findIndex(
    (message) =>
      message.kind === "markdown" &&
      (message.content.startsWith(KNOWLEDGE_MESSAGE_PREFIX) || message.content.startsWith(AGENTIC_MESSAGE_PREFIX)),
  );
  if (firstRelevantIndex < 0) {
    return messages;
  }

  const leading = messages.slice(0, firstRelevantIndex);
  const trailing = messages.slice(firstRelevantIndex).filter(
    (message) =>
      !(
        message.kind === "markdown" &&
        (message.content.startsWith(KNOWLEDGE_MESSAGE_PREFIX) || message.content.startsWith(AGENTIC_MESSAGE_PREFIX))
      ),
  );
  return [...leading, ...knowledgeMessages, ...agenticMessages, ...trailing];
}

export default function App() {
  const [themeMode, setThemeMode] = useState<ThemeMode>(() => {
    const stored = typeof window !== "undefined" ? window.localStorage.getItem(THEME_STORAGE_KEY) : null;
    return stored === "dark" ? "dark" : "light";
  });
  const [authLoading, setAuthLoading] = useState(true);
  const [authSubmitting, setAuthSubmitting] = useState(false);
  const [authError, setAuthError] = useState("");
  const [currentUser, setCurrentUser] = useState<UserSummary | null>(null);

  useEffect(() => {
    window.localStorage.setItem(THEME_STORAGE_KEY, themeMode);
  }, [themeMode]);

  useEffect(() => {
    let cancelled = false;

    async function restoreLogin() {
      // 首屏无 token：不探测后端，直接进登录页，避免首屏出现 401/500 裸报错
      if (!hasAuthToken()) {
        if (!cancelled) {
          setAuthError("");
          setAuthLoading(false);
        }
        return;
      }
      try {
        const user = await fetchCurrentUser();
        if (!cancelled) {
          setCurrentUser(user);
          setAuthError("");
        }
      } catch (error) {
        clearAuthToken();
        if (!cancelled && !(error instanceof ApiError && error.status === 401)) {
          setAuthError(formatApiErrorMessage(error, "恢复登录状态失败。"));
        }
      } finally {
        if (!cancelled) {
          setAuthLoading(false);
        }
      }
    }

    void restoreLogin();
    return () => {
      cancelled = true;
    };
  }, []);

  async function handleLogin(payload: { username: string; password: string }) {
    setAuthSubmitting(true);
    setAuthError("");
    try {
      const result = await login(payload.username, payload.password);
      setCurrentUser(result.user);
    } catch (error) {
      setAuthError(formatApiErrorMessage(error, "登录失败。"));
      throw error;
    } finally {
      setAuthSubmitting(false);
    }
  }

  async function handleRegister(payload: { username: string; password: string; displayName?: string }) {
    setAuthSubmitting(true);
    setAuthError("");
    try {
      const result = await register(payload.username, payload.password, payload.displayName);
      setCurrentUser(result.user);
    } catch (error) {
      setAuthError(formatApiErrorMessage(error, "注册失败。"));
      throw error;
    } finally {
      setAuthSubmitting(false);
    }
  }

  if (authLoading) {
    return (
      <div className={`app-container theme-${themeMode}`}>
        <main className="main-content">
          <header className="page-header">
            <div>
              <h1>道路交通事故分析</h1>
              <p>正在恢复登录状态与工作台...</p>
            </div>
          </header>
        </main>
      </div>
    );
  }

  if (!currentUser) {
    return (
      <AuthScreen
        themeMode={themeMode}
        loading={authSubmitting}
        errorMessage={authError}
        onToggleTheme={() => setThemeMode((current) => (current === "dark" ? "light" : "dark"))}
        onLogin={handleLogin}
        onRegister={handleRegister}
      />
    );
  }

  return (
    <WorkspaceApp
      currentUser={currentUser}
      themeMode={themeMode}
      onThemeModeChange={setThemeMode}
      onLogout={() => {
        clearAuthToken();
        setCurrentUser(null);
      }}
    />
  );
}

function WorkspaceApp({
  currentUser,
  themeMode,
  onThemeModeChange,
  onLogout,
}: {
  currentUser: UserSummary;
  themeMode: ThemeMode;
  onThemeModeChange: (mode: ThemeMode) => void;
  onLogout: () => void;
}) {
  const {
    sessions,
    activeSessionId,
    activeSession,
    isLoaded,
    syncError,
    setActiveSessionId,
    createNewSession,
    updateSessionById,
    flushSessionById,
    refreshSessionById,
    reorderSessions,
    persistSessionOnPagehide,
    persistPendingSessionsOnPagehide,
    deleteSession,
  } = useChatHistory();

  const [pendingUploadGroups, setPendingUploadGroups] = useState<PendingUploadGroupState[]>(
    () => createInitialPendingUploadGroups(),
  );
  const [pendingUploadTargetGroupId, setPendingUploadTargetGroupId] = useState<string | null>(null);
  const [isGeneratingInput, setIsGeneratingInput] = useState(false);
  const [isGeneratingReport, setIsGeneratingReport] = useState(false);
  const [errorMessage, setErrorMessage] = useState("");
  const [uploadNoticeMessage, setUploadNoticeMessage] = useState("");
  const [isSidebarOpen, setIsSidebarOpen] = useState(true);
  const [isMobileSidebarOpen, setIsMobileSidebarOpen] = useState(false);
  const [editingSessionId, setEditingSessionId] = useState<string | null>(null);
  const [editTitle, setEditTitle] = useState("");
  const [searchQuery, setSearchQuery] = useState("");
  const [isSearchActive, setIsSearchActive] = useState(false);
  const [draggingSessionId, setDraggingSessionId] = useState<string | null>(null);
  const [dragOverSessionId, setDragOverSessionId] = useState<string | null>(null);
  const [liveLinkedArtifacts, setLiveLinkedArtifacts] = useState<ChatSessionLinkedArtifact[] | null>(null);

  const [mobileActionMenuId, setMobileActionMenuId] = useState<string | null>(null);
  const [mobileTab, setMobileTab] = useState<'chat' | 'review'>('chat');
  const mobileRenamePressTimerRef = useRef<number | null>(null);
  const mobileRenameTouchOriginRef = useRef<{ x: number; y: number } | null>(null);
  const mobileRenameTriggeredRef = useRef(false);
  const mobileRenameSuppressClickSessionIdRef = useRef<string | null>(null);

  function clearMobileRenamePressTimer() {
    if (mobileRenamePressTimerRef.current !== null) {
      window.clearTimeout(mobileRenamePressTimerRef.current);
      mobileRenamePressTimerRef.current = null;
    }
    mobileRenameTouchOriginRef.current = null;
    mobileRenameTriggeredRef.current = false;
  }

  useEffect(() => {
    const handleGlobalClick = () => {
      if (mobileActionMenuId) setMobileActionMenuId(null);
    };
    document.addEventListener("click", handleGlobalClick);
    return () => document.removeEventListener("click", handleGlobalClick);
  }, [mobileActionMenuId]);

  useEffect(() => {
    if (!isMobileSidebarOpen) {
      clearMobileRenamePressTimer();
      setMobileActionMenuId(null);
    }
  }, [isMobileSidebarOpen]);

  useEffect(() => () => {
    clearMobileRenamePressTimer();
  }, []);

  useEffect(() => {
    if (!isMobileSidebarOpen || !mobileActionMenuId) {
      return;
    }

    const timerId = window.setTimeout(() => {
      const wrapper = document.querySelector<HTMLElement>(`[data-session-id="${mobileActionMenuId}"]`);
      wrapper?.scrollIntoView({ block: "nearest" });
    }, 30);

    return () => window.clearTimeout(timerId);
  }, [isMobileSidebarOpen, mobileActionMenuId]);

  const moveSessionUp = (sessionId: string) => {
    const index = sessions.findIndex(s => s.id === sessionId);
    if (index > 0) {
      const newSessions = [...sessions];
      [newSessions[index - 1], newSessions[index]] = [newSessions[index], newSessions[index - 1]];
      reorderSessions(newSessions.map(s => s.id));
    }
    setMobileActionMenuId(null);
  };

  const moveSessionDown = (sessionId: string) => {
    const index = sessions.findIndex(s => s.id === sessionId);
    if (index >= 0 && index < sessions.length - 1) {
      const newSessions = [...sessions];
      [newSessions[index], newSessions[index + 1]] = [newSessions[index + 1], newSessions[index]];
      reorderSessions(newSessions.map(s => s.id));
    }
    setMobileActionMenuId(null);
  };

  const [appView, setAppView] = useState<AppView>("workspace");
  const [adminTab, setAdminTab] = useState<AdminTab>("users");
  const [publicAppConfig, setPublicAppConfig] = useState<PublicAppConfig>(DEFAULT_PUBLIC_APP_CONFIG);
  const [isAccountMenuOpen, setIsAccountMenuOpen] = useState(false);
  const [artifactPreview, setArtifactPreview] = useState<ArtifactPreviewState | null>(null);
  const [exportingFormat, setExportingFormat] = useState<ReportExportFormat | null>(null);
  const [isPdfStudioOpen, setIsPdfStudioOpen] = useState(false);
  const [isUploadWorkbenchExpanded, setIsUploadWorkbenchExpanded] = useState(false);
  const [pdfCoverDraft, setPdfCoverDraft] = useState<PdfCoverDraft>(() => buildDefaultPdfCoverDraft(""));
  const [userModelConfigState, setUserModelConfigState] = useState<CapabilityConfigState | null>(null);
  const [isUserModelDrawerOpen, setIsUserModelDrawerOpen] = useState(false);
  const [isSavingUserModelConfig, setIsSavingUserModelConfig] = useState(false);
  const [userModelConfigError, setUserModelConfigError] = useState("");
  const fileInputRef = useRef<HTMLInputElement>(null);
  const chatListRef = useRef<HTMLDivElement>(null);
  const accountMenuRef = useRef<HTMLDivElement>(null);
  const accountMenuTriggerRef = useRef<HTMLButtonElement>(null);
  const sessionsRef = useRef(sessions);
  const pendingTimersRef = useRef<number[]>([]);
  const inputGeneratingSessionIdRef = useRef<string | null>(null);
  const inputProgressMessageIdRef = useRef<string | null>(null);
  const reportAbortControllerRef = useRef<AbortController | null>(null);
  const reportGeneratingSessionIdRef = useRef<string | null>(null);
  const reportProgressMessageIdRef = useRef<string | null>(null);
  const [reportingSessionId, setReportingSessionId] = useState<string | null>(null);
  const isAdminUser = currentUser.role === "admin";
  const uploadDropzoneHintLines = buildUploadDropzoneHintLines(publicAppConfig.upload_limits);
  const shouldShowUploadWorkbench = Boolean(activeSession && !activeSession.draftJson && !activeSession.reportResult);

  useEffect(() => {
    sessionsRef.current = sessions;
  }, [sessions]);

  useEffect(() => {
    if (!isAccountMenuOpen) {
      return;
    }
    const handlePointerDown = (event: Event) => {
      if (
        accountMenuRef.current?.contains(event.target as Node) ||
        accountMenuTriggerRef.current?.contains(event.target as Node)
      ) {
        return;
      }
      setIsAccountMenuOpen(false);
    };
    document.addEventListener("pointerdown", handlePointerDown);
    return () => document.removeEventListener("pointerdown", handlePointerDown);
  }, [isAccountMenuOpen]);

  useEffect(() => () => {
    pendingTimersRef.current.forEach((timerId) => window.clearTimeout(timerId));
    pendingTimersRef.current = [];
    reportAbortControllerRef.current?.abort();
  }, []);

  useEffect(() => {
    if (appView === "admin") {
      setIsSearchActive(false);
      setMobileActionMenuId(null);
    }
  }, [appView]);

  useEffect(() => {
    let cancelled = false;

    async function loadPublicAppConfig() {
      try {
        const nextConfig = await fetchPublicAppConfig();
        if (!cancelled) {
          setPublicAppConfig({
            upload_limits: {
              ...DEFAULT_PUBLIC_APP_CONFIG.upload_limits,
              ...nextConfig.upload_limits,
            },
            report_model: normalizeReportModelConfig(nextConfig.report_model),
          });
        }
      } catch (error) {
        console.warn("读取后端配置失败，将继续使用前端兜底限制。", error);
      }
    }

    void loadPublicAppConfig();
    return () => {
      cancelled = true;
    };
  }, [currentUser.id]);

  useEffect(() => {
    let cancelled = false;
    async function loadUserModelConfigState() {
      try {
        const state = await fetchUserModelConfigs();
        if (cancelled) {
          return;
        }
        setUserModelConfigState(state);
        setUserModelConfigError("");
        const configured = new Set(state.capabilities.filter((item) => item.configured).map((item) => item.capability));
        if (!isAdminUser && (!configured.has("vision") || !configured.has("report"))) {
          setIsUserModelDrawerOpen(true);
        }
      } catch (error) {
        if (!cancelled) {
          setUserModelConfigError(resolveUiErrorMessage(error, "读取个人模型配置失败。"));
        }
      }
    }

    void loadUserModelConfigState();
    return () => {
      cancelled = true;
    };
  }, [currentUser.id, isAdminUser]);

  useEffect(() => {
    const handleBeforeUnload = (event: BeforeUnloadEvent) => {
      if (!inputGeneratingSessionIdRef.current && !reportGeneratingSessionIdRef.current) {
        return;
      }
      event.preventDefault();
      event.returnValue = "";
    };

    const handlePageHide = () => {
      const persistedSessionIds: string[] = [];
      if (inputGeneratingSessionIdRef.current && inputProgressMessageIdRef.current) {
        markInputGenerationInterrupted(inputGeneratingSessionIdRef.current, inputProgressMessageIdRef.current);
        persistSessionOnPagehide(inputGeneratingSessionIdRef.current);
        persistedSessionIds.push(inputGeneratingSessionIdRef.current);
      }
      if (reportGeneratingSessionIdRef.current && reportProgressMessageIdRef.current) {
        markReportGenerationInterrupted(reportGeneratingSessionIdRef.current, reportProgressMessageIdRef.current);
        persistSessionOnPagehide(reportGeneratingSessionIdRef.current);
        persistedSessionIds.push(reportGeneratingSessionIdRef.current);
      }
      persistPendingSessionsOnPagehide(persistedSessionIds);
    };

    window.addEventListener("beforeunload", handleBeforeUnload);
    window.addEventListener("pagehide", handlePageHide);
    return () => {
      window.removeEventListener("beforeunload", handleBeforeUnload);
      window.removeEventListener("pagehide", handlePageHide);
    };
  }, [persistPendingSessionsOnPagehide, persistSessionOnPagehide, updateSessionById]);

  // Auto-scroll chat to bottom
  useEffect(() => {
    if (chatListRef.current) {
      chatListRef.current.scrollTop = chatListRef.current.scrollHeight;
    }
  }, [activeSession?.messages]);

  // Create a default session if none exists and no active session
  useEffect(() => {
    if (!isLoaded) {
      return;
    }
    if (sessions.length === 0 && !activeSessionId) {
      createNewSession({
        title: formatCurrentTime(),
        messages: [
          createMessage("system", "text", WELCOME_MESSAGE),
        ],
      });
    }
  }, [sessions.length, activeSessionId, createNewSession, isLoaded]);

  useEffect(() => {
    setPendingUploadGroups(createInitialPendingUploadGroups());
    setPendingUploadTargetGroupId(null);
    setArtifactPreview(null);
    setErrorMessage("");
    setUploadNoticeMessage("");
    setIsPdfStudioOpen(false);
    setIsUploadWorkbenchExpanded(false);
    if (fileInputRef.current) {
      fileInputRef.current.value = "";
    }
  }, [activeSessionId]);

  useEffect(() => {
    if (!shouldShowUploadWorkbench) {
      setIsUploadWorkbenchExpanded(false);
    }
  }, [shouldShowUploadWorkbench]);

  useEffect(() => {
    const sessionId = activeSession?.id;
    const fallbackArtifacts = normalizeLinkedArtifacts(activeSession?.linkedArtifacts);
    const shouldRefreshArtifacts = Boolean(
      activeSession?.draftJson || activeSession?.reportResult || fallbackArtifacts.length > 0,
    );
    if (!sessionId || !shouldRefreshArtifacts) {
      setLiveLinkedArtifacts(null);
      return;
    }
    const resolvedSessionId = sessionId;

    let cancelled = false;

    async function refreshLinkedArtifacts() {
      try {
        const artifacts = await listChatSessionLinkedArtifacts(resolvedSessionId);
        if (cancelled) {
          return;
        }
        setLiveLinkedArtifacts(normalizeLinkedArtifacts(artifacts));
      } catch (error) {
        if (cancelled) {
          return;
        }
        console.error("刷新会话关联产物失败", error);
        setLiveLinkedArtifacts(fallbackArtifacts);
      }
    }

    void refreshLinkedArtifacts();
    return () => {
      cancelled = true;
    };
  }, [
    activeSession?.id,
    activeSession?.sessionState,
    activeSession?.reportResult?.trace_id,
  ]);

  useEffect(() => {
    if (!artifactPreview && !isUploadWorkbenchExpanded) {
      return;
    }
    const previousOverflow = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    return () => {
      document.body.style.overflow = previousOverflow;
    };
  }, [artifactPreview, isUploadWorkbenchExpanded]);

  useEffect(() => {
    if (!artifactPreview && !isUploadWorkbenchExpanded) {
      return;
    }
    const handleKeyDown = (event: KeyboardEvent) => {
      if (event.key !== "Escape") {
        return;
      }
      if (artifactPreview) {
        setArtifactPreview(null);
      }
      if (isUploadWorkbenchExpanded) {
        setIsUploadWorkbenchExpanded(false);
      }
    };
    window.addEventListener("keydown", handleKeyDown);
    return () => window.removeEventListener("keydown", handleKeyDown);
  }, [artifactPreview, isUploadWorkbenchExpanded]);

  useEffect(() => {
    const reportMarkdown = activeSession?.reportResult?.report.report_markdown;
    if (!reportMarkdown) {
      return;
    }
    setPdfCoverDraft(buildDefaultPdfCoverDraft(reportMarkdown));
  }, [activeSession?.reportResult?.trace_id, activeSession?.reportResult?.report.report_markdown]);

  function getSessionSnapshot(sessionId: string) {
    return sessionsRef.current.find((session) => session.id === sessionId) || null;
  }

  function markInputGenerationInterrupted(sessionId: string, messageId: string) {
    updateSessionById(sessionId, (session) => {
      const nextMessages = session.messages.map((message) => {
        if (message.id !== messageId) {
          return message;
        }
        return {
          ...message,
          content: "页面刷新或离开页面已中断本次事故信息生成。",
          meta: {
            ...message.meta,
            badge: "草稿阶段",
            title: "事故信息生成已中断",
            status: "error" as const,
            stages: Array.isArray(message.meta?.stages)
              ? message.meta?.stages.map((stage) => ({ ...stage, state: "done" as const }))
              : undefined,
          },
        };
      });
      const hasInterruptNotice = nextMessages.some(
        (message) => message.role === "assistant" && message.content === INPUT_REFRESH_INTERRUPT_MESSAGE,
      );
      return {
        messages: hasInterruptNotice
          ? nextMessages
          : [...nextMessages, createMessage("assistant", "text", INPUT_REFRESH_INTERRUPT_MESSAGE)],
      };
    });
  }

  function markReportGenerationInterrupted(sessionId: string, messageId: string) {
    updateSessionById(sessionId, (session) => {
      const nextMessages = session.messages.map((message) => {
        if (message.id !== messageId) {
          return message;
        }
        return {
          ...message,
          content: "页面刷新或离开页面导致当前报告生成中断。",
          meta: {
            ...message.meta,
            badge: "报告阶段",
            title: "分析报告生成已中断",
            status: "error" as const,
            stages: buildProgressStages(REPORT_PROGRESS_LABELS, REPORT_PROGRESS_LABELS.length - 1, "error"),
          },
        };
      });
      const hasInterruptNotice = nextMessages.some(
        (message) => message.role === "assistant" && message.content === REPORT_REFRESH_INTERRUPT_MESSAGE,
      );
      return {
        messages: hasInterruptNotice
          ? normalizeKnowledgeMessageOrder(nextMessages)
          : normalizeKnowledgeMessageOrder([
              ...nextMessages,
              createMessage("assistant", "text", REPORT_REFRESH_INTERRUPT_MESSAGE),
            ]),
      };
    });
  }

  function patchSessionMessage(
    sessionId: string,
    messageId: string,
    patch: Partial<ChatMessage>,
  ) {
    updateSessionById(sessionId, (session) => ({
      messages: session.messages.map((message) =>
        message.id === messageId ? { ...message, ...patch } : message,
      ),
    }));
  }

  function appendSessionMessages(
    sessionId: string,
    messages: ChatMessage[],
    updates?: Partial<Omit<ChatSession, "id" | "createdAt">>,
  ) {
    updateSessionById(sessionId, (session) => ({
      ...updates,
      messages: normalizeKnowledgeMessageOrder([...session.messages, ...messages]),
    }));
  }

  async function handleAutoSaveDraft(editedJsonString: string) {
    const sessionId = activeSession?.id;
    if (!sessionId) {
      return;
    }

    const currentSession = getSessionSnapshot(sessionId);
    if (!currentSession || currentSession.draftJson === editedJsonString) {
      return;
    }

    try {
      JSON.parse(editedJsonString);
    } catch {
      return;
    }

    updateSessionById(sessionId, (session) => {
      const nextMessages = [...session.messages];
      for (let index = nextMessages.length - 1; index >= 0; index -= 1) {
        const message = nextMessages[index];
        if (message.role === "assistant" && message.kind === "json") {
          nextMessages[index] = {
            ...message,
            content: editedJsonString,
          };
          break;
        }
      }

      return {
        draftJson: editedJsonString,
        messages: nextMessages,
      };
    });
    await flushSessionById(sessionId);
  }

  function startProgressMessage(
    sessionId: string,
    messageId: string,
    badge: string,
    title: string,
    labels: string[],
  ) {
    let activeIndex = 0;
    const timerIds: number[] = [];

    const applyStatus = (
      status: ChatMessageMeta["status"],
      content: string,
      stageIndex: number,
      nextTitle: string = title,
    ) => {
      activeIndex = stageIndex;
      patchSessionMessage(sessionId, messageId, {
        content,
        meta: {
          badge,
          title: nextTitle,
          status,
          stages: buildProgressStages(labels, stageIndex, status),
        },
      });
    };

    applyStatus("running", labels[0] ?? "处理中", 0);

    labels.slice(1).forEach((label, index) => {
      const timerId = window.setTimeout(() => {
        applyStatus("running", label, index + 1);
      }, PROGRESS_STAGE_INTERVAL_MS * (index + 1));
      timerIds.push(timerId);
      pendingTimersRef.current.push(timerId);
    });

    const clearTimers = () => {
      timerIds.forEach((timerId) => window.clearTimeout(timerId));
      pendingTimersRef.current = pendingTimersRef.current.filter((timerId) => !timerIds.includes(timerId));
    };

    return {
      succeed(finalContent: string, finalTitle?: string) {
        clearTimers();
        applyStatus("success", finalContent, Math.max(labels.length - 1, 0), finalTitle ?? title);
      },
      fail(finalContent: string, finalTitle?: string) {
        clearTimers();
        applyStatus("error", finalContent, Math.min(activeIndex, Math.max(labels.length - 1, 0)), finalTitle ?? title);
      },
    };
  }

  function updateReportProgressMessageFromEvent(
    sessionId: string,
    messageId: string,
    event: Record<string, unknown>,
  ) {
    const stage = String(event.stage || "connect");
    const status = String(event.status || "started");
    const label = String(event.label || "正在处理");
    const stageIndex = REPORT_STAGE_INDEX[stage] ?? 0;

    patchSessionMessage(sessionId, messageId, {
      content: label,
      meta: {
        badge: "报告阶段",
        title:
          status === "failed"
            ? "分析报告生成失败"
            : status === "completed" && stage === "postprocess"
              ? "分析报告生成完成"
              : "正在生成分析报告",
        status: status === "failed" ? "error" : status === "completed" && stage === "postprocess" ? "success" : "running",
        stages: buildProgressStages(
          REPORT_PROGRESS_LABELS,
          stageIndex,
          status === "failed" ? "error" : status === "completed" && stage === "postprocess" ? "success" : "running",
        ),
      },
    });
  }

  function handleAddSession() {
    resetPendingUploadState();
    setErrorMessage("");
    createNewSession({
      title: formatCurrentTime(),
      messages: [
        createMessage("system", "text", WELCOME_MESSAGE),
      ],
    });
  }

  function resetPendingUploadState() {
    setPendingUploadGroups(createInitialPendingUploadGroups());
    setPendingUploadTargetGroupId(null);
    setUploadNoticeMessage("");
    setIsUploadWorkbenchExpanded(false);
    if (fileInputRef.current) {
      fileInputRef.current.value = "";
    }
  }

  function handleRemovePendingFile(groupId: string, itemId: string) {
    setPendingUploadGroups((current) => removePendingUploadItem(current, groupId, itemId));
    setErrorMessage("");
    setUploadNoticeMessage("");
  }

  async function handleFileChange(event: ChangeEvent<HTMLInputElement>) {
    const files = Array.from(event.target.files ?? []);
    const targetGroupId = pendingUploadTargetGroupId;
    if (!targetGroupId) {
      if (fileInputRef.current) {
        fileInputRef.current.value = "";
      }
      return;
    }
    if (files.length === 0) {
      return;
    }

    const validationResult = validatePendingUploadSelection(
      pendingUploadGroups,
      targetGroupId,
      files,
      publicAppConfig.upload_limits,
    );
    if (validationResult.blockingMessage) {
      setErrorMessage(withUploadLimitContext(validationResult.blockingMessage, publicAppConfig.upload_limits));
      if (fileInputRef.current) {
        fileInputRef.current.value = "";
      }
      return;
    }

    setPendingUploadGroups((current) => appendPendingFiles(current, targetGroupId, files));
    setErrorMessage("");
    setUploadNoticeMessage(validationResult.noticeMessage ?? "");
    if (fileInputRef.current) {
      fileInputRef.current.value = "";
    }
  }

  function handleTriggerUploadClick(groupId: string) {
    setPendingUploadTargetGroupId(groupId);
    fileInputRef.current?.click();
  }

  function handleOpenUploadWorkbench() {
    setIsUploadWorkbenchExpanded(true);
  }

  function handleCloseUploadWorkbench() {
    setIsUploadWorkbenchExpanded(false);
  }

  async function handleOpenArtifactPreview(category: string) {
    if (!activeSession?.id) {
      return;
    }
    setArtifactPreview({
      category,
      detail: null,
      loading: true,
      error: "",
    });
    try {
      const detail = await fetchChatSessionLinkedArtifactDetail(activeSession.id, category);
      setArtifactPreview({
        category,
        detail,
        loading: false,
        error: "",
      });
    } catch (error) {
      setArtifactPreview({
        category,
        detail: null,
        loading: false,
        error: resolveUiErrorMessage(error, "读取会话关联产物失败。"),
      });
    }
  }

  function handleCloseArtifactPreview() {
    setArtifactPreview(null);
  }

  function handleArtifactCardKeyDown(
    event: ReactKeyboardEvent<HTMLElement>,
    category: string,
  ) {
    if (event.key !== "Enter" && event.key !== " ") {
      return;
    }
    event.preventDefault();
    void handleOpenArtifactPreview(category);
  }

  async function handleGenerateInput() {
    const sessionId = activeSession?.id;
    if (!sessionId) {
      return;
    }

    const uploadPayload = buildGroupedUploadPayload(pendingUploadGroups);
    if (uploadPayload.files.length === 0) {
      setErrorMessage("请先按清单至少上传一个分组的图片或视频，再开始生成事故信息。");
      return;
    }
    const currentSession = getSessionSnapshot(sessionId);

    setErrorMessage("");
    setUploadNoticeMessage("");
    setIsGeneratingInput(true);
    setIsUploadWorkbenchExpanded(false);

    const batchLabel = formatBatchMediaLabel(uploadPayload.files);
    const selectedNames = describePendingUploadGroups(pendingUploadGroups);
    const stageLabels = getInputProgressLabels(uploadPayload.files);
    const userMessage = createMessage(
      "user",
      "text",
      `我按清单分组上传了${batchLabel}：${selectedNames}`
    );
    const progressMessage = createMessage(
      "assistant",
      "progress",
      stageLabels[0] ?? "准备开始分析",
      {
        badge: "草稿阶段",
        title: "正在生成事故信息",
        status: "running",
        stages: buildProgressStages(stageLabels, 0, "running"),
      },
    );

    const nextTitle =
      uploadPayload.files.length === 1
        ? uploadPayload.files[0].name
        : `${uploadPayload.files[0].name} 等 ${uploadPayload.files.length} 项`;

    updateSessionById(sessionId, {
      title: nextTitle,
      sourceType: detectBatchMediaType(uploadPayload.files),
      sourceName: selectedNames,
      reportResult: null,
      draftMeta: null,
      draftJson: "",
      messages: [...(currentSession?.messages || []), userMessage, progressMessage],
    });
    inputGeneratingSessionIdRef.current = sessionId;
    inputProgressMessageIdRef.current = progressMessage.id;
    await flushSessionById(sessionId);
    const progressController = startProgressMessage(
      sessionId,
      progressMessage.id,
      progressMessage.meta?.badge || "草稿阶段",
      progressMessage.meta?.title || "正在生成事故信息",
      stageLabels,
    );

    try {
      const response = await generateInputFromUploads(uploadPayload);
      const draftJsonString = prettyJson(response.generated_input);
      const processHint = response.media_type === "mixed"
        ? `已完成多源识别，共处理 ${response.source_count} 个文件，送入视觉模型 ${response.frame_manifest.length} 张代表图片/关键帧。`
        : response.media_type === "video"
          ? `已完成 YOLO + 抽帧 + 视觉模型识别，送入视觉模型 ${response.frame_manifest.length} 张关键帧。`
          : response.source_count > 1
            ? `已完成多图片识别，共处理 ${response.source_count} 张图片。`
            : "当前图片链路直接走视觉模型生成事故信息草稿。";

      progressController.succeed(
        "事故信息草稿已生成。",
        "事故信息已生成",
      );
      const assistantMsg1 = createMessage(
        "assistant",
        "text",
        `事故信息草稿已生成，请在下方编辑确认。${processHint}`
      );
      const yoloPreviewMarkdown = formatYoloPreviewMarkdown(
        response.yolo_summary_preview,
        response.frame_manifest,
      );
      const yoloMessage = yoloPreviewMarkdown
        ? createMessage("assistant", "markdown", yoloPreviewMarkdown)
        : null;
      const assistantMsg2 = createMessage("assistant", "json", draftJsonString);

      appendSessionMessages(sessionId, [
        assistantMsg1,
        ...(yoloMessage ? [yoloMessage] : []),
        assistantMsg2,
      ], {
        draftMeta: response,
        draftJson: draftJsonString,
        sourceType: response.media_type,
        sourceName: selectedNames,
      });
      resetPendingUploadState();
      await refreshSessionById(sessionId);
    } catch (error) {
      const message = resolveUiErrorMessage(error, "生成事故信息失败。");
      progressController.fail(`事故信息生成失败：${message}`, "事故信息生成失败");
      setErrorMessage(withUploadLimitContext(message, publicAppConfig.upload_limits));
      appendSessionMessages(sessionId, [
        createMessage("assistant", "text", `事故信息生成失败：${message}`),
      ]);
    } finally {
      inputGeneratingSessionIdRef.current = null;
      inputProgressMessageIdRef.current = null;
      setIsGeneratingInput(false);
    }
  }

  async function handleSaveUserModelConfig(payload: UpdateCapabilityConfigsPayload) {
    try {
      setIsSavingUserModelConfig(true);
      setUserModelConfigError("");
      const nextState = await updateUserModelConfigs(payload);
      setUserModelConfigState(nextState);
      setIsUserModelDrawerOpen(false);
    } catch (error) {
      setUserModelConfigError(resolveUiErrorMessage(error, "保存个人模型配置失败。"));
      throw error;
    } finally {
      setIsSavingUserModelConfig(false);
    }
  }

  async function handleConfirmAndGenerateReport(confirmedJsonString: string) {
    const sessionId = activeSession?.id;
    if (!sessionId) {
      return;
    }

    if (!confirmedJsonString.trim()) {
      setErrorMessage("当前没有可确认的事故信息草稿。");
      return;
    }

    let accidentData: Record<string, unknown>;
    try {
      accidentData = JSON.parse(confirmedJsonString) as Record<string, unknown>;
    } catch {
      setErrorMessage("事故信息 JSON 格式不合法，请先修正后再确认。");
      return;
    }

    setErrorMessage("");
    setIsGeneratingReport(true);
    setReportingSessionId(sessionId);

    const currentSession = getSessionSnapshot(sessionId);
    const abortController = new AbortController();
    reportAbortControllerRef.current = abortController;
    reportGeneratingSessionIdRef.current = sessionId;
    const userMsg = createMessage("user", "text", "我已确认事故信息，请继续生成指导意见和分析报告。");
    const progressMessage = createMessage(
      "assistant",
      "progress",
      "正在与报告服务建立连接...",
      {
        badge: "报告阶段",
        title: "正在生成分析报告",
        status: "running",
        stages: buildProgressStages(REPORT_PROGRESS_LABELS, 0, "running"),
      },
    );
    updateSessionById(sessionId, {
      draftJson: confirmedJsonString,
      messages: [...(currentSession?.messages || []), userMsg, progressMessage],
    });
    reportProgressMessageIdRef.current = progressMessage.id;
    await flushSessionById(sessionId);

    try {
      let hasKnowledgeSummary = false;
      const emittedRounds = new Set<number>();

      const response = await generateReportFromConfirmedInputStream(accidentData, {
        onEvent: (event) => {
          if (event.event === "stage") {
            updateReportProgressMessageFromEvent(sessionId, progressMessage.id, event);
            return;
          }

          if (event.event === "knowledge" && !hasKnowledgeSummary) {
            const knowledgeMarkdown = formatKnowledgeSnippetsMarkdown(
              ((event.snippets as GenerateReportResponse["knowledge_snippets"] | undefined) ?? []),
              (event.retrieval_meta as Record<string, unknown>) ?? {},
            );
            if (knowledgeMarkdown) {
              hasKnowledgeSummary = true;
              appendSessionMessages(sessionId, [
                createMessage("assistant", "markdown", knowledgeMarkdown),
              ]);
            }
            return;
          }

          if (event.event === "agentic_round") {
            const round = (event.round as { round?: number } | undefined) ?? {};
            const roundNumber = Number(round.round ?? 0);
            if (roundNumber > 0 && !emittedRounds.has(roundNumber)) {
              emittedRounds.add(roundNumber);
              const agenticMarkdown = formatAgenticRoundsMarkdown([
                round as GenerateReportResponse["agentic_retrieval_rounds"][number],
              ]);
              if (agenticMarkdown) {
                appendSessionMessages(sessionId, [
                  createMessage("assistant", "markdown", agenticMarkdown),
                ]);
              }
            }
          }
        },
      }, sessionId, abortController.signal);

      patchSessionMessage(sessionId, progressMessage.id, {
        content: "报告文件已写入输出目录。",
        meta: {
          badge: "报告阶段",
          title: "分析报告生成完成",
          status: "success",
          stages: buildProgressStages(REPORT_PROGRESS_LABELS, REPORT_PROGRESS_LABELS.length - 1, "success"),
        },
      });

      const normalizedReportMarkdown = normalizeMarkdownForDisplay(response.report.report_markdown);
      const fallbackKnowledgeMarkdown = !hasKnowledgeSummary
        ? formatKnowledgeSnippetsMarkdown(
            response.initial_knowledge_snippets ?? response.knowledge_snippets ?? [],
            response.retrieval_meta ?? {},
          )
        : "";
      const fallbackAgenticMarkdown = response.agentic_retrieval_rounds?.length
        ? formatAgenticRoundsMarkdown(response.agentic_retrieval_rounds)
        : "";

      updateSessionById(sessionId, (session) => {
        const nextMessages = normalizeKnowledgeMessageOrder([...session.messages]);
        const hasRenderedKnowledge = nextMessages.some(
          (message) =>
            message.kind === "markdown" &&
            message.content.startsWith(KNOWLEDGE_MESSAGE_PREFIX),
        );
        const hasRenderedAgentic = nextMessages.some(
          (message) =>
            message.kind === "markdown" &&
            message.content.startsWith(AGENTIC_MESSAGE_PREFIX),
        );
        const firstAgenticIndex = nextMessages.findIndex(
          (message) =>
            message.kind === "markdown" &&
            message.content.startsWith(AGENTIC_MESSAGE_PREFIX),
        );

        if (fallbackKnowledgeMarkdown && !hasRenderedKnowledge) {
          const knowledgeMessage = createMessage("assistant", "markdown", fallbackKnowledgeMarkdown);
          if (firstAgenticIndex >= 0) {
            nextMessages.splice(firstAgenticIndex, 0, knowledgeMessage);
          } else {
            nextMessages.push(knowledgeMessage);
          }
        }
        if (fallbackAgenticMarkdown && !hasRenderedAgentic) {
          nextMessages.push(createMessage("assistant", "markdown", fallbackAgenticMarkdown));
        }

        return {
          messages: normalizeKnowledgeMessageOrder(nextMessages),
          reportResult: {
            ...response,
            initial_knowledge_snippets: response.initial_knowledge_snippets ?? [],
            knowledge_snippets: response.knowledge_snippets ?? [],
            retrieval_meta: response.retrieval_meta ?? {},
            agentic_retrieval_rounds: response.agentic_retrieval_rounds ?? [],
            report: {
              ...response.report,
              report_markdown: normalizedReportMarkdown,
            },
          },
        };
      });
      await flushSessionById(sessionId);
    } catch (error) {
      if (error instanceof DOMException && error.name === "AbortError") {
        patchSessionMessage(sessionId, progressMessage.id, {
          content: "已手动停止本次分析报告生成。",
          meta: {
            badge: "报告阶段",
            title: "分析报告已停止",
            status: "error",
            stages: buildProgressStages(REPORT_PROGRESS_LABELS, REPORT_PROGRESS_LABELS.length - 1, "error"),
          },
        });
        appendSessionMessages(sessionId, [
          createMessage("assistant", "text", "已停止当前报告生成，现有草稿会保留，你可以稍后重新开始。"),
        ]);
        await flushSessionById(sessionId);
        return;
      }
      const message = resolveUiErrorMessage(error, "生成分析报告失败。");
      patchSessionMessage(sessionId, progressMessage.id, {
        content: `分析报告生成失败：${message}`,
        meta: {
          badge: "报告阶段",
          title: "分析报告生成失败",
          status: "error",
          stages: buildProgressStages(REPORT_PROGRESS_LABELS, REPORT_PROGRESS_LABELS.length - 1, "error"),
        },
      });
      setErrorMessage(message);
      appendSessionMessages(sessionId, [
        createMessage("assistant", "text", `分析报告生成失败：${message}`),
      ]);
      await flushSessionById(sessionId);
    } finally {
      if (reportAbortControllerRef.current === abortController) {
        reportAbortControllerRef.current = null;
      }
      reportGeneratingSessionIdRef.current = null;
      reportProgressMessageIdRef.current = null;
      setIsGeneratingReport(false);
      setReportingSessionId(null);
    }
  }

  function handleStopReportGeneration() {
    if (!reportAbortControllerRef.current) {
      return;
    }
    const confirmed = window.confirm("此操作会停止当前分析报告生成，是否继续？");
    if (!confirmed) {
      return;
    }
    reportAbortControllerRef.current?.abort();
  }

  async function handleDownloadReportExport(
    exportFormat: ReportExportFormat,
    options?: PdfExportOptions,
  ): Promise<boolean> {
    const sessionId = activeSession?.id;
    const traceId = activeSession?.reportResult?.trace_id;
    if (!sessionId || !traceId) {
      setErrorMessage("当前会话还没有可导出的报告。");
      return false;
    }

    setErrorMessage("");
    setExportingFormat(exportFormat);
    try {
      const { blob, fileName } = await downloadReportExport(traceId, exportFormat, options);
      const objectUrl = window.URL.createObjectURL(blob);
      const anchor = document.createElement("a");
      anchor.href = objectUrl;
      anchor.download = fileName;
      document.body.appendChild(anchor);
      anchor.click();
      anchor.remove();
      window.setTimeout(() => window.URL.revokeObjectURL(objectUrl), 1000);
      const exportCategory = EXPORT_CATEGORY_BY_FORMAT[exportFormat];
      updateSessionById(sessionId, (session) => {
        const nextLinkedFiles = [...session.linkedFiles];
        const existingIndex = nextLinkedFiles.findIndex((file) => file.category === exportCategory);
        if (existingIndex >= 0) {
          nextLinkedFiles[existingIndex] = {
            ...nextLinkedFiles[existingIndex],
            exists: true,
          };
        } else {
          nextLinkedFiles.push({
            label:
              exportFormat === "pdf"
                ? "报告 PDF"
                : exportFormat === "docx"
                  ? "报告 Word"
                  : "报告 Markdown",
            path: "",
            category: exportCategory,
            path_type: "file",
            exists: true,
          });
        }

        return {
          linkedFiles: nextLinkedFiles,
          sessionState: exportFormat === "md" ? session.sessionState : "export_ready",
        };
      });
      return true;
    } catch (error) {
      const message = resolveUiErrorMessage(error, "报告导出失败。");
      setErrorMessage(message);
      return false;
    } finally {
      setExportingFormat(null);
    }
  }

  function handleOpenPdfStudio() {
    const reportMarkdown = activeSession?.reportResult?.report.report_markdown;
    if (!reportMarkdown || !activeSession?.reportResult?.trace_id) {
      setErrorMessage("当前会话还没有可导出的报告。");
      return;
    }
    setErrorMessage("");
    setPdfCoverDraft(buildDefaultPdfCoverDraft(reportMarkdown));
    setIsPdfStudioOpen(true);
  }

  function handleClosePdfStudio() {
    setIsPdfStudioOpen(false);
  }

  function handleResetPdfCoverDraft() {
    const reportMarkdown = activeSession?.reportResult?.report.report_markdown ?? "";
    setPdfCoverDraft(buildDefaultPdfCoverDraft(reportMarkdown));
  }

  async function handleConfirmPdfExport() {
    const title = pdfCoverDraft.title.trim();
    const compiledBy = resolvePdfCoverCompiledBy(pdfCoverDraft);
    if (!title) {
      setErrorMessage("PDF 封面标题不能为空。");
      return;
    }
    if (!compiledBy) {
      setErrorMessage("PDF 编制人不能为空。");
      return;
    }
    if (pdfCoverDraft.dateMode === "custom" && !pdfCoverDraft.dateText.trim()) {
      setErrorMessage("自定义日期模式下，请填写封面日期。");
      return;
    }

    const exported = await handleDownloadReportExport("pdf", {
      coverTitle: title,
      coverSubtitle: pdfCoverDraft.subtitle.trim(),
      coverCompiledBy: compiledBy,
      coverDateMode: pdfCoverDraft.dateMode,
      coverDateText: pdfCoverDraft.dateMode === "custom" ? pdfCoverDraft.dateText.trim() : undefined,
    });
    if (exported) {
      setIsPdfStudioOpen(false);
    }
  }

  async function handleExportActionClick(exportFormat: ReportExportFormat) {
    if (exportFormat === "pdf") {
      handleOpenPdfStudio();
      return;
    }
    await handleDownloadReportExport(exportFormat);
  }

  function startRenameSession(session: ChatSession) {
    clearMobileRenamePressTimer();
    setMobileActionMenuId(null);
    setEditingSessionId(session.id);
    setEditTitle(session.title);
  }

  function handleStartRename(session: ChatSession, e: MouseEvent) {
    e.stopPropagation();
    startRenameSession(session);
  }

  async function handleFinishRename(sessionId: string) {
    if (editTitle.trim()) {
      updateSessionById(sessionId, { title: editTitle.trim() });
      await flushSessionById(sessionId);
    }
    setEditingSessionId(null);
  }

  function handleSessionDragStart(sessionId: string, event: DragEvent<HTMLDivElement>) {
    if (searchQuery.trim() || editingSessionId === sessionId) {
      event.preventDefault();
      return;
    }

    event.stopPropagation();
    event.dataTransfer.effectAllowed = "move";
    event.dataTransfer.setData("text/plain", sessionId);
    setDraggingSessionId(sessionId);
    setDragOverSessionId(null);
  }

  function handleSessionDragOver(sessionId: string, event: DragEvent<HTMLDivElement>) {
    if (!draggingSessionId || draggingSessionId === sessionId || searchQuery.trim()) {
      return;
    }
    event.preventDefault();
    event.dataTransfer.dropEffect = "move";
    if (dragOverSessionId !== sessionId) {
      setDragOverSessionId(sessionId);
    }
  }

  async function handleSessionDrop(sessionId: string, event: DragEvent<HTMLDivElement>) {
    event.preventDefault();
    const sourceId = draggingSessionId || event.dataTransfer.getData("text/plain");
    setDragOverSessionId(null);
    setDraggingSessionId(null);

    if (!sourceId || sourceId === sessionId || searchQuery.trim()) {
      return;
    }

    const orderedIds = sessions.map((session) => session.id);
    const sourceIndex = orderedIds.indexOf(sourceId);
    const targetIndex = orderedIds.indexOf(sessionId);
    if (sourceIndex < 0 || targetIndex < 0) {
      return;
    }

    const nextOrderedIds = [...orderedIds];
    nextOrderedIds.splice(sourceIndex, 1);
    nextOrderedIds.splice(targetIndex, 0, sourceId);

    try {
      await reorderSessions(nextOrderedIds);
    } catch (error) {
      setErrorMessage(resolveUiErrorMessage(error, "会话排序失败。"));
    }
  }

  function handleSessionDragEnd() {
    setDraggingSessionId(null);
    setDragOverSessionId(null);
  }

  const filteredSessions = sessions.filter(session => {
    if (!searchQuery.trim()) return true;
    const query = searchQuery.toLowerCase();
    if (session.title.toLowerCase().includes(query)) return true;
    return session.messages.some(msg => 
      msg.content.toLowerCase().includes(query)
    );
  });
  const activeLinkedArtifacts = normalizeLinkedArtifacts(
    liveLinkedArtifacts ?? activeSession?.linkedArtifacts ?? [],
  );

  async function handleDeleteSession(sessionId: string) {
    const confirmed = window.confirm("此操作会将会话相关的所有中间文件都删除，是否继续？");
    if (!confirmed) {
      return;
    }
    try {
      setErrorMessage("");
      await deleteSession(sessionId);
    } catch (error) {
      setErrorMessage(resolveUiErrorMessage(error, "删除会话失败。"));
    }
  }

  async function handleSelectSession(sessionId: string) {
    clearMobileRenamePressTimer();
    setActiveSessionId(sessionId);
    setMobileActionMenuId(null);
    if (isMobileSidebarOpen) {
      setIsMobileSidebarOpen(false);
    }
    await refreshSessionById(sessionId);
  }

  function handleSessionItemClick(sessionId: string) {
    if (mobileRenameSuppressClickSessionIdRef.current === sessionId) {
      mobileRenameSuppressClickSessionIdRef.current = null;
      return;
    }
    void handleSelectSession(sessionId);
  }

  function handleMobileRenameTouchStart(session: ChatSession, event: TouchEvent<HTMLDivElement>) {
    if (!isMobileSidebarOpen || editingSessionId === session.id) {
      return;
    }

    const target = event.target;
    if (target instanceof HTMLElement && target.closest("button, input, textarea")) {
      return;
    }

    const touch = event.touches[0];
    if (!touch) {
      return;
    }

    clearMobileRenamePressTimer();
    mobileRenameTouchOriginRef.current = { x: touch.clientX, y: touch.clientY };
    mobileRenamePressTimerRef.current = window.setTimeout(() => {
      mobileRenameTriggeredRef.current = true;
      mobileRenameSuppressClickSessionIdRef.current = session.id;
      startRenameSession(session);
      window.setTimeout(() => {
        if (mobileRenameSuppressClickSessionIdRef.current === session.id) {
          mobileRenameSuppressClickSessionIdRef.current = null;
        }
      }, 600);
    }, MOBILE_SESSION_RENAME_PRESS_MS);
  }

  function handleMobileRenameTouchMove(event: TouchEvent<HTMLDivElement>) {
    if (!isMobileSidebarOpen || mobileRenamePressTimerRef.current === null) {
      return;
    }

    const touch = event.touches[0];
    const origin = mobileRenameTouchOriginRef.current;
    if (!touch || !origin) {
      clearMobileRenamePressTimer();
      return;
    }

    if (
      Math.abs(touch.clientX - origin.x) > MOBILE_SESSION_RENAME_MOVE_TOLERANCE ||
      Math.abs(touch.clientY - origin.y) > MOBILE_SESSION_RENAME_MOVE_TOLERANCE
    ) {
      clearMobileRenamePressTimer();
    }
  }

  function handleMobileRenameTouchEnd(sessionId: string, event: TouchEvent<HTMLDivElement>) {
    const renameTriggered = mobileRenameTriggeredRef.current;
    clearMobileRenamePressTimer();
    if (renameTriggered) {
      mobileRenameSuppressClickSessionIdRef.current = sessionId;
      event.preventDefault();
      event.stopPropagation();
    }
  }

  function handleMobileRenameTouchCancel() {
    clearMobileRenamePressTimer();
  }

  function renderThemeToggle() {
    const isDarkMode = themeMode === "dark";
    return (
      <button
        className="theme-toggle-btn"
        onClick={() => onThemeModeChange(isDarkMode ? "light" : "dark")}
        title={isDarkMode ? "切换为浅色模式" : "切换为深色模式"}
        aria-label={isDarkMode ? "切换为浅色模式" : "切换为深色模式"}
      >
        {isDarkMode ? <MoonIcon /> : <SunIcon />}
      </button>
    );
  }

  function renderAdminHeaderTabs() {
    if (!isAdminUser || appView !== "admin") {
      return null;
    }
    return (
      <div className="admin-header-tabs-shell">
        <button
          type="button"
          className="admin-header-back-btn"
          onClick={() => setAppView("workspace")}
        >
          <ArrowLeftIcon />
          <span>返回主界面</span>
        </button>
        <div className="admin-header-tabs" role="tablist" aria-label="管理控制台标签">
          <button
            type="button"
            className={`admin-header-tab ${adminTab === "users" ? "is-active" : ""}`}
            onClick={() => setAdminTab("users")}
          >
            用户管理
          </button>
          <button
            type="button"
            className={`admin-header-tab ${adminTab === "spaces" ? "is-active" : ""}`}
            onClick={() => setAdminTab("spaces")}
          >
            空间管理
          </button>
        </div>
      </div>
    );
  }

  function renderHeaderAdminToggle() {
    if (!isAdminUser) {
      return null;
    }
    return (
      <button
        type="button"
        className={`theme-toggle-btn admin-console-toggle ${appView === "admin" ? "is-active" : ""}`}
        onClick={() => {
          if (appView !== "admin") {
            setAppView("admin");
          }
        }}
        title="打开管理控制台"
        aria-label="打开管理控制台"
        aria-pressed={appView === "admin"}
      >
        <ShieldIcon />
      </button>
    );
  }

  function renderAccountMenu() {
    if (!isAccountMenuOpen) {
      return null;
    }
    return (
      <div ref={accountMenuRef} className="account-popover" role="menu" aria-label="账号菜单">
        <div className="account-popover-header">
          <span>{currentUser.username}</span>
        </div>
        <div className="account-popover-actions">
          {isAdminUser ? (
            <button
              type="button"
              className="account-popover-item"
              onClick={() => {
                setIsUserModelDrawerOpen(true);
                setIsAccountMenuOpen(false);
              }}
            >
              模型配置调整
            </button>
          ) : (
            <button
              type="button"
              className="account-popover-item"
              onClick={() => {
                setIsUserModelDrawerOpen(true);
                setIsAccountMenuOpen(false);
              }}
            >
              模型配置调整
            </button>
          )}
          <button
            type="button"
            className="account-popover-item is-danger"
            onClick={() => {
              setIsAccountMenuOpen(false);
              void onLogout();
            }}
          >
            退出登录
          </button>
        </div>
      </div>
    );
  }

  function renderArtifactMeta(meta: Record<string, unknown>) {
    const entries = Object.entries(meta).filter(([, value]) => {
      if (value === null || value === undefined) {
        return false;
      }
      if (typeof value === "string") {
        return value.trim().length > 0;
      }
      if (Array.isArray(value)) {
        return value.length > 0;
      }
      if (typeof value === "object") {
        return Object.keys(value as Record<string, unknown>).length > 0;
      }
      return true;
    });
    if (!entries.length) {
      return <p className="artifact-preview-empty">当前产物没有额外元信息。</p>;
    }
    return (
      <div className="artifact-preview-meta-list">
        {entries.map(([key, value]) => (
          <div key={key} className="artifact-preview-meta-item">
            <span>{key}</span>
            <strong>{typeof value === "string" ? value : JSON.stringify(value, null, 2)}</strong>
          </div>
        ))}
      </div>
    );
  }

  function renderArtifactDetailBody(detail: LinkedArtifactDetailResponse) {
    if (detail.category === "knowledge_snippets") {
      return (
        <div className="artifact-document-flow">
          {detail.content.map((item, index) => (
            <article key={`${detail.category}-${index}`} className="artifact-document-block">
              <span className="artifact-document-kicker">{String(item.section ?? "知识片段")}</span>
              <h4>{String(item.title ?? `片段 ${index + 1}`)}</h4>
              <p>{String(item.content ?? "") || "无正文内容"}</p>
              <div className="artifact-document-meta">
                {item.source ? <span>来源：{String(item.source)}</span> : null}
                {item.citation ? <span>引用：{String(item.citation)}</span> : null}
                {typeof item.score === "number" ? <span>匹配分：{Number(item.score).toFixed(3)}</span> : null}
              </div>
            </article>
          ))}
        </div>
      );
    }

    if (detail.category === "agentic_queries") {
      return (
        <div className="artifact-document-flow">
          {detail.content.map((item, index) => (
            <article key={`${detail.category}-${index}`} className="artifact-document-block">
              <span className="artifact-document-kicker">第 {String(item.round ?? index + 1)} 轮自主搜索</span>
              <h4>{String(item.query ?? "未记录检索关键词")}</h4>
              <p>{String(item.reason ?? "未记录触发原因")}</p>
              <div className="artifact-document-meta">
                <span>请求条数：{String(item.requested_top_k ?? "-")}</span>
                <span>返回条数：{String(item.returned_count ?? "-")}</span>
              </div>
              <pre className="artifact-json-block">{prettyJson(item.snippets ?? [])}</pre>
            </article>
          ))}
        </div>
      );
    }

    if (detail.category === "images_and_keyframes") {
      const assetMap = new Map(detail.assets.map((asset) => [asset.asset_id, asset]));
      return (
        <div className="artifact-gallery-flow">
          {detail.content.map((group, index) => {
            const items = Array.isArray(group.items) ? group.items : [];
            return (
              <section key={`${detail.category}-${index}`} className="artifact-gallery-section">
                <div className="artifact-gallery-section-head">
                  <div>
                    <span className="artifact-document-kicker">分组 {String(group.sequence ?? index + 1)}</span>
                    <h4>{String(group.category_label ?? "未分组材料")}</h4>
                    {group.category_subtitle ? <p>{String(group.category_subtitle)}</p> : null}
                  </div>
                  <strong>{items.length} 项</strong>
                </div>
                <div className="artifact-gallery-grid">
                  {items.map((item) => {
                    const asset = assetMap.get(String(item.asset_id ?? ""));
                    if (!asset || !activeSession?.id) {
                      return null;
                    }
                    const assetUrl = buildChatSessionLinkedArtifactAssetUrl(activeSession.id, detail.category, asset.asset_id);
                    return (
                      <article key={asset.asset_id} className="artifact-gallery-card">
                        <div className="artifact-gallery-media">
                          {asset.media_type === "video" ? (
                            <video src={assetUrl} controls preload="metadata" />
                          ) : (
                            <img src={assetUrl} alt={asset.file_name} loading="lazy" />
                          )}
                        </div>
                        <div className="artifact-gallery-copy">
                          <div className="artifact-gallery-topline">
                            <span>{asset.annotation_label || asset.kind}</span>
                            {asset.sequence ? <strong>序号 {asset.sequence}</strong> : null}
                          </div>
                          <h5>{asset.file_name}</h5>
                          {asset.source_name ? <p>来源：{asset.source_name}</p> : null}
                          {asset.reason ? <p>说明：{asset.reason}</p> : null}
                          {typeof asset.timestamp_seconds === "number" ? (
                            <p>时间点：{asset.timestamp_seconds.toFixed(2)} 秒</p>
                          ) : null}
                        </div>
                      </article>
                    );
                  })}
                </div>
              </section>
            );
          })}
        </div>
      );
    }

    return (
      <div className="artifact-document-flow">
        {detail.content.map((item, index) => (
          <article key={`${detail.category}-${index}`} className="artifact-document-block">
            <span className="artifact-document-kicker">{detail.label}</span>
            <pre className="artifact-json-block">{prettyJson(item)}</pre>
          </article>
        ))}
      </div>
    );
  }

  function renderUploadWorkbenchSurface(fullscreen = false) {
    const pendingStats = getPendingUploadStats(pendingUploadGroups);
    const totalBufferedItems = pendingStats.totalImages + pendingStats.totalVideos;
    return (
      <div className={`upload-workbench ${fullscreen ? "is-fullscreen" : "is-inline"}`}>
        {!fullscreen ? renderBrandWatermark("workspace-watermark is-inline") : null}
        {fullscreen ? (
          <div className="upload-workbench-toolbar">
            <div className="upload-workbench-toolbar-main">
              <div className="upload-workbench-topline">
                <span className="upload-workbench-kicker">首传分组工作台</span>
                <span className="upload-workbench-mode-badge">{pendingUploadGroups.length} 个固定分组</span>
              </div>
              <div className="upload-workbench-toolbar-titleline">
                <h3>事故资料分组整理台</h3>
                <p className="upload-workbench-toolbar-copy">
                  <span>请先按分组上传首轮材料，确认无误后再开始生成事故信息；</span>
                  <span>每组最多 20 张图片、5 个视频；</span>
                  <span>生成前可随时删改。</span>
                </p>
              </div>
            </div>
            <div className="upload-workbench-toolbar-side">
              <div className="upload-workbench-toolbar-stats">
                <span>已整理 {pendingStats.activeGroupCount}/{pendingUploadGroups.length} 组</span>
                <span>图片 {pendingStats.totalImages}</span>
                <span>视频 {pendingStats.totalVideos}</span>
                <span>{formatSizeLimit(pendingStats.totalBytes || 0)}</span>
              </div>
              <div className="upload-workbench-action-row">
                <button
                  type="button"
                  className="upload-workbench-secondary"
                  onClick={handleCloseUploadWorkbench}
                  disabled={isGeneratingInput}
                >
                  <span className="upload-workbench-button-label">退出满屏</span>
                </button>
                <button
                  className="upload-workbench-primary"
                  type="button"
                  onClick={() => void handleGenerateInput()}
                  disabled={isGeneratingInput || !hasPendingUploads(pendingUploadGroups)}
                >
                  {isGeneratingInput ? (
                    <span className="upload-workbench-button-content">
                      <span className="spinner" />
                      <span className="upload-workbench-button-label">事故信息生成中</span>
                    </span>
                  ) : (
                    <span className="upload-workbench-button-label">生成事故信息</span>
                  )}
                </button>
              </div>
            </div>
          </div>
        ) : (
          <div className="upload-workbench-hero">
            <div className="upload-workbench-copy">
              <div className="upload-workbench-topline">
                <span className="upload-workbench-kicker">首传分组工作台</span>
                <span className="upload-workbench-mode-badge">{pendingUploadGroups.length} 个固定分组</span>
              </div>
              <div className="upload-workbench-title-block">
                <h3>事故资料分组整理</h3>
                <p>请先按事故概况、视频、现场、车损与隐私材料完成分组上传，系统会按当前分组与顺序生成事故信息草稿。</p>
              </div>
              <div className="upload-workbench-summary-grid">
                <div className="upload-workbench-summary-item">
                  <span>已整理</span>
                  <strong>{pendingStats.activeGroupCount}</strong>
                </div>
                <div className="upload-workbench-summary-item">
                  <span>材料数</span>
                  <strong>{totalBufferedItems}</strong>
                </div>
                <div className="upload-workbench-summary-item">
                  <span>图 / 视频</span>
                  <strong>{pendingStats.totalImages}/{pendingStats.totalVideos}</strong>
                </div>
                <div className="upload-workbench-summary-item">
                  <span>缓冲体积</span>
                  <strong>{formatSizeLimit(pendingStats.totalBytes || 0)}</strong>
                </div>
              </div>
            </div>
            <div className="upload-workbench-actions">
              <div className="upload-workbench-stats">
                <span className="upload-workbench-stats-label">当前整理状态</span>
                <strong>{formatPendingUploadStatLine(pendingUploadGroups)}</strong>
              </div>
              <div className="upload-workbench-action-row">
                <button
                  type="button"
                  className="upload-workbench-secondary"
                  onClick={handleOpenUploadWorkbench}
                  disabled={isGeneratingInput}
                >
                  <span className="upload-workbench-button-label">上传事故资料</span>
                </button>
                <button
                  className="upload-workbench-primary"
                  type="button"
                  onClick={() => void handleGenerateInput()}
                  disabled={isGeneratingInput || !hasPendingUploads(pendingUploadGroups)}
                >
                  {isGeneratingInput ? (
                    <span className="upload-workbench-button-content">
                      <span className="spinner" />
                      <span className="upload-workbench-button-label">事故信息生成中</span>
                    </span>
                  ) : (
                    <span className="upload-workbench-button-label">生成事故信息</span>
                  )}
                </button>
              </div>
                <div className="upload-workbench-guidelines">
                  <div className="upload-workbench-guideline">
                    <span>上传限制</span>
                    <strong>支持 JPG/PNG 图片与 MP4 视频，每组可多次追加。</strong>
                  </div>
                  <div className="upload-workbench-guideline">
                    <span>缓冲规则</span>
                    <strong>{`图片≤${formatSizeLimit(publicAppConfig.upload_limits.max_image_bytes)}，视频≤${formatSizeLimit(publicAppConfig.upload_limits.max_video_bytes)}，总量≤${formatSizeLimit(publicAppConfig.upload_limits.max_total_bytes)}。`}</strong>
                  </div>
                </div>
            </div>
          </div>
        )}
        {fullscreen && (
          <div className="upload-workbench-grid-shell">
            <div className="upload-workbench-boardhead">
              <div className="upload-workbench-boardcopy">
                <span className="upload-workbench-boardkicker">分组台面</span>
                <p>每个分组都可以多次追加。空分组会被自动跳过，已加入缓冲区的文件在生成前都可以删除。</p>
              </div>
              <span className="upload-workbench-boardmode">满屏整理模式</span>
            </div>
            <div className="upload-group-grid">
              {pendingUploadGroups.map((group) => {
                const imageCount = group.items.filter((item) => item.mediaType === "image").length;
                const videoCount = group.items.filter((item) => item.mediaType === "video").length;
                return (
                  <section key={group.id} className={`upload-group-panel ${group.items.length > 0 ? "has-files" : ""}`}>
                    <div className="upload-group-panel-top">
                      <div className="upload-group-heading">
                        <div className="upload-group-heading-topline">
                          <span className="upload-group-seq">分组 {group.sequence}</span>
                          <span className={`upload-group-state ${group.items.length > 0 ? "is-ready" : "is-empty"}`}>
                            {group.items.length > 0 ? "已缓冲" : "待添加"}
                          </span>
                        </div>
                        <h4 className={getUploadGroupTitleClassName(group.label)}>{group.label}</h4>
                        <p className={`upload-group-description ${group.subtitle ? "" : "is-blank"}`}>
                          {group.subtitle || "\u00A0"}
                        </p>
                      </div>
                      <button
                        type="button"
                        className="upload-group-trigger"
                        onClick={() => handleTriggerUploadClick(group.id)}
                        disabled={isGeneratingInput}
                      >
                        {group.items.length > 0 ? "继续添加" : "上传资料"}
                      </button>
                    </div>
                    <div className="upload-group-metrics">
                      <span>{imageCount} 张图片</span>
                      <span>{videoCount} 个视频</span>
                      <span>{formatSizeLimit(group.items.reduce((sum, item) => sum + item.sizeBytes, 0))}</span>
                    </div>
                    <div className="upload-group-stage">
                      {group.items.length > 0 ? (
                        <div className="upload-group-stage-filled">
                          <div className="upload-group-stage-head">
                            <span>缓冲区清单</span>
                            <strong>{group.items.length} 项</strong>
                          </div>
                          <div className="upload-buffer-list">
                            {group.items.map((item, index) => (
                              <div key={item.id} className="upload-buffer-item">
                                <div className="upload-buffer-copy">
                                  <span>{item.mediaType === "video" ? "视频" : "图片"} {index + 1}</span>
                                  <strong>{item.file.name}</strong>
                                  <p>{formatSizeLimit(item.sizeBytes)}</p>
                                </div>
                                <button
                                  type="button"
                                  className="upload-buffer-delete"
                                  onClick={() => handleRemovePendingFile(group.id, item.id)}
                                  disabled={isGeneratingInput}
                                >
                                  删除
                                </button>
                              </div>
                            ))}
                          </div>
                        </div>
                      ) : (
                        <div className="upload-group-empty">
                          <strong>当前暂无材料</strong>
                          <span>点击上方上传，将该类图片或视频加入缓冲区。</span>
                        </div>
                      )}
                    </div>
                  </section>
                );
              })}
            </div>
          </div>
        )}
      </div>
    );
  }

  function renderUploadWorkbenchDialog() {
    if (!shouldShowUploadWorkbench || !isUploadWorkbenchExpanded) {
      return null;
    }
    return (
      <div className="upload-workbench-overlay" onClick={handleCloseUploadWorkbench}>
        <div
          className="upload-workbench-shell"
          role="dialog"
          aria-modal="true"
          aria-label="上传工作台"
          onClick={(event) => event.stopPropagation()}
        >
          {renderUploadWorkbenchSurface(true)}
        </div>
      </div>
    );
  }

  function renderArtifactPreviewDialog() {
    if (!artifactPreview) {
      return null;
    }
    return (
      <div className="artifact-preview-overlay" onClick={handleCloseArtifactPreview}>
        <div className="artifact-preview-shell" onClick={(event) => event.stopPropagation()}>
          <div className="artifact-preview-header">
            <div>
              <span className="artifact-preview-kicker">本会话关联产物预览</span>
              <h3>{artifactPreview.detail?.label ?? "正在加载"}</h3>
              <p>{artifactPreview.detail?.summary || "点击后可查看该类中间产物的完整内容。"}</p>
            </div>
            <button type="button" className="artifact-preview-close" onClick={handleCloseArtifactPreview}>
              关闭
            </button>
          </div>
          {artifactPreview.loading ? (
            <div className="artifact-preview-loading">正在读取中间产物...</div>
          ) : artifactPreview.error ? (
            <div className="error-text">{artifactPreview.error}</div>
          ) : artifactPreview.detail ? (
            <div className="artifact-preview-layout">
              <aside className="artifact-preview-sidebar">
                {renderArtifactMeta(artifactPreview.detail.meta)}
              </aside>
              <div className="artifact-preview-body">
                {renderArtifactDetailBody(artifactPreview.detail)}
              </div>
            </div>
          ) : null}
        </div>
      </div>
    );
  }

  if (!isLoaded) {
    return (
      <div className={`app-container theme-${themeMode}`}>
        <main className="main-content">
          <header className="page-header">
            <div>
              <h1>道路交通事故分析</h1>
              <p>正在加载历史会话和关联文件记录...</p>
            </div>
            {renderThemeToggle()}
          </header>
        </main>
      </div>
    );
  }

  if (!activeSession) {
    return (
      <div className={`app-container theme-${themeMode}`}>
        <main className="main-content">
          <header className="page-header">
            <div>
              <h1>道路交通事故分析</h1>
              <p>正在准备会话工作区...</p>
            </div>
            {renderThemeToggle()}
          </header>
        </main>
      </div>
    );
  }

  return (
    <div className={`app-container theme-${themeMode}`}>
      {isMobileSidebarOpen && (
        <div className="mobile-overlay" onClick={() => setIsMobileSidebarOpen(false)} />
      )}
      <aside className={`sidebar ${isSidebarOpen ? "" : "collapsed"} ${isMobileSidebarOpen ? "mobile-open" : ""}`}>
        <div className="sidebar-header">
          {isSidebarOpen && (
            <div className="sidebar-logo">
              <img src="/logo.png" alt="Logo" onError={(e) => { e.currentTarget.style.display = 'none'; }} />
            </div>
          )}
          <button 
            className="toggle-sidebar-btn desktop-only" 
            onClick={() => setIsSidebarOpen(!isSidebarOpen)}
            title={isSidebarOpen ? "收起侧边栏" : "展开侧边栏"}
          >
            <SidebarIcon isOpen={isSidebarOpen} />
          </button>
          <button 
            className="toggle-sidebar-btn mobile-only" 
            onClick={() => setIsMobileSidebarOpen(false)}
            title="收起侧边栏"
          >
            <MenuIcon />
          </button>
        </div>

        <>
          <div className="sidebar-menu">
            <button className="sidebar-menu-btn" onClick={handleAddSession} title="新建对话">
              <ChatPlusIcon />
              {isSidebarOpen && <span>新建对话</span>}
            </button>
            
            <div className="search-container">
              <button 
                className="sidebar-menu-btn" 
                onClick={() => {
                  setIsSearchActive(!isSearchActive);
                  if (!isSidebarOpen) setIsSidebarOpen(true);
                }}
                title="搜索对话"
              >
                <SearchIcon />
                {isSidebarOpen && (
                  !isSearchActive ? (
                    <span>搜索对话</span>
                  ) : (
                    <input 
                      autoFocus
                      type="text" 
                      placeholder="搜索历史标题或内容..." 
                      value={searchQuery}
                      onChange={(e) => setSearchQuery(e.target.value)}
                      onClick={(e) => e.stopPropagation()}
                      className="sidebar-search-input"
                    />
                  )
                )}
              </button>
            </div>
          </div>

          {isSidebarOpen && (
            <>
              <div className="session-list-header">
                所有对话
              </div>

              <div className="session-list">
                {filteredSessions.map((session) => (
                  <div
                    key={session.id}
                    data-session-id={session.id}
                    className={`session-item-wrapper ${isMobileSidebarOpen && mobileActionMenuId === session.id ? "mobile-menu-open" : ""}`}
                  >
                    <div
                      className={`session-item ${session.id === activeSessionId ? "active" : ""} ${draggingSessionId === session.id ? "dragging" : ""} ${dragOverSessionId === session.id ? "drag-over" : ""}`}
                      onClick={() => handleSessionItemClick(session.id)}
                      draggable={!isMobileSidebarOpen && !searchQuery.trim() && editingSessionId !== session.id}
                      onDragStart={(event) => handleSessionDragStart(session.id, event)}
                      onDragOver={(event) => handleSessionDragOver(session.id, event)}
                      onDrop={(event) => void handleSessionDrop(session.id, event)}
                      onDragEnd={handleSessionDragEnd}
                      onTouchStart={(event) => handleMobileRenameTouchStart(session, event)}
                      onTouchMove={handleMobileRenameTouchMove}
                      onTouchEnd={(event) => handleMobileRenameTouchEnd(session.id, event)}
                      onTouchCancel={handleMobileRenameTouchCancel}
                      title={isMobileSidebarOpen ? "长按会话卡片可重命名" : searchQuery.trim() || editingSessionId === session.id ? "" : "拖放移动调整顺序"}
                    >
                      <div
                        className="session-info"
                        style={{ flex: 1, minWidth: 0, paddingRight: 8 }}
                      >
                        {editingSessionId === session.id ? (
                          <input
                            autoFocus
                            value={editTitle}
                            onChange={(e) => setEditTitle(e.target.value)}
                            onBlur={() => void handleFinishRename(session.id)}
                            onKeyDown={(e) => {
                              if (e.key === "Enter") void handleFinishRename(session.id);
                              if (e.key === "Escape") setEditingSessionId(null);
                            }}
                            onClick={(e) => e.stopPropagation()}
                            onMouseDown={(e) => e.stopPropagation()}
                            onPointerDown={(e) => e.stopPropagation()}
                            style={{
                              width: '100%',
                              padding: '2px 4px',
                              fontSize: '14px',
                              border: '1px solid var(--primary-color)',
                              borderRadius: '4px',
                              outline: 'none'
                            }}
                          />
                        ) : (
                          <span 
                            className="session-title" 
                            onDoubleClick={isMobileSidebarOpen ? undefined : (e) => handleStartRename(session, e)}
                            title={isMobileSidebarOpen ? "长按会话卡片可重命名" : "双击重命名"}
                          >
                            {session.title}
                          </span>
                        )}
                        {(() => {
                          const sessionArtifacts = normalizeLinkedArtifacts(session.linkedArtifacts);
                          if (sessionArtifacts.length === 0) {
                            return null;
                          }
                          return (
                            <div className="session-linked-files">
                              {sessionArtifacts.slice(0, 3).map((artifact) => (
                                <span
                                  key={`${session.id}-${artifact.category}`}
                                  className="session-linked-file-chip"
                                  title={artifact.summary}
                                >
                                  {formatSessionArtifactChipLabel(artifact.category, artifact.label)}
                                </span>
                              ))}
                              {sessionArtifacts.length > 3 && (
                                <span className="session-linked-file-more">
                                  +{sessionArtifacts.length - 3}
                                </span>
                              )}
                            </div>
                          );
                        })()}
                      </div>
                      <div className="session-item-right">
                        <span className="session-date">
                          {formatSessionCardDate(session.createdAt)}
                        </span>
                        {isSidebarOpen && (
                          <>
                            <button
                              className="pc-delete-btn"
                              onClick={async (e) => {
                                e.stopPropagation();
                                await handleDeleteSession(session.id);
                              }}
                              title="删除会话"
                            >
                              ×
                            </button>
                            <button
                              className="mobile-action-menu-btn mobile-only"
                              onClick={(e) => {
                                e.stopPropagation();
                                setMobileActionMenuId(mobileActionMenuId === session.id ? null : session.id);
                              }}
                            >
                              <MoreVertIcon />
                            </button>
                          </>
                        )}
                      </div>
                    </div>
                    {isMobileSidebarOpen && mobileActionMenuId === session.id && (
                      <div className="mobile-session-actions-bar mobile-only">
                        <button onClick={(e) => { e.stopPropagation(); moveSessionUp(session.id); }}>
                          向上移
                        </button>
                        <button onClick={(e) => { e.stopPropagation(); moveSessionDown(session.id); }}>
                          向下移
                        </button>
                        <button className="danger" onClick={async (e) => {
                          e.stopPropagation();
                          await handleDeleteSession(session.id);
                        }}>
                          删除
                        </button>
                      </div>
                    )}
                  </div>
                ))}
                {searchQuery && filteredSessions.length === 0 && (
                  <div style={{ padding: '20px', textAlign: 'center', color: '#94a3b8', fontSize: '14px' }}>
                    没有找到匹配的结果
                  </div>
                )}
              </div>
            </>
          )}
        </>

        <div className="sidebar-footer">
          <div className="header-account-menu-shell sidebar-account-menu-shell">
            <button
              ref={accountMenuTriggerRef}
              type="button"
              className={`sidebar-account-chip sidebar-account-trigger ${isAccountMenuOpen ? "is-open" : ""}`}
              onClick={() => setIsAccountMenuOpen((current) => !current)}
              aria-label="打开账号菜单"
              title="打开账号菜单"
            >
              <span className="brand-avatar">{currentUser.username.slice(0, 1).toUpperCase()}</span>
              {isSidebarOpen && (
                <div className="sidebar-account-copy">
                  <strong>{isAdminUser ? currentUser.username : (currentUser.display_name || currentUser.username)}</strong>
                  <span>{isAdminUser ? "管理员账号" : currentUser.username}</span>
                </div>
              )}
            </button>
            {renderAccountMenu()}
          </div>
        </div>
      </aside>

      <main className="main-content">
        <header className={`page-header ${appView === "admin" ? "is-admin-header" : ""}`}>
          <div className="page-header-title-group">
            <button
              className="mobile-menu-btn"
              onClick={() => setIsMobileSidebarOpen(true)}
              title="打开侧边栏"
              aria-label="打开侧边栏"
            >
              <MenuIcon />
            </button>
            {appView === "admin" ? (
              renderAdminHeaderTabs()
            ) : (
              <div>
                <h1>道路交通事故分析</h1>
                <p>智能识别道路交通事故照片/视频，辅助生成带有定责意见与研判论述的分析报告</p>
              </div>
            )}
          </div>
          <div className="page-header-actions">
            {renderHeaderAdminToggle()}
            {renderThemeToggle()}
          </div>
        </header>
        {appView === "admin" && isAdminUser ? (
          <AdminConsole currentUser={currentUser} activeTab={adminTab} />
        ) : (
          <>
            <div className="mobile-tabs mobile-only">
              <button 
                className={`mobile-tab-btn ${mobileTab === 'chat' ? 'active' : ''}`}
                onClick={() => setMobileTab('chat')}
              >
                对话交互
              </button>
              <button 
                className={`mobile-tab-btn ${mobileTab === 'review' ? 'active' : ''}`}
                onClick={() => setMobileTab('review')}
              >
                分析与审阅
              </button>
            </div>

            <div className="workspace workspace-grid">
              <section className={`panel ${mobileTab !== 'chat' ? 'mobile-hidden' : ''}`} style={{ flex: 1 }}>
                <div className="panel-header">
                  <h2>聊天与记录</h2>
                </div>
                <div className="panel-body chat-list" ref={chatListRef}>
                  {activeSession.messages.map((message, messageIndex) => (
                    <div
                      key={`${message.id}-${messageIndex}`}
                      className={`chat-bubble ${message.role} ${message.kind === "progress" ? "progress-bubble" : ""}`}
                    >
                      {message.kind === "progress" ? (
                        <div className={`progress-card ${message.meta?.status || "running"}`}>
                          <div className="progress-card-topline">
                            {message.meta?.badge && <span className="progress-badge">{message.meta.badge}</span>}
                            <div className="progress-card-status">
                              {message.meta?.status === "running" ? (
                                <span className="spinner spinner-dark" />
                              ) : (
                                <span className={`progress-indicator ${message.meta?.status || "running"}`}>
                                  {message.meta?.status === "success" ? "✓" : message.meta?.status === "error" ? "!" : ""}
                                </span>
                              )}
                            </div>
                          </div>
                          <div className="progress-card-main">
                            <strong className="progress-card-title">{message.meta?.title || "处理中"}</strong>
                            <p className="progress-card-content">{message.content}</p>
                          </div>
                          {message.meta?.stages?.length ? (
                            <div className="progress-stage-list">
                              {message.meta.stages.map((stage) => (
                                <div key={`${message.id}-${stage.label}`} className={`progress-stage-item ${stage.state}`}>
                                  <span className="progress-stage-dot" />
                                  <span>{stage.label}</span>
                                </div>
                              ))}
                            </div>
                          ) : null}
                        </div>
                      ) : message.kind === "markdown" ? (
                        <div className="markdown-report markdown-report-compact">
                          <ReactMarkdown remarkPlugins={[remarkGfm]} components={reportMarkdownComponents}>
                            {normalizeMarkdownForDisplay(message.content)}
                          </ReactMarkdown>
                        </div>
                      ) : message.kind === "json" ? (
                        <pre style={{ fontSize: '12px', margin: 0 }}>{message.content}</pre>
                      ) : (
                        <p style={{ margin: 0 }}>{message.content}</p>
                      )}
                    </div>
                  ))}
                </div>
              </section>

              <section className={`panel ${mobileTab !== 'review' ? 'mobile-hidden' : ''}`} style={{ flex: 1.2 }}>
                <div className="panel-header">
                  <h2>操作与审阅区</h2>
                </div>
                <div className="panel-body">
                  {errorMessage && <div className="error-text">{errorMessage}</div>}
                  {uploadNoticeMessage && <div className="warning-text">{uploadNoticeMessage}</div>}
                  {syncError && <div className="error-text">会话记录同步提醒：{syncError}</div>}

                  {activeLinkedArtifacts.length > 0 && (
                    <div className="artifact-wall-panel">
                      {renderBrandWatermark("artifact-wall-watermark")}
                      <div className="artifact-wall-header">
                        <h3>本会话关联文件</h3>
                        <span>{activeLinkedArtifacts.length} 项</span>
                      </div>
                      <div className="artifact-wall-grid">
                        {activeLinkedArtifacts.map((artifact) => (
                          <article
                            key={artifact.category}
                            className="artifact-wall-card"
                            role="button"
                            tabIndex={0}
                            aria-label={`预览${artifact.label}`}
                            onClick={() => void handleOpenArtifactPreview(artifact.category)}
                            onKeyDown={(event) => handleArtifactCardKeyDown(event, artifact.category)}
                          >
                            <span className="artifact-wall-kicker">{artifact.kind}</span>
                            <strong>{artifact.label}</strong>
                            <p>{artifact.summary || "点击查看完整中间产物。"}</p>
                            <div className="artifact-wall-footer">
                              <span>{artifact.item_count} 项</span>
                              <span>点击预览</span>
                            </div>
                          </article>
                        ))}
                      </div>
                    </div>
                  )}

                  <input
                    ref={fileInputRef}
                    className="upload-input-hidden"
                    type="file"
                    multiple
                    accept=".png,.jpg,.jpeg,.webp,.bmp,.gif,.heic,.heif,.mp4,.mov,.avi,.mkv,.webm,.m4v,image/*,video/*"
                    onChange={handleFileChange}
                  />

                  {shouldShowUploadWorkbench && renderUploadWorkbenchSurface()}

                  {activeSession.draftJson && !activeSession.reportResult && (
                    <div>
                       <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 12 }}>
                          <h3 style={{ margin: 0, fontSize: '16px', color: '#0f172a' }}>事故属性摘要表（可直接点击编辑）</h3>
                          {activeSession.draftMeta?.media_type && (
                            <span className="tag">
                              {activeSession.draftMeta.media_type === "video"
                                ? "📹 视频识别"
                                : activeSession.draftMeta.media_type === "mixed"
                                  ? "🧩 多源识别"
                                  : "🖼️ 照片识别"}
                            </span>
                          )}
                       </div>
                       <JsonTableEditor 
                          initialJson={activeSession.draftJson}
                          onAutoSave={handleAutoSaveDraft}
                          onConfirm={handleConfirmAndGenerateReport}
                          disabled={isGeneratingReport}
                          isGeneratingReport={isGeneratingReport && activeSession.id === reportingSessionId}
                          onCancelGenerate={handleStopReportGeneration}
                       />
                    </div>
                  )}

                  {activeSession.reportResult && (
                    <div>
                  <div className="report-export-ribbon">
                    <div className="report-export-ribbon-top">
                      <div className="report-export-heading">
                        <span className="report-export-kicker">
                          <ExportRibbonIcon />
                          文书级导出工具带
                        </span>
                        <h3>报告导出与下载</h3>
                        <p>当前报告已写入本地输出目录，可直接下载 `report.md`，也可生成更适合流转与归档的 Word 版本，或先编排 PDF 封面后再输出固定版文书。</p>
                      </div>
                      <div className="report-export-status">
                        <span className="report-export-status-label">当前 trace_id</span>
                        <strong>{activeSession.reportResult.trace_id}</strong>
                      </div>
                    </div>
                    <div className="report-export-actions">
                      {REPORT_EXPORT_ACTIONS.map((action) => {
                        const isBusy = exportingFormat === action.format;
                        const isDisabled = Boolean(exportingFormat) || isGeneratingReport;
                        const isReady = activeSession.linkedFiles.some(
                          (file) => file.category === action.category && file.exists,
                        );
                        const isStudioActive = action.format === "pdf" && isPdfStudioOpen;
                        return (
                          <button
                            key={action.format}
                            type="button"
                            className={`report-export-card ${isBusy ? "is-loading" : ""} ${isStudioActive ? "is-active" : ""}`}
                            disabled={isDisabled}
                            onClick={() => void handleExportActionClick(action.format)}
                          >
                            <span className="report-export-card-kicker">{action.kicker}</span>
                            <strong>{action.label}</strong>
                            <span className="report-export-card-description">{action.description}</span>
                            <span className="report-export-card-meta">
                              {isBusy
                                ? "正在准备下载..."
                                : isStudioActive
                                  ? "封面编排台已展开"
                                : isReady
                                  ? "已生成，可直接重复下载"
                                  : action.pendingHint}
                            </span>
                          </button>
                        );
                      })}
                    </div>
                    {isPdfStudioOpen && (
                      <div className="report-pdf-studio">
                        <div className="report-pdf-studio-header">
                          <div className="report-pdf-studio-copy">
                            <span className="report-pdf-studio-kicker">PDF 封面编排台</span>
                            <h4>先定义封面信息，再导出正式归档版</h4>
                            <p>这份设置只作用于本次 PDF 导出。标题与日期进入封面，目录和正文仍自动沿用当前报告内容。</p>
                          </div>
                          <div className="report-pdf-studio-header-actions">
                            <button type="button" className="report-pdf-link-btn" onClick={handleResetPdfCoverDraft}>
                              恢复默认
                            </button>
                            <button type="button" className="report-pdf-link-btn" onClick={handleClosePdfStudio}>
                              收起
                            </button>
                          </div>
                        </div>
                        <div className="report-pdf-studio-layout">
                          <div className="report-pdf-form">
                            <div className="report-pdf-field">
                              <label htmlFor="pdf-cover-title">封面标题</label>
                              <input
                                id="pdf-cover-title"
                                type="text"
                                value={pdfCoverDraft.title}
                                maxLength={48}
                                onChange={(event) => setPdfCoverDraft((current) => ({ ...current, title: event.target.value }))}
                                placeholder="请输入 PDF 封面标题"
                              />
                            </div>
                            <div className="report-pdf-field">
                              <label htmlFor="pdf-cover-subtitle">封面副标题</label>
                              <input
                                id="pdf-cover-subtitle"
                                type="text"
                                value={pdfCoverDraft.subtitle}
                                maxLength={64}
                                onChange={(event) => setPdfCoverDraft((current) => ({ ...current, subtitle: event.target.value }))}
                                placeholder="例如：桥区雨天事故责任分析专报"
                              />
                            </div>
                            <div className="report-pdf-field">
                              <label htmlFor="pdf-cover-compiled-by">编制人</label>
                              <input
                                id="pdf-cover-compiled-by"
                                type="text"
                                value={pdfCoverDraft.compiledBy}
                                maxLength={48}
                                onChange={(event) => setPdfCoverDraft((current) => ({ ...current, compiledBy: event.target.value }))}
                                placeholder="请输入 PDF 编制人"
                              />
                            </div>
                            <div className="report-pdf-field">
                              <label>日期呈现</label>
                              <div className="report-pdf-date-toggle">
                                {[
                                  { value: "today", label: "使用今天" },
                                  { value: "custom", label: "自定义日期" },
                                  { value: "hide", label: "不显示日期" },
                                ].map((option) => (
                                  <button
                                    key={option.value}
                                    type="button"
                                    className={`report-pdf-date-pill ${pdfCoverDraft.dateMode === option.value ? "is-active" : ""}`}
                                    onClick={() => setPdfCoverDraft((current) => ({ ...current, dateMode: option.value as PdfCoverDateMode }))}
                                  >
                                    {option.label}
                                  </button>
                                ))}
                              </div>
                            </div>
                            {pdfCoverDraft.dateMode === "custom" && (
                              <div className="report-pdf-field">
                                <label htmlFor="pdf-cover-date-text">封面日期文本</label>
                                <input
                                  id="pdf-cover-date-text"
                                  type="text"
                                  value={pdfCoverDraft.dateText}
                                  maxLength={32}
                                  onChange={(event) => setPdfCoverDraft((current) => ({ ...current, dateText: event.target.value }))}
                                  placeholder="例如：2026年03月25日"
                                />
                              </div>
                            )}
                            <div className="report-pdf-form-actions">
                              <button
                                type="button"
                                className="report-pdf-toolbar-btn report-pdf-toolbar-btn-primary"
                                onClick={() => void handleConfirmPdfExport()}
                                disabled={exportingFormat === "pdf"}
                              >
                                {exportingFormat === "pdf" ? "正在导出 PDF..." : "按当前封面导出"}
                              </button>
                              <button
                                type="button"
                                className="report-pdf-toolbar-btn report-pdf-toolbar-btn-secondary"
                                onClick={handleClosePdfStudio}
                                disabled={Boolean(exportingFormat) || isGeneratingReport}
                              >
                                暂不导出
                              </button>
                            </div>
                          </div>
                          <div className="report-pdf-preview">
                            <div className="report-pdf-preview-sheet">
                              <div className="report-pdf-preview-topline">
                                <span>锐鉴安途事故分析文书</span>
                                <span>{activeSession.reportResult.trace_id}</span>
                              </div>
                              <div className="report-pdf-preview-hero">
                                <span className="report-pdf-preview-brand">{PDF_COMPILED_BY}</span>
                                <h5>{pdfCoverDraft.title.trim() || PDF_COVER_TITLE}</h5>
                                <p>{pdfCoverDraft.subtitle.trim() || PDF_COVER_SUBTITLE}</p>
                              </div>
                              <div className="report-pdf-preview-meta">
                                <div>
                                  <span>编制人</span>
                                  <strong>{resolvePdfCoverCompiledBy(pdfCoverDraft)}</strong>
                                </div>
                                {resolvePdfCoverPreviewDate(pdfCoverDraft) && (
                                  <div>
                                    <span>编制日期</span>
                                    <strong>{resolvePdfCoverPreviewDate(pdfCoverDraft)}</strong>
                                  </div>
                                )}
                                <div>
                                  <span>报告编号</span>
                                  <strong>{activeSession.reportResult.trace_id}</strong>
                                </div>
                              </div>
                            </div>
                          </div>
                        </div>
                      </div>
                    )}
                    <p className="report-export-footnote">Word 会清洗 Markdown 标记并优化段落样式；PDF 可先配置封面标题与日期，再由后端按当前设置渲染封面、目录和正文。</p>
                  </div>

                  <div style={{ marginBottom: 24 }}>
                    <h3 className="content-section-title">专家指导意见</h3>
                    <div className="guidance-cards">
                      {Object.entries(activeSession.reportResult.guidance).map(([key, val]) => {
                        const isString = typeof val === "string";
                        return (
                          <div key={key} className="info-card">
                             <h4>{key}</h4>
                             {isString ? <p>{val}</p> : <pre>{JSON.stringify(val, null, 2)}</pre>}
                          </div>
                        )
                      })}
                    </div>
                  </div>

                  <div>
                    <h3 className="content-section-title">分析研判报告正文</h3>
                    <div className="report-surface markdown-report">
                      {(() => {
                        const normalized = normalizeMarkdownForDisplay(activeSession.reportResult.report.report_markdown);
                        return normalized ? (
                          <ReactMarkdown remarkPlugins={[remarkGfm]} components={reportMarkdownComponents}>
                            {normalized}
                          </ReactMarkdown>
                        ) : (
                          <p style={{ margin: 0, color: "#8a8a8a" }}>暂无报告内容。</p>
                        );
                      })()}
                    </div>
                  </div>
                </div>
              )}
                </div>
              </section>
            </div>
          </>
        )}
      </main>
      {renderUploadWorkbenchDialog()}
      {renderArtifactPreviewDialog()}
      <UserModelConfigDrawer
        open={isUserModelDrawerOpen}
        saving={isSavingUserModelConfig}
        state={userModelConfigState}
        errorMessage={userModelConfigError}
        onClose={() => setIsUserModelDrawerOpen(false)}
        onSave={handleSaveUserModelConfig}
      />
    </div>
  );
}
