import type { ReportRunEvent } from "./types";

export type ReportProgressState = "running" | "done" | "error";

export interface ReportProgressStep {
  key: string;
  label: string;
  state: ReportProgressState;
  startedAt: number;
  endedAt: number | null;
}

const ERROR_LABELS: Record<string, string> = {
  completion_unknown: "模型请求未收到结果，已暂停",
  unknown_cost_ack_required: "存在费用未知的请求，需确认后继续",
  invalid_review_or_candidate: "审查结果未通过程序终检",
  budget_exhausted: "达到预算或时长上限",
  resource_pressure: "服务器资源不足，已暂停",
};

function text(value: unknown): string {
  return typeof value === "string" ? value : "";
}

function stepLabel(event: ReportRunEvent, candidates: number, round: number): string | null {
  const data = event.data ?? {};
  if (event.type === "request" && data.status === "dispatched") {
    if (data.role === "expert") return "专家模型分析事故";
    if (data.role === "generator") return round > 0 ? `按审查意见修订（第 ${round} 轮）` : "撰写报告";
    if (data.role === "reviewer") return `独立审查第 ${Math.max(candidates, 1)} 版`;
    return null;
  }
  if (event.type === "tool" && data.status === "completed") {
    if (data.name === "search_knowledge") return data.role === "reviewer" ? "审查检索法律知识" : "检索法律知识";
    if (data.name === "read_knowledge") return data.role === "reviewer" ? "审查核对法条原文" : "读取法条原文";
  }
  return null;
}

/** 把持久化事件折叠成可读的进度步骤；只用事件中的公开字段，不推测模型内容。 */
export function summarizeReportProgress(events: ReportRunEvent[], active: boolean): ReportProgressStep[] {
  const steps: ReportProgressStep[] = [];
  let candidates = 0;
  let round = 0;
  const close = (at: number) => {
    const last = steps[steps.length - 1];
    if (last && last.endedAt === null) {
      last.endedAt = at;
      if (last.state === "running") last.state = "done";
    }
  };
  const push = (event: ReportRunEvent, label: string, state: ReportProgressState, instant = false) => {
    const at = Date.parse(event.occurred_at);
    const last = steps[steps.length - 1];
    // 同一步骤的连续模型调用合并显示，避免工具往返把列表刷得过长。
    if (!instant && last && last.endedAt === null && last.label === label) return;
    close(at);
    steps.push({ key: `${event.seq}`, label, state, startedAt: at, endedAt: instant ? at : null });
  };
  for (const event of events) {
    const data = event.data ?? {};
    if (event.type === "stage" && typeof data.revision_round === "number") round = data.revision_round;
    if (event.type === "checkpoint" && data.step === "candidate") {
      candidates += 1;
      push(event, `完成第 ${candidates} 版报告`, "done", true);
      continue;
    }
    if (event.type === "review") {
      push(event, `第 ${Math.max(candidates, 1)} 版审查结束`, "done", true);
      continue;
    }
    if (event.type === "error") {
      const reason = text(data.reason);
      push(event, ERROR_LABELS[reason] ?? `运行停止（${reason || "未知原因"}）`, "error", true);
      continue;
    }
    const label = stepLabel(event, candidates, round);
    if (!label) continue;
    const instant = event.type === "tool";
    push(event, label, instant ? "done" : "running", instant);
  }
  if (!active) {
    const last = steps[steps.length - 1];
    if (last && last.endedAt === null) {
      const final = events.length ? Date.parse(events[events.length - 1].occurred_at) : last.startedAt;
      last.endedAt = Math.max(final, last.startedAt);
      last.state = "done";
    }
  }
  return steps;
}

export function formatDuration(ms: number): string {
  const seconds = Math.max(0, Math.round(ms / 1000));
  const hours = Math.floor(seconds / 3600);
  const minutes = Math.floor((seconds % 3600) / 60);
  const rest = seconds % 60;
  if (hours > 0) return `${hours} 小时 ${minutes} 分`;
  return minutes > 0 ? `${minutes} 分 ${rest} 秒` : `${rest} 秒`;
}
