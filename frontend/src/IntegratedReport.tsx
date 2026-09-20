import { forwardRef, useEffect, useId, useImperativeHandle, useRef, useState } from "react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import { Check, Download, FileText, Pencil, Play, RotateCw, Square, Trash2, X } from "lucide-react";
import {
  authorizeReportRun, cancelReportRun, createReportRun, downloadReportRunExport,
  executeReportRunStream, fetchAuthorizationPreview, fetchReportEvidence,
  fetchReportRun, fetchReportRunCandidate, formatApiErrorMessage, listReportRuns,
  resumeReportRunStream, saveReportEvidence,
} from "./api";
import {
  buildAuthorizationPayload, createEmptyEvidenceRecord, createEvidenceId,
  parseAccidentData, RUN_TERMINAL_STATES,
} from "./ReportHarnessPanel";
import type { ReportEvidenceResponse, ReportExportFormat, ReportRunCandidate, ReportRunView } from "./types";
import "./integratedReport.css";

export interface IntegratedReportHandle {
  generate: (json: string) => Promise<void>;
  cancel: () => Promise<void>;
}

const labels: Record<string, string> = {
  queued: "等待生成", preparing: "分析事故并检索依据", generating: "撰写报告",
  checking: "核查事实与法律依据", revising: "修改并复查报告", published: "报告已完成",
  needs_review: "报告仍有待解决的问题", suspended: "生成已暂停",
  cancelled: "已停止", failed: "生成失败", budget_exhausted: "本次额度已用完",
};

