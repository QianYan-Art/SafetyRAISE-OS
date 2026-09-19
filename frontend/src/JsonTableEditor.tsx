import { useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";
import type { TextareaHTMLAttributes } from "react";

interface JsonTableEditorProps {
  initialJson: string;
  onConfirm: (validJsonString: string) => void | Promise<void>;
  onAutoSave?: (validJsonString: string) => void | Promise<void>;
  onDraftChange?: (jsonString: string) => void;
  resetKey?: string;
  disabled?: boolean;
  isGeneratingReport?: boolean;
  isCancellingReport?: boolean;
  generationStatusLabel?: string;
  onCancelGenerate?: () => void;
  confirmLabel?: string;
}

function GrowingValueInput(props: TextareaHTMLAttributes<HTMLTextAreaElement>) {
  const ref = useRef<HTMLTextAreaElement>(null);
  const resize = () => {
    const element = ref.current;
    if (!element) return;
    element.style.height = "auto";
    element.style.height = `${element.scrollHeight + element.offsetHeight - element.clientHeight}px`;
  };
  useLayoutEffect(resize, [props.value]);
  useEffect(() => {
    const element = ref.current;
    if (!element || typeof ResizeObserver === "undefined") return;
    let width = element.getBoundingClientRect().width;
    const observer = new ResizeObserver(([entry]) => {
      if (entry.contentRect.width !== width) {
        width = entry.contentRect.width;
        resize();
      }
    });
    observer.observe(element);
    return () => observer.disconnect();
  }, []);
  return <textarea {...props} ref={ref} rows={1} />;
}

function parseJsonToStringMap(initialJson: string): Record<string, string> {
  if (!initialJson) {
    return {};
  }
  const parsed = JSON.parse(initialJson);
  const stringifiedMap: Record<string, string> = {};
  for (const key in parsed) {
    if (Object.prototype.hasOwnProperty.call(parsed, key)) {
      const val = parsed[key];
      stringifiedMap[key] = typeof val === "object" ? JSON.stringify(val) : String(val);
    }
  }
  return stringifiedMap;
}

function buildJsonString(data: Record<string, string>): string {
  const result: Record<string, unknown> = {};
  for (const [key, val] of Object.entries(data)) {
    try {
      if (val === "null") result[key] = null;
      else if (val === "true") result[key] = true;
      else if (val === "false") result[key] = false;
      else if (!isNaN(Number(val)) && val.trim() !== "") result[key] = Number(val);
      else if ((val.startsWith("{") && val.endsWith("}")) || (val.startsWith("[") && val.endsWith("]"))) {
        result[key] = JSON.parse(val);
      } else {
        result[key] = val;
      }
    } catch {
      result[key] = val;
    }
  }
  return JSON.stringify(result, null, 2);
}

export function JsonTableEditor({
  initialJson,
  onConfirm,
  onAutoSave,
  onDraftChange,
  resetKey,
  disabled,
  isGeneratingReport = false,
  isCancellingReport = false,
  generationStatusLabel = "正在生成报告",
  onCancelGenerate,
  confirmLabel = "确认事故信息并生成报告",
}: JsonTableEditorProps) {
  const [data, setData] = useState<Record<string, string>>({});
  const [error, setError] = useState("");
  const lastSavedJsonRef = useRef("");
  const initializedRef = useRef(false);
  const currentJsonRef = useRef("");
  const resetKeyRef = useRef(resetKey);

  useEffect(() => {
    try {
      const resetKeyChanged = resetKeyRef.current !== resetKey;
      if (resetKeyChanged) {
        resetKeyRef.current = resetKey;
        initializedRef.current = false;
      }
      if (!resetKeyChanged && initializedRef.current && buildJsonString(data) !== lastSavedJsonRef.current) {
        return;
      }
      if (initialJson) {
        const stringifiedMap = parseJsonToStringMap(initialJson);
        const nextJsonString = buildJsonString(stringifiedMap);
        if (nextJsonString !== buildJsonString(data)) {
          setData(stringifiedMap);
        }
        lastSavedJsonRef.current = nextJsonString;
        setError("");
      } else {
        setData({});
        lastSavedJsonRef.current = "";
      }
      initializedRef.current = true;
    } catch {
      setError("输入草稿格式异常，无法解析为表格。");
    }
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [initialJson, resetKey]);

  const handleChange = (key: string, newValue: string) => {
    const nextData = {
      ...data,
      [key]: newValue,
    };
    setData(nextData);
    setError("");
    const nextJsonString = buildJsonString(nextData);
    currentJsonRef.current = nextJsonString;
    onDraftChange?.(nextJsonString);
  };

  const currentJsonString = useMemo(() => buildJsonString(data), [data]);
  currentJsonRef.current = currentJsonString;

  const handleBlur = async () => {
    if (!onAutoSave) {
      return;
    }
    if (currentJsonString === lastSavedJsonRef.current) {
      return;
    }

    try {
      await onAutoSave(currentJsonString);
      if (currentJsonRef.current === currentJsonString) {
        lastSavedJsonRef.current = currentJsonString;
      }
      setError("");
    } catch (err) {
      setError("自动保存失败：" + (err instanceof Error ? err.message : String(err)));
    }
  };

  const handleConfirm = async () => {
    try {
      await onConfirm(currentJsonString);
    } catch (err) {
      setError("无法生成有效的确认数据：" + (err instanceof Error ? err.message : String(err)));
    }
  };

  const renderError = error ? (
    <div className="error-text" role="alert">
      {error}
    </div>
  ) : null;

  if (error && !initializedRef.current) {
    return renderError;
  }

  const entries = Object.entries(data);

  if (entries.length === 0) {
    return (
      <div>
        {renderError}
        <p className="hint">暂无草稿数据。</p>
      </div>
    );
  }

  return (
    <div>
      {renderError}
      <table className="json-table-editor">
        <thead>
          <tr>
            <th>信息字段</th>
            <th>内容</th>
          </tr>
        </thead>
        <tbody>
          {entries.map(([key, val]) => (
            <tr key={key}>
              <td className="key-cell">{key}</td>
              <td>
                <GrowingValueInput
                  className="value-input"
                  aria-label={key}
                  title={val}
                  value={val}
                  onChange={(e) => handleChange(key, e.target.value)}
                  onBlur={handleBlur}
                  disabled={disabled}
                  placeholder="[空]"
                />
              </td>
            </tr>
          ))}
        </tbody>
      </table>
      <div className={`report-action-dock ${isGeneratingReport ? "is-generating" : ""}`}>
        <button
          type="button"
          className="btn-danger report-stop-btn"
          onClick={onCancelGenerate}
          disabled={!isGeneratingReport || isCancellingReport}
          aria-busy={isCancellingReport}
          aria-hidden={!isGeneratingReport}
          tabIndex={isGeneratingReport ? 0 : -1}
        >
          {isCancellingReport ? "正在停止" : "停止"}
        </button>
        <button
          type="button"
          className={`btn-primary report-submit-btn ${isGeneratingReport ? "is-generating" : ""}`}
          onClick={() => void handleConfirm()}
          disabled={disabled}
        >
          {isGeneratingReport ? (
            <>
              <span className="spinner" />
              {generationStatusLabel}
            </>
          ) : confirmLabel}
        </button>
      </div>
    </div>
  );
}
