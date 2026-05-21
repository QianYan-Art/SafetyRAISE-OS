import { useEffect, useMemo, useRef, useState } from "react";

interface JsonTableEditorProps {
  initialJson: string;
  onConfirm: (validJsonString: string) => void;
  onAutoSave?: (validJsonString: string) => void | Promise<void>;
  disabled?: boolean;
  isGeneratingReport?: boolean;
  onCancelGenerate?: () => void;
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
  disabled,
  isGeneratingReport = false,
  onCancelGenerate,
}: JsonTableEditorProps) {
  const [data, setData] = useState<Record<string, string>>({});
  const [error, setError] = useState("");
  const lastSavedJsonRef = useRef("");

  useEffect(() => {
    try {
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
    } catch {
      setError("输入草稿格式异常，无法解析为表格。");
    }
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [initialJson]);

  const handleChange = (key: string, newValue: string) => {
    setData((prev) => ({
      ...prev,
      [key]: newValue,
    }));
  };

  const currentJsonString = useMemo(() => buildJsonString(data), [data]);

  const handleBlur = async () => {
    if (!onAutoSave) {
      return;
    }
    if (currentJsonString === lastSavedJsonRef.current) {
      return;
    }

    try {
      await onAutoSave(currentJsonString);
      lastSavedJsonRef.current = currentJsonString;
      setError("");
    } catch (err) {
      setError("自动保存失败：" + (err instanceof Error ? err.message : String(err)));
    }
  };

  const handleConfirm = () => {
    try {
      onConfirm(currentJsonString);
    } catch (err) {
      setError("无法生成有效的确认数据：" + (err instanceof Error ? err.message : String(err)));
    }
  };

  if (error) {
    return (
      <div className="error-text">
        {error}
        <br />
        <pre>{initialJson}</pre>
      </div>
    );
  }

  const entries = Object.entries(data);

  if (entries.length === 0) {
    return <p className="hint">暂无草稿数据。</p>;
  }

  return (
    <div>
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
                <input
                  type="text"
                  className="value-input"
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
          disabled={!isGeneratingReport}
          aria-hidden={!isGeneratingReport}
          tabIndex={isGeneratingReport ? 0 : -1}
        >
          停止
        </button>
        <button
          type="button"
          className={`btn-primary report-submit-btn ${isGeneratingReport ? "is-generating" : ""}`}
          onClick={handleConfirm}
          disabled={disabled}
        >
          {isGeneratingReport ? (
            <>
              <span className="spinner" />
              正在生成报告
            </>
          ) : "确认事故信息并生成报告"}
        </button>
      </div>
    </div>
  );
}