export const IntegratedReport = forwardRef<IntegratedReportHandle, {
  sessionId: string;
  onPersistDraft: (sessionId: string, json: string) => Promise<void>;
  onBusyChange: (busy: boolean) => void;
  onActiveChange?: (active: boolean) => void;
  onCancellingChange?: (cancelling: boolean) => void;
  onStatusChange?: (label: string) => void;
}>(function IntegratedReport({ sessionId, onPersistDraft, onBusyChange, onActiveChange, onCancellingChange, onStatusChange }, ref) {
  const fieldId = useId();
  const [run, setRun] = useState<ReportRunView | null>(null);
  const [cancelling, setCancelling] = useState(false);
  const cancellationPending = useRef(false);
  const [history, setHistory] = useState<ReportRunView[]>([]);
  const [candidate, setCandidate] = useState<ReportRunCandidate | null>(null);
  const [evidence, setEvidence] = useState<ReportEvidenceResponse | null>(null);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const [source, setSource] = useState("");
  const [locator, setLocator] = useState("");
  const [text, setText] = useState("");
  const [confirmed, setConfirmed] = useState(false);
  const [retryUnknown, setRetryUnknown] = useState(false);
  const [exporting, setExporting] = useState(false);
  const [loading, setLoading] = useState(true);
  const [loadFailed, setLoadFailed] = useState(false);
  const [reloadVersion, setReloadVersion] = useState(0);
  const [editingId, setEditingId] = useState<string | null>(null);
  const alive = useRef(true);
  const running = useRef(false);
  const currentId = useRef<string | null>(null);
  const versions = useRef(new Map<string, number>());
  const readSequence = useRef(0);
  const stream = useRef<AbortController | null>(null);
  const active = Boolean(run && !RUN_TERMINAL_STATES.has(run.state) && run.state !== "suspended");
  const statusLabel = cancelling ? "正在停止" : labels[run?.state ?? ""] ?? "处理中";

  useEffect(() => { onBusyChange(loading || loadFailed || busy || active); }, [loading, loadFailed, busy, active, onBusyChange]);
  useEffect(() => {
    onActiveChange?.(active);
    return () => onActiveChange?.(false);
  }, [active, onActiveChange]);
  useEffect(() => {
    onCancellingChange?.(cancelling);
    return () => onCancellingChange?.(false);
  }, [cancelling, onCancellingChange]);
  useEffect(() => { setRetryUnknown(false); }, [run?.run_id, run?.state_version]);
  useEffect(() => { onStatusChange?.(statusLabel); }, [statusLabel, onStatusChange]);

  function updateRun(value: ReportRunView) {
    if (!alive.current || (versions.current.get(value.run_id) ?? -1) > value.state_version) return false;
    versions.current.set(value.run_id, value.state_version);
    setRun(value);
    currentId.current = value.run_id;
    setHistory((rows) => rows.some((item) => item.run_id === value.run_id)
      ? rows.map((item) => item.run_id === value.run_id ? value : item) : [value, ...rows]);
    return true;
  }

  async function loadRun(id: string) {
    const sequence = ++readSequence.current;
    const value = await fetchReportRun(id);
    if (!alive.current || currentId.current !== id || readSequence.current !== sequence) return;
    if (!updateRun(value)) return;
    if (value.candidate_version > 0) {
      const content = await fetchReportRunCandidate(id);
      if (alive.current && currentId.current === id && readSequence.current === sequence) setCandidate(content);
    }
  }

  useEffect(() => {
    alive.current = true;
    let valid = true;
    setLoading(true);
    setLoadFailed(false);
    setError("");
    void Promise.all([listReportRuns(sessionId), fetchReportEvidence(sessionId)])
      .then(async ([rows, materials]) => {
        if (!valid) return;
        setHistory(rows.runs);
        setEvidence(materials);
        if (rows.runs[0]) {
          updateRun(rows.runs[0]);
          await loadRun(rows.runs[0].run_id);
        }
      }).catch((err) => {
        if (valid) {
          setLoadFailed(true);
          setError(formatApiErrorMessage(err, "读取报告记录失败。"));
        }
      }).finally(() => { if (valid) setLoading(false); });
    return () => {
      valid = false;
      alive.current = false;
      stream.current?.abort();
      onBusyChange(false);
    };
  }, [sessionId, reloadVersion]);

  useEffect(() => {
    if (!run || (!busy && (RUN_TERMINAL_STATES.has(run.state) || run.state === "suspended"))) return;
    const id = run.run_id;
    let cancelled = false;
    let timer: ReturnType<typeof setTimeout>;
    async function poll() {
      try { await loadRun(id); }
      catch (err) {
        if (!cancelled) setError(formatApiErrorMessage(err, "读取进度失败，可重新打开此会话。"));
      }
      if (!cancelled) timer = setTimeout(() => void poll(), 2000);
    }
    timer = setTimeout(() => void poll(), 2000);
    return () => { cancelled = true; clearTimeout(timer); };
  }, [run?.run_id, run?.state, busy]);

  async function execute(value: ReportRunView, resume = false) {
    const acknowledged = value.can_resume_protocol ? false : retryUnknown;
    setRetryUnknown(false);
    const controller = new AbortController();
    stream.current = controller;
    const handlers = { onEvent: () => undefined };
    try {
      if (resume) {
        await resumeReportRunStream(value.run_id, value.state_version, acknowledged, handlers, controller.signal);
      } else {
        await executeReportRunStream(value.run_id, value.state_version, handlers, controller.signal);
      }
    } finally {
      if (alive.current && currentId.current === value.run_id) await loadRun(value.run_id);
      if (stream.current === controller) stream.current = null;
    }
  }

  async function action(operation: () => Promise<void>) {
    if (running.current) return;
    running.current = true;
    setBusy(true);
    onBusyChange(true);
    setError("");
    try { await operation(); }
    catch (err) {
      if (alive.current && !(err instanceof DOMException && err.name === "AbortError")) {
        setError(formatApiErrorMessage(err, "本次操作未完成，已有报告和记录已保留。"));
      }
    } finally {
      running.current = false;
      if (alive.current) { setBusy(false); onBusyChange(false); }
    }
  }

  async function cancel() {
    const id = currentId.current;
    if (!id || cancellationPending.current) return;
    cancellationPending.current = true;
    setCancelling(true);
    setError("");
    try {
      await cancelReportRun(id);
      stream.current?.abort();
      await loadRun(id);
    } catch (err) {
      if (alive.current) setError(formatApiErrorMessage(err, "停止失败，正在保留当前执行状态。"));
    } finally {
      cancellationPending.current = false;
      if (alive.current) setCancelling(false);
    }
  }

  useImperativeHandle(ref, () => ({
    generate: (json) => action(async () => {
      if (loading) throw new Error("正在读取此会话，请稍后重试。");
      if (loadFailed) throw new Error("报告记录未读取成功，请先重新读取。");
      if (active) throw new Error("当前报告仍在生成，请等待完成或先停止。");
      const data = parseAccidentData(json);
      await onPersistDraft(sessionId, json);
      if (!alive.current) return;
      const materials = await fetchReportEvidence(sessionId);
      if (!alive.current) return;
      setEvidence(materials);
      let value = await createReportRun({
        request_id: createEvidenceId(), session_id: sessionId,
        accident_data: data, evidence_revision: materials.revision,
      });
      if (!alive.current) return;
      updateRun(value);
      setCandidate(null);
      const preview = await fetchAuthorizationPreview(value.run_id);
      if (!alive.current) return;
      if (!preview.available) throw new Error("报告服务的模型或知识库尚未就绪。");
      value = await authorizeReportRun(value.run_id, buildAuthorizationPayload(preview));
      if (!alive.current) return;
      updateRun(value);
      await execute(value);
    }),
    cancel,
  }));

  async function addEvidence() {
    await action(async () => {
      if (!source.trim() || !locator.trim() || !text.trim()) throw new Error("请填写材料来源、页码或位置和补充内容。");
      if (!evidence) throw new Error("补充材料尚未读取成功，请重新打开此会话。");
      const original = evidence.records.find((item) => item.evidence_id === editingId);
      if (editingId && !original) throw new Error("此材料已发生变化，请重新打开此会话。");
      const item = {
        ...(original ?? createEmptyEvidenceRecord()), source_label: source.trim(), text: text.trim(),
        source_locator: locator.trim(),
        verification_status: confirmed ? "human_confirmed" as const : "unverified" as const,
        verification_note: confirmed ? "由当前用户确认已核实。" : "",
      };
      const records = original
        ? evidence.records.map((record) => record.evidence_id === editingId ? item : record)
        : [...evidence.records, item];
      const saved = await saveReportEvidence(sessionId, evidence.revision, records);
      if (!alive.current) return;
      setEvidence(saved); resetEvidenceEditor();
    });
  }

  function resetEvidenceEditor() {
    setEditingId(null); setSource(""); setLocator(""); setText(""); setConfirmed(false);
  }

  async function removeEvidence(id: string) {
    await action(async () => {
      if (!evidence) return;
      const saved = await saveReportEvidence(sessionId, evidence.revision,
        evidence.records.filter((item) => item.evidence_id !== id));
      if (!alive.current) return;
      setEvidence(saved);
      if (editingId === id) resetEvidenceEditor();
    });
  }

  async function download(format: ReportExportFormat) {
    if (!run?.formal_export_eligible || exporting) return;
    setExporting(true);
    setError("");
    try {
      const result = await downloadReportRunExport(run.run_id, format, "formal");
      const url = URL.createObjectURL(result.blob);
      const link = document.createElement("a");
      link.href = url; link.download = result.fileName; link.click();
      setTimeout(() => URL.revokeObjectURL(url), 1000);
    } catch (err) { setError(formatApiErrorMessage(err, "下载失败。")); }
    finally { if (alive.current) setExporting(false); }
  }

  const body = run?.report?.report_markdown ?? candidate?.candidate_report.report_markdown;
  const issues = candidate?.review_result?.issues?.filter((item) => item.status !== "resolved") ?? [];
  return <section className="integrated-report" aria-label="事故分析报告" data-state={run?.state ?? "empty"}>
    {error && <p role="alert" className="form-error">{error}</p>}
    {loadFailed && <button type="button" disabled={loading || busy}
      onClick={() => setReloadVersion((value) => value + 1)}><RotateCw size={16} aria-hidden="true" />重新读取报告记录</button>}
    <details className="report-evidence-section">
      <summary>补充材料（{evidence?.records.length ?? 0}）</summary>
      {evidence?.records.map((item) => <article key={item.evidence_id} className="report-evidence-item">
        <div className="report-evidence-heading"><strong>{item.source_label}</strong>
          <span className={`evidence-verification ${item.verification_status}`}>
            {item.verification_status === "human_confirmed" ? "已人工核实"
              : item.verification_status === "disputed" ? "存在争议" : "未核实"}
          </span>
        </div>
        <small className="evidence-locator">{item.source_locator}</small>
        <p>{item.text}</p>
        <div className="report-evidence-actions">
        <button type="button" className="report-icon-button" aria-label="编辑" title="编辑材料" disabled={busy || active} onClick={() => {
          setEditingId(item.evidence_id); setSource(item.source_label); setLocator(item.source_locator); setText(item.text);
          setConfirmed(item.verification_status === "human_confirmed");
        }}><Pencil size={16} aria-hidden="true" /></button>
        <button type="button" className="report-icon-button report-danger" aria-label="删除" title="删除材料" disabled={busy || active} onClick={() => void removeEvidence(item.evidence_id)}><Trash2 size={16} aria-hidden="true" /></button>
        </div>
      </article>)}
      <div className="report-evidence-editor">
      <div className="report-evidence-fields">
      <label>材料来源<input value={source} onChange={(event) => setSource(event.target.value)} disabled={busy || active} /></label>
      <label>页码或位置<input value={locator} onChange={(event) => setLocator(event.target.value)} disabled={busy || active} /></label>
      </div>
      <label htmlFor={`${fieldId}-text`}>补充内容</label>
      <textarea id={`${fieldId}-text`} value={text} onChange={(event) => setText(event.target.value)} disabled={busy || active} rows={3} />
      <label className="report-checkbox"><input type="checkbox" checked={confirmed} onChange={(event) => setConfirmed(event.target.checked)} disabled={busy || active} />已人工核实</label>
      <div className="report-evidence-actions">
      <button type="button" onClick={() => void addEvidence()} disabled={loading || !evidence || busy || active || !source.trim() || !locator.trim() || !text.trim()}><Check size={16} aria-hidden="true" />保存补充材料</button>
      {editingId && <button type="button" disabled={busy} onClick={resetEvidenceEditor}><X size={16} aria-hidden="true" />取消编辑</button>}
      </div>
      </div>
    </details>
    <div className="report-section-heading"><FileText size={18} aria-hidden="true" /><h3>事故分析报告</h3></div>
    {!run && !loadFailed && <p className="report-empty" role="status">{loading ? "正在读取报告" : "尚未生成报告"}</p>}
    {run && <>
      <div className="integrated-report-toolbar">
        <strong className="report-status" role="status">{statusLabel}</strong>
        {history.length > 1 && <select aria-label="历史报告" value={run.run_id} disabled={busy || active}
          onChange={(event) => {
            const id = event.target.value;
            const selected = history.find((item) => item.run_id === id);
            if (!selected) return;
            setRun(selected);
            currentId.current = id;
            setRetryUnknown(false);
            setCandidate(null);
            setError("");
            void loadRun(id).catch((err) => setError(formatApiErrorMessage(err, "读取历史报告失败。")));
          }}>
          {history.map((item, index) => <option key={item.run_id} value={item.run_id}>
            报告 {history.length - index} · {labels[item.state] ?? item.state}
          </option>)}
        </select>}
        {active && <button type="button" className="report-danger" disabled={cancelling} aria-busy={cancelling} onClick={() => void cancel()}><Square size={14} aria-hidden="true" />{cancelling ? "正在停止" : "停止生成"}</button>}
        {(run.state === "suspended" || run.state === "queued" || run.can_resume_protocol === true) && !busy && !cancelling && <>
          {!run.can_resume_protocol && Number(run.budget.unknown_requests ?? 0) > 0 && <label className="report-checkbox report-retry-confirmation">
            <input type="checkbox" checked={retryUnknown} onChange={(event) => setRetryUnknown(event.target.checked)} />
            确认重试未收到结果的请求，可能重复计费
          </label>}
          <button type="button" className="report-primary" disabled={busy || (Number(run.budget.unknown_requests ?? 0) > 0 && !retryUnknown)}
            onClick={() => void action(async () => {
              const preview = await fetchAuthorizationPreview(run.run_id);
              if (!alive.current) return;
              if (!preview.available) throw new Error("当前配置无法继续此报告，请重新生成。");
              const value = await authorizeReportRun(run.run_id, buildAuthorizationPayload(preview));
              if (!alive.current) return;
              updateRun(value);
              await execute(value, value.state === "suspended" || value.can_resume_protocol === true);
            })}><Play size={15} aria-hidden="true" />继续生成</button>
        </>}
        {run.formal_export_eligible && (["docx", "pdf", "md"] as const).map((format) =>
          <button key={format} type="button" aria-label={`下载${format === "docx" ? "Word" : format.toUpperCase()}`} disabled={exporting} onClick={() => void download(format)}>
            <Download size={16} aria-hidden="true" />{format === "docx" ? "Word" : format.toUpperCase()}
          </button>)}
      </div>
      {run.terminal_reason === "budget_exhausted" && (
        <p className="report-limit-notice">本次运行达到预算或时长上限，报告未发布。</p>
      )}
      {issues.length > 0 && <details open={run.state === "needs_review"}>
        <summary>待解决问题（{issues.length}）</summary>
        {issues.map((issue) => <p key={issue.issue_id}>{issue.explanation}</p>)}
      </details>}
      {body && <div className="markdown-report"><ReactMarkdown remarkPlugins={[remarkGfm]}>{body}</ReactMarkdown></div>}
    </>}
  </section>;
});
