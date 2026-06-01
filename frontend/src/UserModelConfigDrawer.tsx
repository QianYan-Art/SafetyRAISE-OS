import { useEffect, useMemo, useState } from "react";

import type {
  CapabilityConfigRecord,
  CapabilityConfigState,
  EmbeddingTuningParams,
  ModelCapability,
  UpdateCapabilityConfigsPayload,
} from "./types";

interface UserModelConfigDrawerProps {
  open: boolean;
  saving: boolean;
  state: CapabilityConfigState | null;
  errorMessage: string;
  onClose: () => void;
  onSave: (payload: UpdateCapabilityConfigsPayload) => Promise<void>;
}

const CAPABILITY_ORDER: ModelCapability[] = ["vision", "embedding", "report"];
const CAPABILITY_META: Record<ModelCapability, { title: string; hint: string }> = {
  vision: {
    title: "视觉模型",
    hint: "用于把事故图片/视频识别为结构化事故信息。仅支持标准 OpenAI 兼容接口。",
  },
  embedding: {
    title: "嵌入模型",
    hint: "用于知识库检索的向量化。普通用户留空则沿用管理员配置；也可只调下方检索参数。",
  },
  report: {
    title: "报告生成模型",
    hint: "用于生成最终分析报告（自动捕获并清洗思维链）。仅支持标准 OpenAI 兼容接口。",
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

function emptyDraft(): CapabilityDraft {
  return { baseUrl: "", modelName: "", apiKey: "", topK: "", denseChunks: "", denseRules: "" };
}

function recordToDraft(record: CapabilityConfigRecord | undefined): CapabilityDraft {
  const params = record?.params ?? {};
  return {
    baseUrl: record?.base_url ?? "",
    modelName: record?.model_name ?? "",
    apiKey: "", // 始终空：展示打码值作提示，留空表示保留原 key
    topK: params.top_k != null ? String(params.top_k) : "",
    denseChunks: params.dense_top_k_chunks != null ? String(params.dense_top_k_chunks) : "",
    denseRules: params.dense_top_k_rules != null ? String(params.dense_top_k_rules) : "",
  };
}

function buildDraftMap(state: CapabilityConfigState | null): Record<ModelCapability, CapabilityDraft> {
  const lookup = new Map((state?.capabilities ?? []).map((item) => [item.capability, item]));
  return {
    vision: recordToDraft(lookup.get("vision")),
    embedding: recordToDraft(lookup.get("embedding")),
    report: recordToDraft(lookup.get("report")),
  };
}

function toNumberOrNull(value: string): number | null {
  const trimmed = value.trim();
  if (!trimmed) return null;
  const parsed = Number(trimmed);
  return Number.isFinite(parsed) ? Math.trunc(parsed) : null;
}

export function UserModelConfigDrawer(props: UserModelConfigDrawerProps) {
  const { open, saving, state, errorMessage, onClose, onSave } = props;
  const isAdmin = state?.role === "admin";
  const recordByCapability = useMemo(
    () => new Map((state?.capabilities ?? []).map((item) => [item.capability, item])),
    [state],
  );
  const embeddingDefaults: EmbeddingTuningParams = state?.system_defaults?.embedding ?? {};

  const [draft, setDraft] = useState<Record<ModelCapability, CapabilityDraft>>(() => buildDraftMap(state));
  const [localError, setLocalError] = useState("");

  useEffect(() => {
    if (!open) return;
    setDraft(buildDraftMap(state));
    setLocalError("");
  }, [open, state]);

  if (!open) return null;

  function patch(capability: ModelCapability, field: keyof CapabilityDraft, value: string) {
    setDraft((current) => ({ ...current, [capability]: { ...current[capability], [field]: value } }));
  }

  async function handleSubmit() {
    setLocalError("");
    if (!isAdmin) {
      // 普通用户：视觉、报告必须填 URL
      for (const cap of ["vision", "report"] as ModelCapability[]) {
        const hasExisting = Boolean(recordByCapability.get(cap)?.configured);
        if (!draft[cap].baseUrl.trim() && !hasExisting) {
          setLocalError(`请先填写「${CAPABILITY_META[cap].title}」的 URL。`);
          return;
        }
      }
    }

    const items = CAPABILITY_ORDER.map((cap) => {
      const d = draft[cap];
      const base: {
        capability: ModelCapability;
        base_url: string | null;
        model_name: string | null;
        api_key: string | null;
        params?: EmbeddingTuningParams | null;
      } = {
        capability: cap,
        base_url: d.baseUrl.trim() || null,
        model_name: d.modelName.trim() || null,
        api_key: d.apiKey.trim() || null, // 留空 → 后端保留原 key
      };
      if (cap === "embedding") {
        base.params = {
          top_k: toNumberOrNull(d.topK),
          dense_top_k_chunks: toNumberOrNull(d.denseChunks),
          dense_top_k_rules: toNumberOrNull(d.denseRules),
        };
      }
      return base;
    });

    await onSave({ items });
  }

  return (
    <div className="drawer-overlay" onClick={onClose}>
      <aside className="drawer-shell" onClick={(event) => event.stopPropagation()}>
        <div className="drawer-header">
          <div>
            <span className="drawer-kicker">模型接入设置</span>
            <h3>配置你的模型接入点</h3>
            <p>
              {isAdmin
                ? "管理员：这里保存的三能力配置就是系统测试时默认使用的端点。专家小模型由系统固定，不可设置。"
                : "视觉与报告模型必须填写；嵌入留空则沿用管理员配置。仅支持标准 OpenAI 兼容接口，密钥仅存于你的账号。"}
            </p>
          </div>
          <button type="button" className="btn-icon drawer-close-btn" onClick={onClose} aria-label="关闭抽屉">
            ×
          </button>
        </div>

        <div className="drawer-body">
          {localError || errorMessage ? (
            <div className="auth-alert auth-alert-error">{localError || errorMessage}</div>
          ) : null}

          {CAPABILITY_ORDER.map((cap) => {
            const meta = CAPABILITY_META[cap];
            const record = recordByCapability.get(cap);
            const maskedKey = record?.api_key_masked || "";
            return (
              <section key={cap} className="model-config-card">
                <div className="model-config-card-top">
                  <div>
                    <span className="model-config-slot-label">{meta.title}</span>
                    <p>{meta.hint}</p>
                  </div>
                  {record?.configured ? <span className="badge badge-configured">已配置</span> : null}
                </div>

                <div className="form-field">
                  <label htmlFor={`base-url-${cap}`}>模型 URL</label>
                  <input
                    id={`base-url-${cap}`}
                    className="form-input"
                    type="text"
                    maxLength={255}
                    value={draft[cap].baseUrl}
                    onChange={(e) => patch(cap, "baseUrl", e.target.value)}
                    placeholder="例如 https://openrouter.ai/api/v1/chat/completions"
                    disabled={saving}
                  />
                </div>

                <div className="form-field">
                  <label htmlFor={`model-name-${cap}`}>模型名称</label>
                  <input
                    id={`model-name-${cap}`}
                    className="form-input"
                    type="text"
                    maxLength={128}
                    value={draft[cap].modelName}
                    onChange={(e) => patch(cap, "modelName", e.target.value)}
                    placeholder="例如 deepseek/deepseek-v4-pro"
                    disabled={saving}
                  />
                </div>

                <div className="form-field">
                  <label htmlFor={`api-key-${cap}`}>模型 Key</label>
                  <input
                    id={`api-key-${cap}`}
                    className="form-input"
                    type="password"
                    maxLength={255}
                    value={draft[cap].apiKey}
                    onChange={(e) => patch(cap, "apiKey", e.target.value)}
                    placeholder={maskedKey ? `已配置 ${maskedKey}（留空不变）` : "仅保存在你的账号配置中"}
                    disabled={saving}
                  />
                </div>

                {cap === "embedding" ? (
                  <div className="model-config-params">
                    <div className="form-field">
                      <label htmlFor="embed-top-k">top_k（最终片段数）</label>
                      <input
                        id="embed-top-k"
                        className="form-input"
                        type="number"
                        min={1}
                        max={20}
                        value={draft.embedding.topK}
                        onChange={(e) => patch("embedding", "topK", e.target.value)}
                        placeholder={embeddingDefaults.top_k != null ? `系统默认 ${embeddingDefaults.top_k}` : "系统默认"}
                        disabled={saving}
                      />
                    </div>
                    <div className="form-field">
                      <label htmlFor="embed-dense-chunks">dense 召回（chunks）</label>
                      <input
                        id="embed-dense-chunks"
                        className="form-input"
                        type="number"
                        min={1}
                        max={50}
                        value={draft.embedding.denseChunks}
                        onChange={(e) => patch("embedding", "denseChunks", e.target.value)}
                        placeholder={
                          embeddingDefaults.dense_top_k_chunks != null
                            ? `系统默认 ${embeddingDefaults.dense_top_k_chunks}`
                            : "系统默认"
                        }
                        disabled={saving}
                      />
                    </div>
                    <div className="form-field">
                      <label htmlFor="embed-dense-rules">dense 召回（rules）</label>
                      <input
                        id="embed-dense-rules"
                        className="form-input"
                        type="number"
                        min={1}
                        max={50}
                        value={draft.embedding.denseRules}
                        onChange={(e) => patch("embedding", "denseRules", e.target.value)}
                        placeholder={
                          embeddingDefaults.dense_top_k_rules != null
                            ? `系统默认 ${embeddingDefaults.dense_top_k_rules}`
                            : "系统默认"
                        }
                        disabled={saving}
                      />
                    </div>
                  </div>
                ) : null}
              </section>
            );
          })}
        </div>

        <div className="drawer-footer">
          <button type="button" className="btn-secondary" onClick={onClose} disabled={saving}>
            取消
          </button>
          <button type="button" className="btn-primary" onClick={() => void handleSubmit()} disabled={saving}>
            {saving ? "保存中..." : "保存配置"}
          </button>
        </div>
      </aside>
    </div>
  );
}
