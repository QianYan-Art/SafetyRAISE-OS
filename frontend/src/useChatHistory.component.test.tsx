// @vitest-environment jsdom

import { act, cleanup, render, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { useChatHistory, type ChatSession } from "./useChatHistory";
import type { ChatSessionApiRecord, ChatSessionUpsertPayload } from "./types";

const api = vi.hoisted(() => ({
  createChatSession: vi.fn(),
  deleteChatSession: vi.fn(),
  fetchChatSession: vi.fn(),
  formatApiErrorMessage: vi.fn(),
  listChatSessions: vi.fn(),
  sendChatSessionSnapshot: vi.fn(),
  updateChatSession: vi.fn(),
}));

vi.mock("./api", async () => ({
  ...(await vi.importActual<typeof import("./api")>("./api")),
  ...api,
}));

function record(overrides: Partial<ChatSessionApiRecord> = {}): ChatSessionApiRecord {
  return {
    id: "session-1",
    title: "原标题",
    created_at: 1,
    updated_at: 100,
    session_state: "draft",
    messages: [],
    draft_json: "旧草稿",
    draft_meta: null,
    report_result: null,
    linked_files: [],
    linked_artifacts: [],
    ...overrides,
  };
}

function recordFromPayload(payload: ChatSessionUpsertPayload, updatedAt: number): ChatSessionApiRecord {
  return record({
    id: payload.id ?? "session-1",
    title: payload.title,
    created_at: payload.created_at ?? 1,
    updated_at: updatedAt,
    sort_order: payload.sort_order,
    source_type: payload.source_type,
    source_name: payload.source_name,
    messages: payload.messages,
    draft_json: payload.draft_json,
    draft_meta: payload.draft_meta,
    report_result: payload.report_result,
  });
}

function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (reason?: unknown) => void;
  const promise = new Promise<T>((promiseResolve, promiseReject) => {
    resolve = promiseResolve;
    reject = promiseReject;
  });
  return { promise, resolve, reject };
}

let hookValue: ReturnType<typeof useChatHistory> | null = null;

function Probe() {
  hookValue = useChatHistory({ id: "user-1" });
  return null;
}

function currentHook(): ReturnType<typeof useChatHistory> {
  if (!hookValue) {
    throw new Error("hook 尚未渲染");
  }
  return hookValue;
}

async function waitForLoaded() {
  await waitFor(() => expect(currentHook().isLoaded).toBe(true));
}

beforeEach(() => {
  vi.clearAllMocks();
  localStorage.clear();
  vi.spyOn(console, "error").mockImplementation(() => undefined);
  api.listChatSessions.mockResolvedValue([record()]);
  api.fetchChatSession.mockResolvedValue(record());
  api.createChatSession.mockImplementation(async (payload: ChatSessionUpsertPayload) => (
    recordFromPayload(payload, 200)
  ));
  api.updateChatSession.mockImplementation(async (_sessionId: string, payload: ChatSessionUpsertPayload) => (
    recordFromPayload(payload, 200)
  ));
  api.deleteChatSession.mockResolvedValue(undefined);
  api.sendChatSessionSnapshot.mockReturnValue(false);
  api.formatApiErrorMessage.mockImplementation((error: unknown, fallback: string) => (
    error instanceof Error ? error.message : fallback
  ));
});

afterEach(() => {
  cleanup();
  hookValue = null;
  vi.restoreAllMocks();
});

describe("useChatHistory 持久化交互", () => {
  it("普通已有会话同步携带 expected_updated_at CAS 版本", async () => {
    render(<Probe />);
    await waitForLoaded();

    act(() => {
      currentHook().updateSessionById("session-1", { draftJson: "新的草稿" });
    });

    await waitFor(() => expect(api.updateChatSession).toHaveBeenCalledTimes(1));
    const payload = api.updateChatSession.mock.calls[0][1] as ChatSessionUpsertPayload;
    expect(payload.expected_updated_at).toBe(100);
    expect(payload.draft_json).toBe("新的草稿");
  });

  it("flush 失败会抛给调用方，并保留本地未保存草稿", async () => {
    api.updateChatSession.mockRejectedValue(new Error("版本冲突"));
    render(<Probe />);
    await waitForLoaded();

    act(() => {
      currentHook().updateSessionById("session-1", { draftJson: "本地草稿" });
    });

    let thrown: unknown;
    await act(async () => {
      try {
        await currentHook().flushSessionById("session-1");
      } catch (error) {
        thrown = error;
      }
    });

    expect(thrown).toEqual(new Error("版本冲突"));
    expect(currentHook().sessions[0].draftJson).toBe("本地草稿");
    await waitFor(() => expect(currentHook().syncError).toContain("版本冲突"));
    expect(api.updateChatSession.mock.calls[0][1].expected_updated_at).toBe(100);
  });

  it("定时同步失败显式捕获并保留 syncError，不刷新掉本地内容", async () => {
    api.updateChatSession.mockRejectedValue(new Error("后台版本冲突"));
    render(<Probe />);
    await waitForLoaded();

    act(() => {
      currentHook().updateSessionById("session-1", { draftJson: "未保存的本地内容" });
    });
    await waitFor(() => expect(currentHook().syncError).toContain("后台版本冲突"));

    api.fetchChatSession.mockResolvedValue(record({ updated_at: 300, draft_json: "服务端旧内容" }));
    await act(async () => {
      await currentHook().refreshSessionById("session-1");
    });

    expect(api.fetchChatSession).not.toHaveBeenCalled();
    expect(currentHook().sessions[0].draftJson).toBe("未保存的本地内容");
  });

  it("同一会话请求保持串行，并让后续编辑携带新版本且不覆盖本地输入", async () => {
    const firstSave = deferred<ChatSessionApiRecord>();
    let updateCount = 0;
    let firstPayload: ChatSessionUpsertPayload | undefined;
    api.updateChatSession.mockImplementation(async (_sessionId: string, payload: ChatSessionUpsertPayload) => {
      updateCount += 1;
      if (updateCount === 1) {
        firstPayload = payload;
        return firstSave.promise;
      }
      return recordFromPayload(payload, 300);
    });

    render(<Probe />);
    await waitForLoaded();

    act(() => {
      currentHook().updateSessionById("session-1", { draftJson: "第一版" });
    });
    await waitFor(() => expect(updateCount).toBe(1));

    act(() => {
      currentHook().updateSessionById("session-1", {
        title: "并发重命名",
        draftJson: "第二版",
      });
    });
    firstSave.resolve(recordFromPayload(firstPayload!, 200));

    await waitFor(() => expect(updateCount).toBe(2), { timeout: 2000 });
    const secondPayload = api.updateChatSession.mock.calls[1][1] as ChatSessionUpsertPayload;
    expect(secondPayload.expected_updated_at).toBe(200);
    expect(secondPayload.title).toBe("并发重命名");
    expect(secondPayload.draft_json).toBe("第二版");
    expect(currentHook().sessions[0].draftJson).toBe("第二版");
    expect(currentHook().sessions[0].title).toBe("并发重命名");
  });

  it("新建会话后台失败也有显式错误处理，不产生未处理拒绝", async () => {
    api.listChatSessions.mockResolvedValue([]);
    api.createChatSession.mockRejectedValue(new Error("新建同步失败"));
    render(<Probe />);
    await waitForLoaded();

    act(() => {
      currentHook().createNewSession();
    });

    await waitFor(() => expect(currentHook().syncError).toContain("新建同步失败"));
    expect(currentHook().sessions).toHaveLength(1);
  });
});
