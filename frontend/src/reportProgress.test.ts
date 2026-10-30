import { describe, expect, it } from "vitest";
import { formatDuration, summarizeReportProgress } from "./reportProgress";
import type { ReportRunEvent } from "./types";

let seq = 0;
function event(type: string, at: string, data: Record<string, unknown>): ReportRunEvent {
  seq += 1;
  return { run_id: "run", seq, type, state_version: seq, occurred_at: `2026-10-30T16:${at}Z`, data };
}

const events = [
  event("request", "41:01", { role: "expert", status: "dispatched" }),
  event("tool", "41:59", { name: "search_knowledge", role: "generator", status: "completed" }),
  event("request", "42:00", { role: "generator", status: "dispatched" }),
  event("tool", "44:13", { name: "read_knowledge", role: "generator", status: "completed" }),
  event("request", "44:14", { role: "generator", status: "dispatched" }),
  event("checkpoint", "48:39", { step: "candidate" }),
  event("request", "48:39", { role: "reviewer", status: "dispatched" }),
  event("review", "57:25", { candidate_version: 1 }),
  event("stage", "57:25", { stage: "revising", revision_round: 1 }),
  event("request", "57:26", { role: "generator", status: "dispatched" }),
  event("request", "57:27", { role: "embedding", status: "dispatched" }),
];

describe("summarizeReportProgress", () => {
  it("turns persisted events into readable steps with durations", () => {
    const steps = summarizeReportProgress(events, true);
    expect(steps.map((step) => step.label)).toEqual([
      "专家模型分析事故", "检索法律知识", "撰写报告", "读取法条原文", "撰写报告",
      "完成第 1 版报告", "独立审查第 1 版", "第 1 版审查结束", "按审查意见修订（第 1 轮）",
    ]);
    expect(steps[0]).toMatchObject({ state: "done", endedAt: Date.parse("2026-10-30T16:41:59Z") });
    expect(steps[steps.length - 1]).toMatchObject({ state: "running", endedAt: null });
  });

  it("closes the last step and reports stop reasons when the run ends", () => {
    const stopped = [...events, event("error", "59:00", { reason: "invalid_review_or_candidate" })];
    const steps = summarizeReportProgress(stopped, false);
    expect(steps.every((step) => step.state !== "running")).toBe(true);
    expect(steps[steps.length - 1]).toMatchObject({ label: "审查结果未通过程序终检", state: "error" });
  });

  it("formats durations in minutes and seconds", () => {
    expect(formatDuration(58_000)).toBe("58 秒");
    expect(formatDuration(133_000)).toBe("2 分 13 秒");
    expect(formatDuration(7_120_000)).toBe("1 小时 58 分");
  });
});
