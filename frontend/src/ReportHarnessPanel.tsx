import { forwardRef, useCallback, useEffect, useImperativeHandle, useRef, useState } from "react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";

import {
  ApiError,
  authorizeReportRun,
  cancelReportRun,
  createReportRun,
  downloadReportRunExport,
  executeReportRunStream,
  fetchAuthorizationPreview,
  fetchReportEvidence,
  fetchReportRun,
  fetchReportRunCandidate,
  fetchReportRunEvents,
  formatApiErrorMessage,
  listReportRuns,
  resumeReportRunStream,
  saveReportEvidence,
} from "./api";
import { JsonTableEditor } from "./JsonTableEditor";
import type {
  AuthorizeReportRunPayload,
  ReportAuthorizationPreview,
  ReportEvidenceRecord,
  ReportExportFormat,
  ReportExportMode,
  ReportFieldConflict,
  ReportRunCandidate,
  ReportRunEvent,
  ReportRunView,
} from "./types";
import { normalizeMarkdownForDisplay } from "./markdown";

const RUN_STATUS_LABELS: Record<string, string> = {
  queued: "待执行",
  active: "执行中",
  preparing: "准备中",
  generating: "生成中",
  checking: "审查中",
  revising: "修订中",
  needs_review: "需人工复核",
  revision_required: "待修订",
  suspended: "已暂停",
  published: "已发布",
  cancelled: "已取消",
  failed: "失败",
  budget_exhausted: "预算耗尽",
};

export const RUN_TERMINAL_STATES = new Set(["published", "needs_review", "cancelled", "failed", "budget_exhausted"]);

const EVIDENCE_KIND_LABELS: Record<ReportEvidenceRecord["kind"], string> = {
  observation: "现场观察",
  statement: "当事人陈述",
  document_excerpt: "文书摘录",
  other: "其他",
};

const VERIFICATION_STATUS_LABELS: Record<ReportEvidenceRecord["verification_status"], string> = {
  unverified: "待核实",
  human_confirmed: "人工已核实",
  disputed: "存在争议",
};

const REPORT_RUN_EVENT_LIMIT = 100;

export function createEvidenceId(): string {
  if (typeof crypto !== "undefined" && typeof crypto.randomUUID === "function") {
    return crypto.randomUUID();
  }
  return "xxxxxxxx-xxxx-4xxx-yxxx-xxxxxxxxxxxx".replace(/[xy]/g, (character) => {
    const random = Math.floor(Math.random() * 16);
    const value = character === "x" ? random : (random & 0x3) | 0x8;
    return value.toString(16);
  });
}

export function createEmptyEvidenceRecord(): ReportEvidenceRecord {
  return {
    evidence_id: createEvidenceId(),
    text: "",
    source_label: "",
    source_locator: "",
    kind: "observation",
    verification_status: "unverified",
    conflicts_with: [],
    verification_note: "",
    field_conflicts: [],
  };
}

export function parseAccidentData(json: string): Record<string, unknown> {
  let parsed: unknown;
  try {
    parsed = JSON.parse(json);
  } catch {
    throw new Error("事故输入不是有效 JSON。");
  }
  if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) {
    throw new Error("事故输入必须是非数组 JSON 对象。");
  }
  if (Object.keys(parsed as Record<string, unknown>).length === 0) {
    throw new Error("事故输入不能为空对象。");
  }
  return parsed as Record<string, unknown>;
}

export async function createReportRunAfterDraftSave<T>(
  persistDraft: () => Promise<void>,
  createRun: () => Promise<T>,
): Promise<T> {
  await persistDraft();
  return createRun();
}

export function buildAuthorizationPayload(
  preview: ReportAuthorizationPreview,
): AuthorizeReportRunPayload {
  return {
    snapshot_digest: preview.snapshot_digest,
    endpoint_profile_digest: preview.endpoint_profile_digest,
    approved_knowledge_manifest_digest: preview.approved_knowledge_manifest_digest,
    confirmed: true,
  };
}

export function canExecuteReportRun(run: ReportRunView, authorized: boolean): boolean {
  if (run.state !== "queued") {
    return false;
  }
  return run.execution_profile === "synthetic_test" || authorized;
}

/** 正式导出看批准资格；工程导出以后端导出策略给出的 export_kind 为准。 */
export function canExportReportRun(run: ReportRunView, mode: ReportExportMode): boolean {
  if (mode === "formal") {
    return run.state === "published" && run.formal_export_eligible === true;
  }
  // 旧后端没有 export_kind 字段时，保持“仅已发布可导出”的原规则。
  return run.export_kind !== undefined ? Boolean(run.export_kind) : run.state === "published";
}

export function toggleEvidenceConflict(
  record: ReportEvidenceRecord,
  targetEvidenceId: string,
  checked: boolean,
): ReportEvidenceRecord {
  const conflicts = new Set(record.conflicts_with);
  if (checked) {
    conflicts.add(targetEvidenceId);
  } else {
    conflicts.delete(targetEvidenceId);
  }
  return { ...record, conflicts_with: [...conflicts] };
}

export function updateEvidenceFieldConflict(
  record: ReportEvidenceRecord,
  index: number,
  patch: Partial<ReportFieldConflict>,
): ReportEvidenceRecord {
  return {
    ...record,
    field_conflicts: record.field_conflicts.map((item, itemIndex) => (
      itemIndex === index ? { ...item, ...patch } : item
    )),
  };
}

export function formatRunState(state: string): string {
  return RUN_STATUS_LABELS[state] ?? state;
}

function formatDate(value: string | null | undefined): string {
  if (!value) {
    return "--";
  }
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? value : date.toLocaleString("zh-CN");
}

function formatBudgetValue(value: unknown): string {
  if (typeof value === "number") {
    return new Intl.NumberFormat("zh-CN").format(value);
  }
  if (typeof value === "string" && value.trim()) {
    return value;
  }
  return "--";
}

function executionProfileLabel(profile: string | undefined): string {
  return profile === "synthetic_test" ? "离线工程验证" : "外发运行";
}

function qualityGateLabel(_gate: string, formalExportEligible = false): string {
  return formalExportEligible ? "可正式导出" : "未获正式导出资格";
}

function reviewStatusLabel(status: string): string {
  const labels: Record<string, string> = {
    pending: "待审查",
    passed: "已通过",
    revision_required: "待修订",
    failed: "未通过",
  };
  return labels[status] ?? "待更新";
}

function issueSeverityLabel(severity: string): string {
  const labels: Record<string, string> = {
    blocker: "阻断",
    major: "主要",
    minor: "一般",
  };
  return labels[severity] ?? "一般";
}

function issueStatusLabel(status: string): string {
  const labels: Record<string, string> = {
    open: "待处理",
    resolved: "已处理",
    contested: "有争议",
  };
  return labels[status] ?? "待更新";
}

function authorizationReasonLabel(reason: string | null): string {
  if (reason === "authorization_profile_unavailable") {
    return "当前没有可用的外发配置。";
  }
  return "当前外发配置不可用。";
}

