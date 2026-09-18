import { describe, expect, it } from "vitest";

import {
  buildChatSessionPayload,
  isServerSessionVersionFresh,
  mergeStrictSessionResult,
  type ChatSession,
} from "./useChatHistory";

function session(overrides: Partial<ChatSession> = {}): ChatSession {
  return {
    id: "session-1",
    title: "原标题",
    createdAt: 1,
    updatedAt: 100,
    messages: [
      { id: "assistant-json", role: "assistant", kind: "json", content: "旧事故" },
    ],
    draftJson: "旧事故",
    draftMeta: null,
    reportResult: null,
    linkedFiles: [],
    linkedArtifacts: [],
    ...overrides,
  };
}

describe("会话严格保存并发合并", () => {
  it("刷新迟到的旧 GET 不得回退已知的服务端版本", () => {
    expect(isServerSessionVersionFresh(199, 200)).toBe(false);
    expect(isServerSessionVersionFresh(200, 200)).toBe(true);
    expect(isServerSessionVersionFresh(201, 200)).toBe(true);
  });

  it("严格保存返回期间保留普通更新，并让后继整记录保存携带新草稿", () => {
    const base = session();
    const saved = session({
      updatedAt: 200,
      draftJson: "严格保存的新事故",
      messages: [
        { id: "assistant-json", role: "assistant", kind: "json", content: "严格保存的新事故" },
      ],
    });
    const current = session({
      title: "并发重命名",
      updatedAt: 150,
      messages: [
        ...base.messages,
        { id: "background", role: "assistant", kind: "text", content: "后台消息" },
      ],
    });

    const merged = mergeStrictSessionResult(base, saved, current, {
      draftJson: saved.draftJson,
      messages: saved.messages,
    });

    expect(merged.title).toBe("并发重命名");
    expect(merged.draftJson).toBe("严格保存的新事故");
    expect(merged.messages).toEqual([
      { id: "assistant-json", role: "assistant", kind: "json", content: "严格保存的新事故" },
      { id: "background", role: "assistant", kind: "text", content: "后台消息" },
    ]);
    expect(buildChatSessionPayload(merged, false).draft_json).toBe("严格保存的新事故");
  });

  it("用户在严格请求期间继续编辑时保留更新后的本地草稿", () => {
    const base = session();
    const saved = session({ draftJson: "较早的严格草稿" });
    const current = session({ draftJson: "用户继续编辑的新草稿" });

    const merged = mergeStrictSessionResult(base, saved, current, {
      draftJson: saved.draftJson,
    });

    expect(merged.draftJson).toBe("用户继续编辑的新草稿");
  });
});
