import { useEffect, useId, useRef, useState } from "react";
import { Check, MessageSquareText } from "lucide-react";
import { ApiError, fetchReportRunFeedback, formatApiErrorMessage, saveReportRunFeedback } from "./api";
import { FEEDBACK_TAGS, FEEDBACK_VERDICTS } from "./feedbackOptions";
import type { ReportFeedback as SavedFeedback, ReportFeedbackTag, ReportFeedbackVerdict } from "./types";

const REVIEWER_STORAGE_KEY = "safetyraise.feedback.reviewer";

interface Draft {
  reviewer_name: string;
  verdict: ReportFeedbackVerdict | null;
  issue_tags: ReportFeedbackTag[];
  comment: string;
}

function rememberedReviewer(): string {
  try { return window.localStorage.getItem(REVIEWER_STORAGE_KEY) ?? ""; } catch { return ""; }
}

function rememberReviewer(name: string) {
  try { window.localStorage.setItem(REVIEWER_STORAGE_KEY, name); } catch { /* 本地存储不可用时只影响下次预填 */ }
}

type FilledFeedback = Extract<SavedFeedback, { verdict: ReportFeedbackVerdict }>;

const isFilled = (value: SavedFeedback | null): value is FilledFeedback =>
  Boolean(value && "verdict" in value);

function toDraft(saved: SavedFeedback | null): Draft {
  if (!isFilled(saved)) {
    return { reviewer_name: rememberedReviewer(), verdict: null, issue_tags: [], comment: "" };
  }
  return { reviewer_name: saved.reviewer_name, verdict: saved.verdict,
           issue_tags: [...saved.issue_tags], comment: saved.comment };
}

function sameDraft(left: Draft, right: Draft): boolean {
  return left.reviewer_name.trim() === right.reviewer_name.trim() && left.verdict === right.verdict
    && left.comment.trim() === right.comment.trim()
    && [...left.issue_tags].sort().join() === [...right.issue_tags].sort().join();
}

function savedAt(value: string): string {
  return new Date(value).toLocaleString("zh-CN", { month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit" });
}

/** 报告质量反馈：每次保存追加一个版本，意见与这份报告及当时的系统版本一起留存。 */
export function ReportFeedback({ runId }: { runId: string }) {
  const id = useId();
  const [saved, setSaved] = useState<SavedFeedback | null>(null);
  const [draft, setDraft] = useState<Draft>(() => toDraft(null));
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const currentRun = useRef(runId);

  useEffect(() => {
    currentRun.current = runId;
    setLoading(true); setError(""); setNotice(""); setSaved(null); setDraft(toDraft(null));
    fetchReportRunFeedback(runId)
      .then((value) => { if (currentRun.current === runId) { setSaved(value); setDraft(toDraft(value)); } })
      .catch((err) => { if (currentRun.current === runId) setError(formatApiErrorMessage(err, "读取反馈失败。")); })
      .finally(() => { if (currentRun.current === runId) setLoading(false); });
  }, [runId]);

  const unchanged = sameDraft(draft, toDraft(saved));
  const canSave = !loading && !saving && Boolean(draft.verdict) && Boolean(draft.reviewer_name.trim())
    && !(isFilled(saved) && unchanged);

  async function save() {
    if (!canSave || !draft.verdict || !saved) return;
    setSaving(true); setError(""); setNotice("");
    try {
      const value = await saveReportRunFeedback(runId, saved.revision, {
        reviewer_name: draft.reviewer_name.trim(), verdict: draft.verdict,
        issue_tags: draft.issue_tags, comment: draft.comment.trim(),
      });
      if (currentRun.current !== runId) return;
      rememberReviewer(draft.reviewer_name.trim());
      setSaved(value); setDraft(toDraft(value));
    } catch (err) {
      if (currentRun.current !== runId) return;
      if (err instanceof ApiError && err.code === "feedback_revision_conflict") {
        const latest = await fetchReportRunFeedback(runId).catch(() => null);
        if (latest && currentRun.current === runId) { setSaved(latest); setDraft(toDraft(latest)); }
        setNotice("这份反馈已在其他页面更新，已载入最新内容，请确认后再保存。");
      } else {
        setError(formatApiErrorMessage(err, "保存反馈失败，已填写的内容仍保留。"));
      }
    } finally {
      if (currentRun.current === runId) setSaving(false);
    }
  }

  function toggleTag(tag: ReportFeedbackTag, checked: boolean) {
    setDraft((value) => ({
      ...value,
      issue_tags: checked ? [...value.issue_tags, tag] : value.issue_tags.filter((item) => item !== tag),
    }));
  }

  const disabled = loading || saving;
  return <section className="report-feedback" aria-labelledby={`${id}-title`} aria-busy={loading}>
    <div className="report-section-heading">
      <MessageSquareText size={18} aria-hidden="true" />
      <h3 id={`${id}-title`}>质量反馈</h3>
    </div>
    <p className="report-feedback-hint">意见会与这份报告及生成时的模型、知识库版本一起保存，可随时修改。</p>
    {error && <p role="alert" className="form-error">{error}</p>}
    {notice && <p role="status" className="report-feedback-notice">{notice}</p>}

    <fieldset className="report-feedback-group" disabled={disabled}>
      <legend>总体结论</legend>
      <div className="feedback-verdict-options">
        {FEEDBACK_VERDICTS.map((option) => <label key={option.value} className="feedback-verdict-option"
          data-tone={option.tone} data-checked={draft.verdict === option.value}>
          <input type="radio" name={`${id}-verdict`} value={option.value}
            checked={draft.verdict === option.value}
            onChange={() => setDraft((value) => ({ ...value, verdict: option.value }))} />
          <span>{option.label}</span>
        </label>)}
      </div>
    </fieldset>

    <fieldset className="report-feedback-group" disabled={disabled}>
      <legend>问题类型<span className="report-feedback-optional">可多选</span></legend>
      <div className="feedback-tag-options">
        {FEEDBACK_TAGS.map((option) => <label key={option.value} className="feedback-tag"
          data-checked={draft.issue_tags.includes(option.value)}>
          <input type="checkbox" checked={draft.issue_tags.includes(option.value)}
            onChange={(event) => toggleTag(option.value, event.target.checked)} />
          {draft.issue_tags.includes(option.value) && <Check size={14} aria-hidden="true" />}
          <span>{option.label}</span>
        </label>)}
      </div>
    </fieldset>

    <label htmlFor={`${id}-comment`}>具体意见</label>
    <textarea id={`${id}-comment`} rows={5} maxLength={8000} disabled={disabled}
      placeholder="写明哪一段、什么问题、应该怎么改"
      value={draft.comment} onChange={(event) => setDraft((value) => ({ ...value, comment: event.target.value }))} />

    <div className="report-feedback-footer">
      <label className="report-feedback-reviewer">反馈人
        <input value={draft.reviewer_name} maxLength={40} disabled={disabled} autoComplete="name"
          onChange={(event) => setDraft((value) => ({ ...value, reviewer_name: event.target.value }))} />
      </label>
      <button type="button" className="report-primary" disabled={!canSave} aria-busy={saving}
        onClick={() => void save()}>
        <Check size={16} aria-hidden="true" />{saving ? "正在保存" : "保存反馈"}
      </button>
    </div>
    <p className="report-feedback-state" role="status">
      {loading ? "正在读取反馈" : isFilled(saved)
        ? `已保存第 ${saved.revision} 版 · ${savedAt(saved.updated_at)}${unchanged ? "" : " · 有未保存的修改"}`
        : "尚未填写"}
    </p>
  </section>;
}