function endpointRoleLabel(role: string): string {
  const labels: Record<string, string> = {
    expert: "专家",
    generator: "生成",
    reviewer: "审查",
    embedding: "检索",
  };
  return labels[role] ?? "端点";
}

function eventTypeLabel(type: string): string {
  const labels: Record<string, string> = {
    stage: "阶段",
    tool: "工具",
    request: "请求",
    review: "审查",
    budget: "预算",
    checkpoint: "检查点",
    final: "完成",
    error: "错误",
  };
  return labels[type] ?? "事件";
}

function isAbortError(error: unknown): boolean {
  return Boolean(
    error
    && typeof error === "object"
    && "name" in error
    && (error as { name?: unknown }).name === "AbortError",
  );
}

function upsertRun(runs: ReportRunView[], nextRun: ReportRunView, preferFirst = false): ReportRunView[] {
  const index = runs.findIndex((run) => run.run_id === nextRun.run_id);
  if (index >= 0) {
    return runs.map((run, runIndex) => (runIndex === index ? nextRun : run));
  }
  return preferFirst ? [nextRun, ...runs] : [...runs, nextRun];
}

function isApiNotFound(error: unknown): boolean {
  return error instanceof ApiError && error.status === 404;
}

export interface ReportHarnessPanelHandle {
  flushDraft: (expectedSessionId?: string) => Promise<void>;
  isTransitionBlocked: () => boolean;
}

type ReportHarnessPanelProps = {
  sessionId: string;
  initialDraftJson: string;
  onPersistDraft: (sessionId: string, json: string) => Promise<void>;
};

