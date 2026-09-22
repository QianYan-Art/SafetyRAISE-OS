import { useEffect, useMemo, useRef, useState } from "react";
import { useDialogFocus } from "./useDialogFocus";
import {
  ArrowLeft,
  BookOpen,
  FileText,
  Save,
  ScanLine,
  Settings2,
  X,
} from "lucide-react";
import type { LucideIcon } from "lucide-react";

import type {
  CapabilityConfigRecord,
  CapabilityConfigState,
  EmbeddingTuningParams,
  ModelCapability,
  UpdateCapabilityConfigsPayload,
} from "./types";
import "./account-workspace.css";

interface UserModelConfigDrawerProps {
  open: boolean;
  saving: boolean;
  state: CapabilityConfigState | null;
  errorMessage: string;
  username?: string;
  returnLabel?: string;
  onClose: () => void;
  onSave: (payload: UpdateCapabilityConfigsPayload) => Promise<void>;
}

const CAPABILITY_ORDER: ModelCapability[] = ["vision", "report", "embedding"];
const CAPABILITY_META: Record<ModelCapability, { title: string; tabLabel: string; purpose: string; icon: LucideIcon }> = {
  vision: {
    title: "视觉模型",
    tabLabel: "视觉识别",
    purpose: "从事故图片和视频中提取事故事实。",
    icon: ScanLine,
  },
  report: {
    title: "报告模型",
    tabLabel: "报告生成",
    purpose: "结合已保存事实、专家意见与知识库条文生成分析报告。",
    icon: FileText,
  },
  embedding: {
    title: "嵌入模型",
    tabLabel: "知识库检索",
    purpose: "为事故分析检索相关条文与规则。",
    icon: BookOpen,
  },
};

interface CapabilityDraft {
  baseUrl: string;
  modelName: string;
  apiKey: string;
  topK: string;
  denseChunks: string;
  denseRules: string;
}

function recordToDraft(record: CapabilityConfigRecord | undefined): CapabilityDraft {
  const params = record?.params ?? {};
  return {
    baseUrl: record?.base_url ?? "",
    modelName: record?.model_name ?? "",
    apiKey: "",
    topK: params.top_k != null ? String(params.top_k) : "",
    denseChunks: params.dense_top_k_chunks != null ? String(params.dense_top_k_chunks) : "",
    denseRules: params.dense_top_k_rules != null ? String(params.dense_top_k_rules) : "",
  };
}

function buildDraftMap(state: CapabilityConfigState | null): Record<ModelCapability, CapabilityDraft> {
  const lookup = new Map((state?.capabilities ?? []).map((item) => [item.capability, item]));
  return {
    vision: recordToDraft(lookup.get("vision")),
    report: recordToDraft(lookup.get("report")),
    embedding: recordToDraft(lookup.get("embedding")),
  };
}

function toNumberOrNull(value: string): number | null {
  const trimmed = value.trim();
  if (!trimmed) return null;
  const parsed = Number(trimmed);
  return Number.isFinite(parsed) ? Math.trunc(parsed) : null;
}

function validateIntegerField(value: string, label: string): string | null {
  if (!value.trim()) return null;
  const parsed = Number(value);
  if (!Number.isInteger(parsed) || parsed < 1 || parsed > 50) {
    return `${label}需填写 1 至 50 的整数。`;
  }
  return null;
}

