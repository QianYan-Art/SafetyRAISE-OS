import { useEffect, useState } from "react";
import { formatDuration, type ReportProgressStep } from "./reportProgress";

export interface ReportProgressSummary {
  steps: ReportProgressStep[];
  active: boolean;
  status: string;
}

/** 档案处理记录中的报告生成过程：沿用资料阶段进度卡的样式，运行中每秒刷新当前步骤用时。 */
export function ReportProgressCard({ progress }: { progress: ReportProgressSummary }) {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    if (!progress.active) return;
    const timer = setInterval(() => setNow(Date.now()), 1000);
    return () => clearInterval(timer);
  }, [progress.active]);
  const { steps, active, status } = progress;
  const failed = !active && steps.some((step) => step.state === "error") && steps[steps.length - 1]?.state === "error";
  const total = steps.length
    ? (active ? now : steps[steps.length - 1].endedAt ?? now) - steps[0].startedAt : 0;
  return <div className="chat-bubble assistant progress-bubble">
    <div className={`progress-card ${active ? "running" : failed ? "error" : "success"}`}>
      <div className="progress-card-topline">
        <span className="progress-badge">报告阶段</span>
        <div className="progress-card-status">
          {active ? <span className="spinner spinner-dark" />
            : <span className={`progress-indicator ${failed ? "error" : "success"}`}>{failed ? "!" : "✓"}</span>}
        </div>
      </div>
      <div className="progress-card-main">
        <strong className="progress-card-title">{status}</strong>
        <p className="progress-card-content">{active ? "已用时" : "共用时"} {formatDuration(total)}{active ? "，刷新或关闭页面不会中断生成。" : ""}</p>
      </div>
      <ol className="progress-stage-list" aria-live="polite">
        {steps.map((step) => {
          const time = step.state === "running" ? `进行中 · ${formatDuration(now - step.startedAt)}`
            : step.endedAt !== null && step.endedAt > step.startedAt ? formatDuration(step.endedAt - step.startedAt) : "";
          return <li key={step.key} className={`progress-stage-item ${step.state === "running" ? "running" : "done"}${step.state === "error" ? " is-error" : ""}`}>
            <span className="progress-stage-dot" />
            <span className="progress-stage-label">{step.label}</span>
            {time && <span className="progress-stage-time">{time}</span>}
          </li>;
        })}
      </ol>
    </div>
  </div>;
}