export const ReportHarnessPanel = forwardRef<ReportHarnessPanelHandle, ReportHarnessPanelProps>(function ReportHarnessPanel(
  { sessionId, initialDraftJson, onPersistDraft },
  ref,
) {
  const normalizedInitialDraftJson = initialDraftJson.trim() || "{}";
  const [accidentJson, setAccidentJson] = useState(normalizedInitialDraftJson);
  const [evidenceRevision, setEvidenceRevision] = useState(0);
  const [evidenceRecords, setEvidenceRecords] = useState<ReportEvidenceRecord[]>([]);
  const [evidenceDirty, setEvidenceDirty] = useState(false);
  const [runs, setRuns] = useState<ReportRunView[]>([]);
  const [runCursor, setRunCursor] = useState<string | null>(null);
  const [selectedRunId, setSelectedRunId] = useState<string | null>(null);
  const [selectedRun, setSelectedRun] = useState<ReportRunView | null>(null);
  const [candidate, setCandidate] = useState<ReportRunCandidate | null>(null);
  const [preview, setPreview] = useState<ReportAuthorizationPreview | null>(null);
  const [previewConfirmed, setPreviewConfirmed] = useState(false);
  const [authorizedRunIds, setAuthorizedRunIds] = useState<Record<string, boolean>>({});
  const [events, setEvents] = useState<ReportRunEvent[]>([]);
  const [eventCursor, setEventCursor] = useState(0);
  const [loading, setLoading] = useState(true);
  const [runsLoading, setRunsLoading] = useState(false);
  const [runLoading, setRunLoading] = useState(false);
  const [eventsLoading, setEventsLoading] = useState(false);
  const [savingEvidence, setSavingEvidence] = useState(false);
  const [creatingRun, setCreatingRun] = useState(false);
  const [authorizing, setAuthorizing] = useState(false);
  const [streamingAction, setStreamingAction] = useState<"execute" | "resume" | null>(null);
  const [exporting, setExporting] = useState<string | null>(null);
  const [retryUnknownRequests, setRetryUnknownRequests] = useState(false);
  const [errorMessage, setErrorMessage] = useState("");
  const [noticeMessage, setNoticeMessage] = useState("");
  const streamAbortControllerRef = useRef<AbortController | null>(null);
  const streamRunIdRef = useRef<string | null>(null);
  const loadGenerationRef = useRef(0);
  const runCursorRef = useRef<string | null>(null);
  const selectedRunIdRef = useRef<string | null>(null);
  const sessionIdRef = useRef(sessionId);
  const draftSessionIdRef = useRef(sessionId);
  const accidentJsonRef = useRef(normalizedInitialDraftJson);
  const savedAccidentJsonRef = useRef(normalizedInitialDraftJson);
  const draftSaveChainRef = useRef<Promise<void>>(Promise.resolve());
  sessionIdRef.current = sessionId;
  selectedRunIdRef.current = selectedRunId;

  const hasDraft = Boolean(initialDraftJson.trim());
  const selectedIssues = candidate?.review_result?.issues ?? [];
  const selectedRunProfile = selectedRun?.execution_profile ?? "outbound";
  const isSelectedRunAuthorized = Boolean(selectedRun && authorizedRunIds[selectedRun.run_id]);
  const executeAllowed = Boolean(selectedRun && canExecuteReportRun(selectedRun, isSelectedRunAuthorized));
  const formalExportAllowed = Boolean(selectedRun && canExportReportRun(selectedRun, "formal"));
  const engineeringExportAllowed = Boolean(selectedRun && canExportReportRun(selectedRun, "engineering"));

  const updateAccidentJson = useCallback((nextJson: string) => {
    accidentJsonRef.current = nextJson;
    setAccidentJson(nextJson);
    setErrorMessage("");
  }, []);

  const persistDraft = useCallback(
    async (json: string) => {
      const previousSave = draftSaveChainRef.current;
      const nextSave = previousSave
        .catch(() => undefined)
        .then(async () => {
          if (sessionIdRef.current !== sessionId) {
            throw new Error("会话已切换，已停止保存原会话编辑。");
          }
          parseAccidentData(json);
          await onPersistDraft(sessionId, json);
          if (sessionIdRef.current === sessionId) {
            savedAccidentJsonRef.current = json;
          }
        });
      draftSaveChainRef.current = nextSave.catch(() => undefined);
      return nextSave;
    },
    [onPersistDraft, sessionId],
  );

  useImperativeHandle(
    ref,
    () => ({
      isTransitionBlocked: () => Boolean(
        streamAbortControllerRef.current || savingEvidence || creatingRun || authorizing
        || (selectedRun && !RUN_TERMINAL_STATES.has(selectedRun.state)
          && selectedRun.state !== "queued" && selectedRun.state !== "suspended"),
      ),
      flushDraft: async (expectedSessionId) => {
        if (expectedSessionId && sessionIdRef.current !== expectedSessionId) {
          throw new Error("当前编辑不属于请求离开的会话。");
        }
        await draftSaveChainRef.current;
        if (expectedSessionId && sessionIdRef.current !== expectedSessionId) {
          throw new Error("会话已切换，未继续保存原会话编辑。");
        }
        const currentJson = accidentJsonRef.current;
        if (currentJson === savedAccidentJsonRef.current) {
          return;
        }
        try {
          await persistDraft(currentJson);
        } catch (error) {
          setErrorMessage(formatApiErrorMessage(error, "保存事故输入失败，未切换会话。"));
          throw error;
        }
      },
    }),
    [persistDraft, savingEvidence, creatingRun, authorizing, selectedRun],
  );

  const loadWorkspace = useCallback(async (append = false) => {
    const generation = ++loadGenerationRef.current;
    if (!append) {
      setLoading(true);
      setErrorMessage("");
    } else {
      setRunsLoading(true);
    }
    try {
      const evidenceResponse = append
        ? null
        : await fetchReportEvidence(sessionId);
      const page = await listReportRuns(sessionId, {
        limit: 20,
        cursor: append ? runCursorRef.current : null,
      });
      if (generation !== loadGenerationRef.current) {
        return;
      }
      if (evidenceResponse) {
        setEvidenceRevision(evidenceResponse.revision);
        setEvidenceRecords(evidenceResponse.records ?? []);
        setEvidenceDirty(false);
      }
      setRuns((current) => append ? [...current, ...(page.runs ?? [])] : page.runs ?? []);
      runCursorRef.current = page.next_cursor ?? null;
      setRunCursor(runCursorRef.current);
      setSelectedRunId((current) => {
        if (current && (page.runs ?? []).some((run) => run.run_id === current)) {
          return current;
        }
        if (append) {
          return current;
        }
        return page.runs?.[0]?.run_id ?? null;
      });
      if (!page.runs?.length && !append) {
        setSelectedRunId(null);
      }
    } catch (error) {
      if (generation === loadGenerationRef.current) {
        setErrorMessage(formatApiErrorMessage(error, "读取证据或运行列表失败。"));
      }
    } finally {
      if (generation === loadGenerationRef.current) {
        setLoading(false);
        setRunsLoading(false);
      }
    }
  }, [sessionId]);

  useEffect(() => {
    if (draftSessionIdRef.current === sessionId) {
      return;
    }
    draftSessionIdRef.current = sessionId;
    const nextDraftJson = initialDraftJson.trim() || "{}";
    accidentJsonRef.current = nextDraftJson;
    savedAccidentJsonRef.current = nextDraftJson;
    draftSaveChainRef.current = Promise.resolve();
    setAccidentJson(nextDraftJson);
  }, [initialDraftJson, sessionId]);

  useEffect(() => {
    streamAbortControllerRef.current?.abort();
    streamAbortControllerRef.current = null;
    setEvidenceRevision(0);
    setEvidenceRecords([]);
    setEvidenceDirty(false);
    setRuns([]);
    setRunCursor(null);
    runCursorRef.current = null;
    setSelectedRunId(null);
    setSelectedRun(null);
    setCandidate(null);
    setPreview(null);
    setPreviewConfirmed(false);
    setAuthorizedRunIds({});
    setEvents([]);
    setEventCursor(0);
    setRetryUnknownRequests(false);
    void loadWorkspace(false);
    return () => {
      loadGenerationRef.current += 1;
      streamAbortControllerRef.current?.abort();
      streamAbortControllerRef.current = null;
    };
  }, [loadWorkspace, sessionId]);

  useEffect(() => {
    if (draftSessionIdRef.current !== sessionId || accidentJsonRef.current !== savedAccidentJsonRef.current) {
      return;
    }
    const nextDraftJson = initialDraftJson.trim() || "{}";
    if (nextDraftJson !== accidentJsonRef.current) {
      accidentJsonRef.current = nextDraftJson;
      savedAccidentJsonRef.current = nextDraftJson;
      setAccidentJson(nextDraftJson);
    }
  }, [initialDraftJson, sessionId]);

  useEffect(() => {
    if (!selectedRunId) {
      setSelectedRun(null);
      setCandidate(null);
      setPreview(null);
      setEvents([]);
      setEventCursor(0);
      return;
    }

    const runId = selectedRunId;
    let cancelled = false;
    setSelectedRun(null);
    setRunLoading(true);
    setErrorMessage("");
    setNoticeMessage("");
    setPreview(null);
    setPreviewConfirmed(false);
    setCandidate(null);
    setEvents([]);
    setEventCursor(0);

    async function loadRunDetails() {
      try {
        const run = await fetchReportRun(runId);
        if (cancelled) {
          return;
        }
        setSelectedRun(run);
        setRuns((current) => upsertRun(current, run));

        try {
          const nextCandidate = await fetchReportRunCandidate(runId);
          if (!cancelled) setCandidate(nextCandidate);
        } catch (error) {
          if (!isApiNotFound(error)) {
            throw error;
          }
        }

        try {
          const eventPage = await fetchReportRunEvents(runId, {
            afterSeq: 0,
            limit: REPORT_RUN_EVENT_LIMIT,
          });
          if (!cancelled) {
            setEvents(eventPage.events ?? []);
            setEventCursor(eventPage.next_seq ?? 0);
          }
        } catch (error) {
          if (!cancelled) {
            setErrorMessage(formatApiErrorMessage(error, "读取运行轨迹失败。"));
          }
        }
      } catch (error) {
        if (!cancelled) {
          setErrorMessage(formatApiErrorMessage(error, "读取运行状态失败。"));
        }
      } finally {
        if (!cancelled) {
          setRunLoading(false);
        }
      }
    }

    void loadRunDetails();
    return () => {
      cancelled = true;
    };
  }, [selectedRunId]);

  useEffect(() => {
    const handlePageHide = () => {
      streamAbortControllerRef.current?.abort();
    };
    window.addEventListener("pagehide", handlePageHide);
    return () => {
      window.removeEventListener("pagehide", handlePageHide);
      streamAbortControllerRef.current?.abort();
    };
  }, []);

  const refreshSelectedRun = useCallback(async (runId: string) => {
    const run = await fetchReportRun(runId);
    if (selectedRunIdRef.current !== runId) return run;
    setSelectedRun(run);
    setRuns((current) => upsertRun(current, run));
    try {
      const nextCandidate = await fetchReportRunCandidate(runId);
      if (selectedRunIdRef.current !== runId) return run;
      setCandidate(nextCandidate);
    } catch (error) {
      if (isApiNotFound(error)) {
        if (selectedRunIdRef.current === runId) setCandidate(null);
      } else {
        throw error;
      }
    }
    const eventPage = await fetchReportRunEvents(runId, {
      afterSeq: 0,
      limit: REPORT_RUN_EVENT_LIMIT,
    });
    if (selectedRunIdRef.current !== runId) return run;
    setEvents(eventPage.events ?? []);
    setEventCursor(eventPage.next_seq ?? 0);
    return run;
  }, []);

  function updateEvidenceRecord(recordId: string, patch: Partial<ReportEvidenceRecord>) {
    setEvidenceRecords((current) => current.map((record) => (
      record.evidence_id === recordId ? { ...record, ...patch } : record
    )));
    setEvidenceDirty(true);
  }

  function removeEvidenceRecord(recordId: string) {
    setEvidenceRecords((current) => current
      .filter((record) => record.evidence_id !== recordId)
      .map((record) => ({
        ...record,
        conflicts_with: record.conflicts_with.filter((id) => id !== recordId),
      })));
    setEvidenceDirty(true);
  }

  function addEvidenceRecord() {
    setEvidenceRecords((current) => [...current, createEmptyEvidenceRecord()]);
    setEvidenceDirty(true);
  }

  function addFieldConflict(recordId: string) {
    setEvidenceRecords((current) => current.map((record) => (
      record.evidence_id === recordId
        ? {
            ...record,
            field_conflicts: [...record.field_conflicts, { accident_field: "", explanation: "" }],
          }
        : record
    )));
    setEvidenceDirty(true);
  }

  function removeFieldConflict(recordId: string, index: number) {
    setEvidenceRecords((current) => current.map((record) => (
      record.evidence_id === recordId
        ? { ...record, field_conflicts: record.field_conflicts.filter((_, itemIndex) => itemIndex !== index) }
        : record
    )));
    setEvidenceDirty(true);
  }

  async function handleSaveEvidence() {
    if (savingEvidence) {
      return;
    }
    setSavingEvidence(true);
    setErrorMessage("");
    try {
      const saved = await saveReportEvidence(sessionId, evidenceRevision, evidenceRecords);
      setEvidenceRevision(saved.revision);
      setEvidenceRecords(saved.records ?? []);
      setEvidenceDirty(false);
      setNoticeMessage("补充证据已保存，下一次创建运行会冻结当前证据版本。");
    } catch (error) {
      if (error instanceof ApiError && error.status === 409) {
        setErrorMessage("证据版本已变化，当前编辑未覆盖服务器内容。请先重新读取，再决定是否合并。");
      } else {
        setErrorMessage(formatApiErrorMessage(error, "保存补充证据失败。"));
      }
    } finally {
      setSavingEvidence(false);
    }
  }

  async function handleReloadEvidence() {
    try {
      const response = await fetchReportEvidence(sessionId);
      setEvidenceRevision(response.revision);
      setEvidenceRecords(response.records ?? []);
      setEvidenceDirty(false);
      setErrorMessage("");
      setNoticeMessage("已重新读取服务器证据内容，本地未保存编辑已丢弃。");
    } catch (error) {
      setErrorMessage(formatApiErrorMessage(error, "重新读取证据失败。"));
    }
  }

  async function handleAutoSaveDraft(json: string) {
    updateAccidentJson(json);
    try {
      await persistDraft(json);
      setErrorMessage("");
    } catch (error) {
      setErrorMessage(formatApiErrorMessage(error, "自动保存事故输入失败。"));
      throw error;
    }
  }

  async function handleCreateRun(json: string) {
    if (creatingRun || streamingAction || streamAbortControllerRef.current) {
      return;
    }
    setCreatingRun(true);
    setErrorMessage("");
    setNoticeMessage("");
    try {
      updateAccidentJson(json);
      const accidentData = parseAccidentData(json);
      const run = await createReportRunAfterDraftSave(
        () => persistDraft(json),
        () => createReportRun({
          request_id: createEvidenceId(),
          session_id: sessionId,
          accident_data: accidentData,
          evidence_revision: evidenceRevision,
        }),
      );
      setRuns((current) => upsertRun(current, run, true));
      setSelectedRunId(run.run_id);
      selectedRunIdRef.current = run.run_id;
      setSelectedRun(run);
      setCandidate(null);
      setEvents([]);
      setEventCursor(0);
      setPreviewConfirmed(false);
      setNoticeMessage("运行已创建，正在读取授权预览。请确认后再执行。");
      try {
        const nextPreview = await fetchAuthorizationPreview(run.run_id);
        if (selectedRunIdRef.current === run.run_id) setPreview(nextPreview);
      } catch (error) {
        setErrorMessage(formatApiErrorMessage(error, "运行已创建，但授权预览读取失败。"));
      }
    } catch (error) {
      setErrorMessage(formatApiErrorMessage(error, "创建报告运行失败。"));
    } finally {
      setCreatingRun(false);
    }
  }

  async function handleLoadPreview() {
    if (!selectedRun) {
      return;
    }
    const runId = selectedRun.run_id;
    try {
      const nextPreview = await fetchAuthorizationPreview(runId);
      if (selectedRunIdRef.current !== runId) return;
      setPreview(nextPreview);
      setPreviewConfirmed(false);
      setErrorMessage("");
    } catch (error) {
      if (selectedRunIdRef.current !== runId) return;
      setErrorMessage(formatApiErrorMessage(error, "读取授权预览失败。"));
    }
  }

  async function handleAuthorize() {
    if (!selectedRun || !preview?.available || !previewConfirmed || authorizing) {
      return;
    }
    setAuthorizing(true);
    const runId = selectedRun.run_id;
    setErrorMessage("");
    try {
      const updated = await authorizeReportRun(
        runId,
        buildAuthorizationPayload(preview),
      );
      if (selectedRunIdRef.current !== runId) return;
      setSelectedRun(updated);
      setRuns((current) => upsertRun(current, updated));
      setAuthorizedRunIds((current) => ({ ...current, [updated.run_id]: true }));
      setNoticeMessage("授权已记录。请点击“显式执行”开始运行。");
    } catch (error) {
      if (selectedRunIdRef.current !== runId) return;
      if (error instanceof ApiError && error.status === 409) {
        try {
          await refreshSelectedRun(selectedRun.run_id);
        } catch (refreshError) {
          setErrorMessage(formatApiErrorMessage(refreshError, "授权冲突，刷新运行状态也失败。"));
          return;
        }
      }
      setErrorMessage(formatApiErrorMessage(error, "授权确认失败，运行状态已保留。"));
    } finally {
      setAuthorizing(false);
    }
  }

  function appendStreamEvent(event: ReportRunEvent) {
    if (event.run_id !== streamRunIdRef.current || event.run_id !== selectedRunIdRef.current) return;
    setEvents((current) => {
      if (current.some((item) => item.seq === event.seq)) {
        return current;
      }
      return [...current, event].sort((left, right) => left.seq - right.seq);
    });
    setEventCursor((current) => Math.max(current, event.seq));
  }

  async function handleStartStream(action: "execute" | "resume") {
    if (!selectedRun || streamingAction || (action === "execute" && !executeAllowed)) {
      return;
    }
    if (selectedRunProfile !== "synthetic_test" && !isSelectedRunAuthorized) {
      setErrorMessage("外发运行尚未完成授权预览确认，请先完成确认。");
      return;
    }
    const controller = new AbortController();
    streamAbortControllerRef.current = controller;
    streamRunIdRef.current = selectedRun.run_id;
    setStreamingAction(action);
    setErrorMessage("");
    setNoticeMessage(action === "resume" && retryUnknownRequests
      ? "正在恢复运行，已明确允许重试未知请求，可能产生重复计费。"
      : action === "resume" ? "正在恢复运行，未知请求默认不重试。" : "正在启动运行流。请勿重复点击。");
    try {
      const handlers = { onEvent: appendStreamEvent };
      if (action === "execute") {
        await executeReportRunStream(selectedRun.run_id, selectedRun.state_version, handlers, controller.signal);
      } else {
        await resumeReportRunStream(
          selectedRun.run_id,
          selectedRun.state_version,
          selectedRun.can_resume_protocol ? false : retryUnknownRequests,
          handlers,
          controller.signal,
        );
      }
      await refreshSelectedRun(selectedRun.run_id);
      setNoticeMessage("运行流已结束，页面已读取服务端终态。");
    } catch (error) {
      if (!isAbortError(error)) {
        if (error instanceof ApiError && error.status === 409) {
          try {
            await refreshSelectedRun(selectedRun.run_id);
          } catch (refreshError) {
            setErrorMessage(formatApiErrorMessage(refreshError, "运行冲突，刷新状态失败。"));
            return;
          }
        }
        setErrorMessage(formatApiErrorMessage(error, "运行流启动或执行失败。"));
      }
    } finally {
      if (streamAbortControllerRef.current === controller) {
        streamAbortControllerRef.current = null;
        streamRunIdRef.current = null;
      }
      setStreamingAction(null);
    }
  }

  async function handleCancelRun() {
    if (!selectedRun || RUN_TERMINAL_STATES.has(selectedRun.state)) {
      return;
    }
    setErrorMessage("");
    const runId = selectedRun.run_id;
    try {
      const updated = await cancelReportRun(runId);
      if (streamRunIdRef.current === runId) streamAbortControllerRef.current?.abort();
      if (selectedRunIdRef.current !== runId) return;
      setSelectedRun(updated);
      setRuns((current) => upsertRun(current, updated));
      await refreshSelectedRun(updated.run_id);
      setNoticeMessage("取消屏障已提交，页面显示服务端最新状态。");
    } catch (error) {
      if (selectedRunIdRef.current !== runId) return;
      setErrorMessage(formatApiErrorMessage(error, "取消运行失败。"));
    }
  }

  async function handleLoadMoreEvents() {
    if (!selectedRun || eventsLoading || eventCursor >= selectedRun.last_event_seq) {
      return;
    }
    setEventsLoading(true);
    const runId = selectedRun.run_id;
    try {
      const page = await fetchReportRunEvents(runId, {
        afterSeq: eventCursor,
        limit: REPORT_RUN_EVENT_LIMIT,
      });
      if (selectedRunIdRef.current !== runId) return;
      setEvents((current) => {
        const merged = [...current, ...(page.events ?? [])];
        return merged.filter((event, index, items) => items.findIndex((item) => item.seq === event.seq) === index)
          .sort((left, right) => left.seq - right.seq);
      });
      setEventCursor(page.next_seq ?? eventCursor);
    } catch (error) {
      if (selectedRunIdRef.current !== runId) return;
      setErrorMessage(formatApiErrorMessage(error, "读取后续运行轨迹失败。"));
    } finally {
      setEventsLoading(false);
    }
  }

  async function handleExport(mode: ReportExportMode, format: ReportExportFormat) {
    if (!selectedRun || !canExportReportRun(selectedRun, mode) || exporting) {
      return;
    }
    const exportKey = `${mode}:${format}`;
    setExporting(exportKey);
    setErrorMessage("");
    try {
      const result = await downloadReportRunExport(selectedRun.run_id, format, mode);
      const url = URL.createObjectURL(result.blob);
      const anchor = document.createElement("a");
      anchor.href = url;
      anchor.download = result.fileName;
      document.body.appendChild(anchor);
      anchor.click();
      anchor.remove();
      URL.revokeObjectURL(url);
      setNoticeMessage(mode === "formal" ? "正式导出已开始下载。" : "离线工程验证文件已开始下载，并带有工程标记。");
    } catch (error) {
      if (error instanceof ApiError && error.status === 409) {
        try {
          await refreshSelectedRun(selectedRun.run_id);
        } catch (refreshError) {
          setErrorMessage(formatApiErrorMessage(refreshError, "导出资格冲突，刷新状态失败。"));
          return;
        }
      }
      setErrorMessage(formatApiErrorMessage(error, "报告运行导出失败。"));
    } finally {
      setExporting(null);
    }
  }

  const renderEvidenceRecord = (record: ReportEvidenceRecord) => (
    <article className="harness-evidence-record" key={record.evidence_id}>
      <div className="harness-record-header">
        <div>
          <span className="harness-record-index">证据 {evidenceRecords.indexOf(record) + 1}</span>
          <code>{record.evidence_id}</code>
        </div>
        <button
          type="button"
          className="harness-link-button danger"
          onClick={() => removeEvidenceRecord(record.evidence_id)}
        >
          删除证据
        </button>
      </div>
      <div className="harness-form-grid">
        <label className="harness-field harness-field-wide">
          <span>证据内容</span>
          <textarea
            aria-label="证据内容"
            value={record.text}
            rows={3}
            maxLength={8000}
            onChange={(event) => updateEvidenceRecord(record.evidence_id, { text: event.target.value })}
            placeholder="记录可核对的事实或陈述，不把推断写成事实。"
          />
        </label>
        <label className="harness-field">
          <span>来源标签</span>
          <input
            aria-label="来源标签"
            value={record.source_label}
            maxLength={200}
            onChange={(event) => updateEvidenceRecord(record.evidence_id, { source_label: event.target.value })}
            placeholder="例如：现场照片 03"
          />
        </label>
        <label className="harness-field">
          <span>来源定位</span>
          <input
            aria-label="来源定位"
            value={record.source_locator}
            maxLength={500}
            onChange={(event) => updateEvidenceRecord(record.evidence_id, { source_locator: event.target.value })}
            placeholder="例如：第 3 张图左侧车道"
          />
        </label>
        <label className="harness-field">
          <span>证据类型</span>
          <select
            aria-label="证据类型"
            value={record.kind}
            onChange={(event) => updateEvidenceRecord(record.evidence_id, {
              kind: event.target.value as ReportEvidenceRecord["kind"],
            })}
          >
            {Object.entries(EVIDENCE_KIND_LABELS).map(([value, label]) => (
              <option value={value} key={value}>{label}</option>
            ))}
          </select>
        </label>
        <label className="harness-field">
          <span>核实状态</span>
          <select
            aria-label="核实状态"
            value={record.verification_status}
            onChange={(event) => updateEvidenceRecord(record.evidence_id, {
              verification_status: event.target.value as ReportEvidenceRecord["verification_status"],
            })}
          >
            {Object.entries(VERIFICATION_STATUS_LABELS).map(([value, label]) => (
              <option value={value} key={value}>{label}</option>
            ))}
          </select>
        </label>
        <label className="harness-field harness-field-wide">
          <span>核实说明</span>
          <textarea
            aria-label="核实说明"
            value={record.verification_note ?? ""}
            rows={2}
            maxLength={1000}
            onChange={(event) => updateEvidenceRecord(record.evidence_id, { verification_note: event.target.value })}
            placeholder="人工已核实或存在争议时必填。"
          />
        </label>
      </div>

      <div className="harness-subsection">
        <div className="harness-subsection-heading">
          <div>
            <strong>互相冲突</strong>
            <span>选择同一会话中与此记录相冲突的证据。</span>
          </div>
        </div>
        <div className="harness-checkbox-grid">
          {evidenceRecords.filter((other) => other.evidence_id !== record.evidence_id).length === 0 ? (
            <span className="harness-muted">暂时没有其他证据可选择。</span>
          ) : evidenceRecords.filter((other) => other.evidence_id !== record.evidence_id).map((other) => (
            <label className="harness-checkbox-row" key={other.evidence_id}>
              <input
                type="checkbox"
                checked={record.conflicts_with.includes(other.evidence_id)}
                onChange={(event) => {
                  const next = toggleEvidenceConflict(record, other.evidence_id, event.target.checked);
                  updateEvidenceRecord(record.evidence_id, { conflicts_with: next.conflicts_with });
                }}
              />
              <span>{other.source_label || `证据 ${evidenceRecords.indexOf(other) + 1}`}</span>
              <code>{other.evidence_id.slice(0, 8)}</code>
            </label>
          ))}
        </div>
      </div>

      <div className="harness-subsection">
        <div className="harness-subsection-heading">
          <div>
            <strong>JSON Pointer 字段冲突</strong>
            <span>仅填写最终事故输入内可定位的字段，例如 `/车辆/速度`。</span>
          </div>
          <button type="button" className="harness-link-button" onClick={() => addFieldConflict(record.evidence_id)}>
            添加字段冲突
          </button>
        </div>
        {record.field_conflicts.length === 0 ? (
          <span className="harness-muted">未绑定事故字段。</span>
        ) : (
          <div className="harness-field-conflict-list">
            {record.field_conflicts.map((conflict, index) => (
              <div className="harness-field-conflict-row" key={`${record.evidence_id}-field-${index}`}>
                <input
                  value={conflict.accident_field}
                  placeholder="/事故字段"
                  onChange={(event) => {
                    const next = updateEvidenceFieldConflict(record, index, { accident_field: event.target.value });
                    updateEvidenceRecord(record.evidence_id, { field_conflicts: next.field_conflicts });
                  }}
                />
                <input
                  value={conflict.explanation}
                  placeholder="说明冲突原因"
                  onChange={(event) => {
                    const next = updateEvidenceFieldConflict(record, index, { explanation: event.target.value });
                    updateEvidenceRecord(record.evidence_id, { field_conflicts: next.field_conflicts });
                  }}
                />
                <button
                  type="button"
                  className="harness-icon-button"
                  aria-label="删除字段冲突"
                  title="删除字段冲突"
                  onClick={() => removeFieldConflict(record.evidence_id, index)}
                >
                  ×
                </button>
              </div>
            ))}
          </div>
        )}
      </div>
    </article>
  );

  const renderPreview = () => {
    if (!preview || !selectedRun) {
      return null;
    }
    return (
      <section className="harness-section harness-preview-section">
        <div className="harness-section-heading">
          <div>
            <span className="harness-kicker">授权边界</span>
            <h3>外发预览与确认</h3>
          <p>请核对待发内容、端点标签和知识集合；确认后才能执行。</p>
          </div>
          <span className={`harness-status-pill ${preview.available ? "is-success" : "is-warning"}`}>
            {preview.available ? "可供确认" : "授权配置不可用"}
          </span>
        </div>
        {!preview.available ? (
          <div className="harness-callout is-warning">
            <strong>外发配置暂不可用</strong>
            <p>{authorizationReasonLabel(preview.reason)}</p>
            {selectedRunProfile === "synthetic_test" && <p>当前为离线工程验证，可继续执行。</p>}
          </div>
        ) : (
          <>
            <div className="harness-digest-grid">
              <div><span>输入快照摘要</span><code>{preview.snapshot_digest}</code></div>
              <div><span>外发配置摘要</span><code>{preview.endpoint_profile_digest}</code></div>
              <div><span>知识集合摘要</span><code>{preview.approved_knowledge_manifest_digest}</code></div>
            </div>
            <details className="harness-preview-details" open>
              <summary>待发事故输入与补充证据</summary>
              <pre>{JSON.stringify({
                accident_data: preview.snapshot.accident_data,
                supplemental_records: preview.snapshot.supplemental_records,
              }, null, 2)}</pre>
            </details>
            <div className="harness-preview-catalog-grid">
              <div>
                <strong>端点说明</strong>
                {preview.endpoints.map((endpoint) => (
                  <div className="harness-catalog-item" key={`${endpoint.role}-${endpoint.label}`}>
                    <span>{endpointRoleLabel(endpoint.role)}</span>
                    <strong>{endpoint.label}</strong>
                    <small>{endpoint.model} / {endpoint.version}</small>
                  </div>
                ))}
              </div>
              <div>
                <strong>知识集合</strong>
                {preview.knowledge_collections.map((collection) => (
                  <div className="harness-catalog-item" key={collection.collection_id}>
                    <span>{collection.collection_id}</span>
                    <strong>{collection.label}</strong>
                    <small>{collection.version}</small>
                  </div>
                ))}
              </div>
            </div>
            <label className="harness-confirm-row">
              <input
                type="checkbox"
                checked={previewConfirmed}
                onChange={(event) => setPreviewConfirmed(event.target.checked)}
              />
              <span>我已查看待发文字、端点标签和知识集合，同意按以上三个摘要授权。</span>
            </label>
            <button
              type="button"
              className="harness-primary-button"
              disabled={!previewConfirmed || authorizing}
              onClick={() => void handleAuthorize()}
            >
              {authorizing ? "正在记录授权..." : "确认授权并准备执行"}
            </button>
          </>
        )}
      </section>
    );
  };

  const renderRunDetail = () => {
    if (!selectedRun) {
      return (
        <section className="harness-section harness-empty-detail">
          <span className="harness-kicker">运行详情</span>
          <h3>还没有选中的运行</h3>
          <p>创建新的工程运行，或从左侧运行列表选择历史记录。</p>
        </section>
      );
    }
    const canResume = selectedRun.state === "suspended" || selectedRun.can_resume_protocol === true;
    const canCancel = !RUN_TERMINAL_STATES.has(selectedRun.state);
    const candidateMarkdown = selectedRun.report?.report_markdown
      || candidate?.candidate_report.report_markdown
      || "";
    return (
      <section className="harness-section harness-detail-section">
        <div className="harness-section-heading">
          <div>
            <span className="harness-kicker">选中运行</span>
            <h3>{formatRunState(selectedRun.state)}</h3>
            <p className="harness-run-id">{selectedRun.run_id}</p>
          </div>
          <button
            type="button"
            className="harness-secondary-button"
            disabled={runLoading || Boolean(streamingAction)}
            onClick={() => void refreshSelectedRun(selectedRun.run_id).catch((error) => setErrorMessage(
              formatApiErrorMessage(error, "刷新运行状态失败。"),
            ))}
          >
            刷新状态
          </button>
        </div>

        <div className="harness-run-meta-grid">
          <div><span>运行模式</span><strong>{executionProfileLabel(selectedRun.execution_profile)}</strong></div>
          <div><span>导出资格</span><strong>{qualityGateLabel(selectedRun.quality_gate, selectedRun.formal_export_eligible)}</strong></div>
          <div><span>审查状态</span><strong>{reviewStatusLabel(selectedRun.review_status)}</strong></div>
          <div><span>状态版本</span><strong>{selectedRun.state_version}</strong></div>
          <div><span>候选版本</span><strong>{selectedRun.candidate_version}</strong></div>
          <div><span>最后轨迹序号</span><strong>{selectedRun.last_event_seq}</strong></div>
        </div>

        {selectedRun.terminal_reason && (
          <div className="harness-callout is-warning"><strong>终止原因</strong><p>{selectedRun.terminal_reason}</p></div>
        )}

        <div className="harness-action-row">
          {selectedRunProfile !== "synthetic_test" && !isSelectedRunAuthorized && (
            <button
              type="button"
              className="harness-secondary-button"
              onClick={() => void handleLoadPreview()}
              disabled={Boolean(streamingAction)}
            >
              读取授权预览
            </button>
          )}
          <button
            type="button"
            className="harness-primary-button"
            disabled={!executeAllowed || Boolean(streamingAction) || creatingRun}
            onClick={() => void handleStartStream("execute")}
          >
            {streamingAction === "execute" ? "正在执行..." : "显式执行"}
          </button>
          {canResume && (
            <button
              type="button"
              className="harness-primary-button"
              disabled={Boolean(streamingAction) || (selectedRunProfile !== "synthetic_test" && !isSelectedRunAuthorized)}
              onClick={() => void handleStartStream("resume")}
            >
              {streamingAction === "resume" ? "正在恢复..." : "显式恢复"}
            </button>
          )}
          {canCancel && (
            <button
              type="button"
              className="harness-secondary-button danger"
              disabled={Boolean(streamingAction) && !selectedRun}
              onClick={() => void handleCancelRun()}
            >
              取消运行
            </button>
          )}
        </div>

        {selectedRun.state === "suspended" && (
          <label className="harness-risk-row">
            <input
              type="checkbox"
              checked={retryUnknownRequests}
              onChange={(event) => setRetryUnknownRequests(event.target.checked)}
            />
            <span>允许恢复时重试未知请求，可能产生重复计费或重复外部副作用。</span>
          </label>
        )}

        {selectedRunProfile !== "synthetic_test" && !isSelectedRunAuthorized && selectedRun.state === "queued" && (
          <div className="harness-callout is-warning"><strong>尚未授权</strong><p>外发运行需要先完成授权预览确认。</p></div>
        )}
        {selectedRunProfile === "outbound" && selectedRun.state === "queued" && !preview?.available && (
          <div className="harness-callout is-error"><strong>外发配置暂不可用</strong><p>当前运行暂不能执行。</p></div>
        )}

        <div className="harness-budget-block">
          <div className="harness-subsection-heading"><div><strong>预算</strong><span>当前使用量与创建时冻结的策略。</span></div></div>
          <div className="harness-budget-grid">
            <div><span>已知 token</span><strong>{formatBudgetValue(selectedRun.budget.known_used)}</strong></div>
            <div><span>未知预留</span><strong>{formatBudgetValue(selectedRun.budget.unknown_reserved)}</strong></div>
            <div><span>物理请求</span><strong>{formatBudgetValue(selectedRun.budget.physical_requests)}</strong></div>
            <div><span>剩余 token</span><strong>{formatBudgetValue(selectedRun.budget.remaining)}</strong></div>
            <div><span>活动秒数</span><strong>{formatBudgetValue(selectedRun.budget.active_seconds)}</strong></div>
            <div><span>最大请求数</span><strong>{formatBudgetValue(selectedRun.budget_policy?.max_physical_requests)}</strong></div>
          </div>
        </div>

        <div className="harness-export-block">
          <div className="harness-subsection-heading"><div><strong>导出</strong><span>当前运行的文件导出选项。</span></div></div>
          <div className="harness-export-grid">
            {(["md", "docx", "pdf"] as ReportExportFormat[]).map((format) => (
              <div className="harness-export-item" key={format} aria-label={format.toUpperCase()}>
                <strong>{format.toUpperCase()}</strong>
                <button
                  type="button"
                  className="harness-secondary-button"
                  disabled={!formalExportAllowed || exporting !== null}
                  onClick={() => void handleExport("formal", format)}
                >
                  {exporting === `formal:${format}` ? "正在下载..." : "正式导出"}
                </button>
                <button
                  type="button"
                  className="harness-link-button"
                  disabled={!engineeringExportAllowed || exporting !== null}
                  onClick={() => void handleExport("engineering", format)}
                >
                  {exporting === `engineering:${format}` ? "正在下载..." : "工程样本"}
                </button>
              </div>
            ))}
          </div>
          {!formalExportAllowed && <p className="harness-muted">该运行未获正式导出资格。</p>}
          {engineeringExportAllowed && <p className="harness-warning-text">离线工程验证文件带有工程标记。</p>}
        </div>

        <div className="harness-issues-block">
          <div className="harness-subsection-heading"><div><strong>问题</strong><span>当前运行发现的问题与处理要求。</span></div></div>
          {selectedIssues.length === 0 ? (
            <p className="harness-muted">暂无可读问题，或候选尚未生成。</p>
          ) : (
            <div className="harness-issue-list">
              {selectedIssues.map((issue) => (
                <article className={`harness-issue-item severity-${issue.severity}`} key={issue.issue_id}>
                  <div><strong>{issue.target}</strong><span>{issueSeverityLabel(issue.severity)} / {issueStatusLabel(issue.status)}</span></div>
                  <p>{issue.explanation}</p>
                  <small>闭环条件：{issue.closure_condition}</small>
                </article>
              ))}
            </div>
          )}
        </div>

        {candidateMarkdown && (
          <div className="harness-candidate-block">
            <div className="harness-subsection-heading"><div><strong>{selectedRun.state === "published" ? "已发布" : "候选稿"}</strong><span>{selectedRun.state === "published" ? "当前已发布版本" : "当前候选版本"}</span></div></div>
            <div className="harness-markdown markdown-report">
              <ReactMarkdown remarkPlugins={[remarkGfm]}>{normalizeMarkdownForDisplay(candidateMarkdown)}</ReactMarkdown>
            </div>
          </div>
        )}

        <div className="harness-events-block">
          <div className="harness-subsection-heading">
            <div><strong>运行轨迹</strong><span>按序号显示当前运行记录。</span></div>
            <button
              type="button"
              className="harness-link-button"
              disabled={eventsLoading || eventCursor >= selectedRun.last_event_seq}
              onClick={() => void handleLoadMoreEvents()}
            >
              {eventsLoading ? "正在读取..." : eventCursor < selectedRun.last_event_seq ? "读取后续轨迹" : "已读到当前末尾"}
            </button>
          </div>
          {events.length === 0 ? (
            <p className="harness-muted">暂无公开轨迹。</p>
          ) : (
            <div className="harness-events-table-wrap">
              <table className="harness-events-table">
                <thead><tr><th>序号</th><th>类型</th><th>状态版本</th><th>发生时间</th><th>公开数据</th></tr></thead>
                <tbody>
                  {events.map((event) => (
                    <tr key={`${event.run_id}-${event.seq}`}>
                      <td>{event.seq}</td>
                      <td><span className="harness-event-type">{eventTypeLabel(event.type)}</span></td>
                      <td>{event.state_version}</td>
                      <td>{formatDate(event.occurred_at)}</td>
                      <td><pre>{JSON.stringify(event.data, null, 2)}</pre></td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </div>
      </section>
    );
  };

  if (loading) {
    return <div className="report-harness-loading">正在读取证据版本和报告运行列表...</div>;
  }

  return (
    <div className="report-harness-panel">
      <div className="report-harness-intro">
        <div>
          <span className="harness-kicker">SafetyRAISE / REPORT HARNESS</span>
          <h2>证据报告工作台</h2>
          <p>在当前会话中补充证据、查看运行状态并处理导出。</p>
        </div>
        <div className="report-harness-intro-actions">
          <span className="harness-session-chip">会话 {sessionId.slice(0, 12)}</span>
          <button type="button" className="harness-secondary-button" onClick={() => void loadWorkspace(false)} disabled={runsLoading}>
            刷新只读数据
          </button>
        </div>
      </div>

      {errorMessage && (
        <div className="harness-callout is-error" role="alert">
          <strong>需要处理</strong>
          <p>{errorMessage}</p>
          {(errorMessage.includes("证据版本") || errorMessage.includes("revision")) && <button type="button" className="harness-link-button" onClick={() => void handleReloadEvidence()}>重新读取证据</button>}
        </div>
      )}
      {noticeMessage && <div className="harness-callout is-info" role="status"><p>{noticeMessage}</p></div>}

      <div className="report-harness-layout">
        <div className="report-harness-editor-column">
          <section className="harness-section">
            <div className="harness-section-heading">
              <div>
                <span className="harness-kicker">证据版本</span>
                <h3>补充证据</h3>
                <p>保存时检查证据版本；发生冲突时保留当前编辑。</p>
              </div>
              <span className={`harness-status-pill ${evidenceDirty ? "is-warning" : "is-success"}`}>
                证据版本 {evidenceRevision} {evidenceDirty ? "· 未保存" : "· 已同步"}
              </span>
            </div>
            <div className="harness-evidence-list">
              {evidenceRecords.length === 0 ? <p className="harness-muted">尚无补充证据，添加后可在创建运行时一并冻结。</p> : evidenceRecords.map(renderEvidenceRecord)}
            </div>
            <div className="harness-section-actions">
              <button type="button" className="harness-secondary-button" onClick={addEvidenceRecord}>添加证据</button>
              <button type="button" className="harness-primary-button" disabled={!evidenceDirty || savingEvidence} onClick={() => void handleSaveEvidence()}>
                {savingEvidence ? "正在保存..." : "保存补充证据"}
              </button>
              <button type="button" className="harness-link-button" onClick={() => void handleReloadEvidence()}>放弃本地编辑并重新读取</button>
            </div>
          </section>

          <section className="harness-section">
            <div className="harness-section-heading">
              <div>
                <span className="harness-kicker">创建运行</span>
                <h3>事故输入</h3>
                <p>创建运行会保存当前事故输入和证据版本，不会立即执行。</p>
              </div>
            </div>
            {hasDraft ? (
              <JsonTableEditor
                initialJson={initialDraftJson}
                resetKey={sessionId}
                onDraftChange={updateAccidentJson}
                onAutoSave={handleAutoSaveDraft}
                onConfirm={handleCreateRun}
                disabled={creatingRun || Boolean(streamingAction)}
                confirmLabel={creatingRun ? "正在创建运行..." : "创建证据报告运行"}
              />
            ) : (
              <div className="harness-raw-input">
                <label className="harness-field harness-field-wide">
                  <span>事故输入 JSON</span>
                  <textarea value={accidentJson} rows={10} onChange={(event) => setAccidentJson(event.target.value)} placeholder='例如：{"事故类型":"追尾"}' />
                </label>
                <button type="button" className="harness-primary-button" disabled={creatingRun || Boolean(streamingAction)} onClick={() => void handleCreateRun(accidentJson)}>
                  {creatingRun ? "正在创建运行..." : "创建证据报告运行"}
                </button>
              </div>
            )}
          </section>

          {preview && renderPreview()}
        </div>

        <aside className="report-harness-runs-column">
          <section className="harness-section harness-runs-section">
            <div className="harness-section-heading">
              <div>
                <span className="harness-kicker">只读历史</span>
                <h3>运行列表</h3>
                <p>{runs.length ? `当前会话已读取 ${runs.length} 条运行` : "当前会话还没有运行"}</p>
              </div>
              <span className="harness-status-pill">{runCursor ? "还有历史" : "已到当前页"}</span>
            </div>
            <div className="harness-run-list">
              {runs.length === 0 ? (
                <p className="harness-muted">创建后的运行会出现在这里。</p>
              ) : runs.map((run) => (
                <button
                  type="button"
                  className={`harness-run-list-item ${selectedRunId === run.run_id ? "is-selected" : ""}`}
                  key={run.run_id}
                  disabled={Boolean(streamingAction)}
                  onClick={() => setSelectedRunId(run.run_id)}
                >
                  <span className="harness-run-list-topline"><strong>{formatRunState(run.state)}</strong><span>{executionProfileLabel(run.execution_profile)}</span></span>
                  <code>{run.run_id}</code>
                  <span className="harness-run-list-meta">轨迹 {run.last_event_seq} · {qualityGateLabel(run.quality_gate, run.formal_export_eligible)}</span>
                </button>
              ))}
            </div>
            {runCursor && (
              <button type="button" className="harness-secondary-button harness-full-button" disabled={runsLoading} onClick={() => void loadWorkspace(true)}>
                {runsLoading ? "正在读取历史..." : "读取更早运行"}
              </button>
            )}
          </section>
          {renderRunDetail()}
        </aside>
      </div>
    </div>
  );
});

export default ReportHarnessPanel;
