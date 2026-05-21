import type { ChatMessage, ChatMessageMeta, ChatProgressStage, ChatRole } from "./types";

export const REPORT_PROGRESS_LABELS = [
  "正在生成专家指导意见",
  "正在检索首轮知识片段",
  "正在进行多轮补充检索",
  "正在整理最终报告正文",
];

export function createMessage(
  role: ChatRole,
  kind: ChatMessage["kind"],
  content: string,
  meta: ChatMessageMeta | null = null,
): ChatMessage {
  return {
    id: `${role}-${Date.now()}-${Math.random().toString(16).slice(2)}`,
    role,
    kind,
    content,
    meta,
  };
}

export function buildProgressStages(
  labels: string[],
  activeIndex: number,
  status: ChatMessageMeta["status"],
): ChatProgressStage[] {
  return labels.map((label, index) => {
    if (status === "success") {
      return { label, state: "done" };
    }
    if (index < activeIndex) {
      return { label, state: "done" };
    }
    if (index === activeIndex) {
      return { label, state: "running" };
    }
    return { label, state: "pending" };
  });
}