export function UserModelConfigDrawer(props: UserModelConfigDrawerProps) {
  const { open, saving: externallySaving, state, errorMessage, username, returnLabel, onClose, onSave } = props;
  const dialogRef = useRef<HTMLDivElement>(null);
  useDialogFocus(dialogRef, open);
  const isAdmin = state?.role === "admin";
  const roleLabel = isAdmin ? "管理员账户配置" : "个人账户配置";
  const displayUsername = username?.trim();
  const accountLabel = displayUsername ? `${displayUsername} · ${roleLabel}` : roleLabel;
  const resolvedReturnLabel = returnLabel?.trim() || "返回工作区";
  const recordByCapability = useMemo(
    () => new Map((state?.capabilities ?? []).map((item) => [item.capability, item])),
    [state],
  );
  const embeddingDefaults = state?.system_defaults?.embedding ?? {};

  const [activeCapability, setActiveCapability] = useState<ModelCapability>("vision");
  const [draft, setDraft] = useState<Record<ModelCapability, CapabilityDraft>>(() => buildDraftMap(state));
  const [dirty, setDirty] = useState(false);
  const [localError, setLocalError] = useState("");
  const submittingRef = useRef(false);
  const [submitting, setSubmitting] = useState(false);
  const saving = externallySaving || submitting;

  useEffect(() => {
    if (!open) return;
    setActiveCapability("vision");
    setDraft(buildDraftMap(state));
    setDirty(false);
    setLocalError("");
  }, [open]);

  useEffect(() => {
    if (open && !dirty) {
      setDraft(buildDraftMap(state));
    }
  }, [dirty, open, state]);

  useEffect(() => {
    if (!open) return;
    const previousOverflow = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    const handleKeyDown = (event: KeyboardEvent) => {
      if (event.key !== "Escape" || saving || submittingRef.current) return;
      event.preventDefault();
      if (confirmDiscard()) onClose();
    };
    const handleBeforeUnload = (event: BeforeUnloadEvent) => {
      if (!dirty) return;
      event.preventDefault();
      event.returnValue = "";
    };
    window.addEventListener("keydown", handleKeyDown);
    window.addEventListener("beforeunload", handleBeforeUnload);
    return () => {
      document.body.style.overflow = previousOverflow;
      window.removeEventListener("keydown", handleKeyDown);
      window.removeEventListener("beforeunload", handleBeforeUnload);
    };
  }, [dirty, onClose, open, saving]);

  if (!open) return null;

  function confirmDiscard() {
    return !dirty || window.confirm("有未保存的模型配置，放弃这些修改？");
  }

  function handleClose() {
    if (saving || submittingRef.current) return;
    if (!confirmDiscard()) return;
    setDirty(false);
    onClose();
  }

  function handleCapabilityChange(capability: ModelCapability) {
    if (saving || submittingRef.current) return;
    if (capability === activeCapability) return;
    if (!confirmDiscard()) return;
    setDraft(buildDraftMap(state));
    setDirty(false);
    setLocalError("");
    setActiveCapability(capability);
  }

  function patch(capability: ModelCapability, field: keyof CapabilityDraft, value: string) {
    setDraft((current) => ({ ...current, [capability]: { ...current[capability], [field]: value } }));
    setDirty(true);
    setLocalError("");
  }

  function validateDraft() {
    for (const capability of [activeCapability]) {
      const value = draft[capability].baseUrl.trim();
      if (value && !/^https?:\/\//i.test(value)) {
        return `${CAPABILITY_META[capability].title}接口地址必须以 http:// 或 https:// 开头。`;
      }
    }

    if (!isAdmin && activeCapability !== "embedding") {
      for (const capability of [activeCapability]) {
        const hasExisting = Boolean(recordByCapability.get(capability)?.configured);
        if (!draft[capability].baseUrl.trim() && !hasExisting) {
          return `请先填写「${CAPABILITY_META[capability].title}」的接口地址。`;
        }
      }
    }

    const retrievalChecks: Array<[string, string]> = [
      [draft.embedding.topK, "top_k"],
      [draft.embedding.denseChunks, "dense 召回（chunks）"],
      [draft.embedding.denseRules, "dense 召回（rules）"],
    ];
    for (const [value, label] of activeCapability === "embedding" ? retrievalChecks : []) {
      const error = validateIntegerField(value, label);
      if (error) return error;
    }
    return null;
  }

  async function handleSubmit() {
    if (saving || submittingRef.current) return;
    const validationError = validateDraft();
    if (validationError) {
      setLocalError(validationError);
      return;
    }

    setLocalError("");
    const items = [activeCapability].map((capability) => {
      const value = draft[capability];
      const item: {
        capability: ModelCapability;
        base_url: string | null;
        model_name: string | null;
        api_key: string | null;
        params?: EmbeddingTuningParams | null;
      } = {
        capability,
        base_url: value.baseUrl.trim() || null,
        model_name: value.modelName.trim() || null,
        api_key: value.apiKey.trim() || null,
      };
      if (capability === "embedding") {
        item.params = {
          top_k: toNumberOrNull(value.topK),
          dense_top_k_chunks: toNumberOrNull(value.denseChunks),
          dense_top_k_rules: toNumberOrNull(value.denseRules),
        };
      }
      return item;
    });

    submittingRef.current = true;
    setSubmitting(true);
    try {
      await onSave({ items });
      setDirty(false);
    } catch {
      setLocalError("保存失败，当前修改仍保留。请检查提示后重试。");
    } finally {
      submittingRef.current = false;
      setSubmitting(false);
    }
  }

  function resetDraft() {
    if (saving || submittingRef.current) return;
    setDraft(buildDraftMap(state));
    setDirty(false);
    setLocalError("");
  }

  const activeMeta = CAPABILITY_META[activeCapability];
  const ActiveIcon = activeMeta.icon;
  const activeRecord = recordByCapability.get(activeCapability);
  const activeDraft = draft[activeCapability];
  const displayedError = localError || errorMessage;

  return (
    <div ref={dialogRef} className="account-overlay account-model-overlay" role="dialog" aria-modal="true" aria-label="模型配置">
      <section className="account-page account-model-page">
        <header className="account-topbar">
          <div className="account-topbar-title">
            <Settings2 aria-hidden="true" />
            <strong>账户设置</strong>
          </div>
          <div className="account-topbar-actions">
            <button type="button" className="account-button account-button-secondary account-back-button" onClick={handleClose} disabled={saving}>
              <ArrowLeft aria-hidden="true" />
              {resolvedReturnLabel}
            </button>
            <span className="account-topbar-account" title={accountLabel}>{accountLabel}</span>
            <button type="button" className="account-icon-button" onClick={handleClose} disabled={saving} aria-label="关闭账户设置" title="关闭">
              <X aria-hidden="true" />
            </button>
          </div>
        </header>

        <div className="account-model-heading">
          <div>
            <h1>模型配置</h1>
            <p>{accountLabel}</p>
          </div>
          <span className={`account-save-state${dirty ? " is-dirty" : ""}`}>
            {dirty ? "有未保存修改" : "无未保存修改"}
          </span>
        </div>

        <nav className="account-tabs" role="tablist" aria-label="模型用途">
          {CAPABILITY_ORDER.map((capability) => {
            const meta = CAPABILITY_META[capability];
            const Icon = meta.icon;
            return (
              <button
                key={capability}
                type="button"
                role="tab"
                aria-selected={capability === activeCapability}
                className="account-tab"
                onClick={() => handleCapabilityChange(capability)}
              >
                <Icon aria-hidden="true" />
                {meta.tabLabel}
              </button>
            );
          })}
        </nav>

        <main className="account-model-scroll">
          <form className="account-model-form" onSubmit={(event) => { event.preventDefault(); void handleSubmit(); }}>
            <div className="account-model-purpose">
              <span className="account-model-purpose-icon"><ActiveIcon aria-hidden="true" /></span>
              <div>
                <div className="account-model-title-row">
                  <h2>{activeMeta.title}</h2>
                  {activeRecord?.configured ? <span className="account-status-badge">已配置</span> : null}
                </div>
                <p>{activeMeta.purpose}</p>
              </div>
            </div>

            <p className="account-model-policy">
              {activeCapability === "embedding"
                ? isAdmin
                  ? "管理员未配置时使用系统默认。普通用户未单独配置知识库检索时，会沿用管理员的嵌入配置。"
                  : "未单独配置时沿用管理员的嵌入配置；没有可用管理员配置时使用系统默认。"
                : isAdmin
                  ? "管理员未配置时使用系统默认。此处只修改当前账户，不替其他用户配置视觉或报告模型。"
                  : "开始分析前需要配置自己的视觉模型和报告模型。"}
            </p>

            {displayedError ? (
              <div className="account-alert account-alert-error" role="alert">{displayedError}</div>
            ) : null}

            <div className="account-field-grid">
              <label className="account-field">
                <span>接口地址</span>
                <input
                  className="account-input"
                  type="url"
                  maxLength={255}
                  value={activeDraft.baseUrl}
                  onChange={(event) => patch(activeCapability, "baseUrl", event.target.value)}
                  placeholder="例如 https://api.openai.com/v1"
                  disabled={saving}
                />
                <small>填到 /v1 即可，系统会自动补全 /chat/completions。</small>
              </label>

              <label className="account-field">
                <span>模型名称</span>
                <input
                  className="account-input"
                  type="text"
                  maxLength={128}
                  value={activeDraft.modelName}
                  onChange={(event) => patch(activeCapability, "modelName", event.target.value)}
                  placeholder="输入服务商提供的模型标识"
                  disabled={saving}
                />
              </label>

              <label className="account-field account-field-wide">
                <span>API 密钥</span>
                <input
                  className="account-input"
                  type="password"
                  maxLength={255}
                  value={activeDraft.apiKey}
                  onChange={(event) => patch(activeCapability, "apiKey", event.target.value)}
                  placeholder={activeRecord?.api_key_masked ? `已配置 ${activeRecord.api_key_masked}，留空保留` : "输入服务商提供的密钥"}
                  autoComplete="new-password"
                  disabled={saving}
                />
                <small>{activeRecord?.api_key_masked ? "留空不替换现有密钥；填写新值后更新。" : "密钥只提交给现有账户配置接口，不在界面中回显。"}</small>
              </label>
            </div>

            {activeCapability === "embedding" ? (
              <fieldset className="account-retrieval-fields">
                <legend>检索参数</legend>
                <div className="account-retrieval-grid">
                  <label className="account-field">
                    <span>top_k（最终片段数）</span>
                    <input
                      className="account-input"
                      type="number"
                      min={1}
                      max={50}
                      value={activeDraft.topK}
                      onChange={(event) => patch("embedding", "topK", event.target.value)}
                      placeholder={embeddingDefaults.top_k != null ? `系统默认 ${embeddingDefaults.top_k}` : "系统默认"}
                      disabled={saving}
                    />
                  </label>
                  <label className="account-field">
                    <span>dense 召回（chunks）</span>
                    <input
                      className="account-input"
                      type="number"
                      min={1}
                      max={50}
                      value={activeDraft.denseChunks}
                      onChange={(event) => patch("embedding", "denseChunks", event.target.value)}
                      placeholder={embeddingDefaults.dense_top_k_chunks != null ? `系统默认 ${embeddingDefaults.dense_top_k_chunks}` : "系统默认"}
                      disabled={saving}
                    />
                  </label>
                  <label className="account-field">
                    <span>dense 召回（rules）</span>
                    <input
                      className="account-input"
                      type="number"
                      min={1}
                      max={50}
                      value={activeDraft.denseRules}
                      onChange={(event) => patch("embedding", "denseRules", event.target.value)}
                      placeholder={embeddingDefaults.dense_top_k_rules != null ? `系统默认 ${embeddingDefaults.dense_top_k_rules}` : "系统默认"}
                      disabled={saving}
                    />
                  </label>
                </div>
              </fieldset>
            ) : null}
          </form>
        </main>

        <footer className="account-model-footer">
          <span>{dirty ? "修改尚未提交" : "配置已与当前页面同步"}</span>
          <div className="account-footer-actions">
            <button type="button" className="account-button account-button-secondary" onClick={resetDraft} disabled={saving || !dirty}>
              撤销修改
            </button>
            <button type="button" className="account-button account-button-primary" onClick={() => void handleSubmit()} disabled={saving || !dirty}>
              <Save aria-hidden="true" />
              {saving ? "保存中..." : "保存配置"}
            </button>
          </div>
        </footer>
      </section>
    </div>
  );
}
