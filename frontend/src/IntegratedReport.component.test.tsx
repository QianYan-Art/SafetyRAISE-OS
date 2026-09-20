// @vitest-environment jsdom

import { createRef, useRef, useState } from "react";
import { cleanup, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterAll, afterEach, beforeAll, beforeEach, describe, expect, it, vi } from "vitest";

import { IntegratedReport, type IntegratedReportHandle } from "./IntegratedReport";
import { JsonTableEditor } from "./JsonTableEditor";
import type {
  ReportAuthorizationPreview,
  ReportEvidenceRecord,
  ReportRunView,
} from "./types";

const api = vi.hoisted(() => ({
  authorizeReportRun: vi.fn(),
  cancelReportRun: vi.fn(),
  createReportRun: vi.fn(),
  downloadReportRunExport: vi.fn(),
  executeReportRunStream: vi.fn(),
  fetchAuthorizationPreview: vi.fn(),
  fetchReportEvidence: vi.fn(),
  fetchReportRun: vi.fn(),
  fetchReportRunCandidate: vi.fn(),
  formatApiErrorMessage: vi.fn(),
  listReportRuns: vi.fn(),
  resumeReportRunStream: vi.fn(),
  saveReportEvidence: vi.fn(),
}));

vi.mock("./api", async () => ({
  ...(await vi.importActual<typeof import("./api")>("./api")),
  ...api,
}));

function run(overrides: Partial<ReportRunView> = {}): ReportRunView {
  return {
    run_id: "run-1",
    session_id: "session-1",
    state: "published",
    state_version: 1,
    snapshot_digest: "snapshot-1",
    candidate_version: 0,
    review_status: "passed",
    terminal_reason: null,
    budget: { remaining: 100 },
    last_event_seq: 1,
    quality_gate: "quality_validated",
    formal_export_eligible: false,
    release_binding_status: "approved",
    execution_profile: "outbound",
    report: {
      report_markdown: "",
      sections: [],
      citations: [],
      meta: {},
    },
    ...overrides,
  };
}

function evidence(overrides: Partial<ReportEvidenceRecord> = {}): ReportEvidenceRecord {
  return {
    evidence_id: "evidence-1",
    text: "现场记录文本",
    source_label: "现场照片",
    source_locator: "第 2 页",
    kind: "observation",
    verification_status: "unverified",
    conflicts_with: [],
    verification_note: "",
    field_conflicts: [],
    ...overrides,
  };
}

function authorizationPreview(): ReportAuthorizationPreview {
  return {
    available: true,
    reason: null,
    snapshot_digest: "snapshot-1",
    endpoint_profile_digest: "endpoint-1",
    approved_knowledge_manifest_digest: "knowledge-1",
    snapshot: {
      canonicalization_version: 1,
      accident_data: { 事故类型: "追尾" },
      supplemental_records: [],
      revision: 1,
      knowledge_manifest_digest: "knowledge-1",
      fact_obligations: [],
      source_digests: {},
    },
    endpoints: [],
    knowledge_collections: [],
  };
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

function GenerateSurface({ onPersistDraft }: { onPersistDraft: (sessionId: string, json: string) => Promise<void> }) {
  const reportRef = useRef<IntegratedReportHandle>(null);
  const [busy, setBusy] = useState(false);
  const [active, setActive] = useState(false);
  const [cancelling, setCancelling] = useState(false);
  return (
    <>
      <JsonTableEditor
        initialJson='{"事故类型":"追尾"}'
        onConfirm={(json) => reportRef.current?.generate(json)}
        disabled={busy}
        isGeneratingReport={active}
        isCancellingReport={cancelling}
        onCancelGenerate={() => void reportRef.current?.cancel()}
      />
      <IntegratedReport
        ref={reportRef}
        sessionId="session-1"
        onPersistDraft={onPersistDraft}
        onBusyChange={setBusy}
        onActiveChange={setActive}
        onCancellingChange={setCancelling}
      />
    </>
  );
}

async function waitForInitialLoad() {
  await waitFor(() => expect(screen.queryByText("正在读取报告")).toBeNull());
}

const originalCreateObjectURL = URL.createObjectURL;
const originalRevokeObjectURL = URL.revokeObjectURL;

beforeAll(() => {
  Object.defineProperty(URL, "createObjectURL", {
    configurable: true,
    writable: true,
    value: vi.fn(() => "blob:test"),
  });
  Object.defineProperty(URL, "revokeObjectURL", {
    configurable: true,
    writable: true,
    value: vi.fn(),
  });
});

afterAll(() => {
  if (originalCreateObjectURL) {
    Object.defineProperty(URL, "createObjectURL", { configurable: true, writable: true, value: originalCreateObjectURL });
  } else {
    Reflect.deleteProperty(globalThis.URL, "createObjectURL");
  }
  if (originalRevokeObjectURL) {
    Object.defineProperty(URL, "revokeObjectURL", { configurable: true, writable: true, value: originalRevokeObjectURL });
  } else {
    Reflect.deleteProperty(globalThis.URL, "revokeObjectURL");
  }
});

beforeEach(() => {
  vi.clearAllMocks();
  vi.spyOn(HTMLAnchorElement.prototype, "click").mockImplementation(() => undefined);
  api.listReportRuns.mockResolvedValue({ runs: [], next_cursor: null });
  api.fetchReportEvidence.mockResolvedValue({ revision: 1, records: [] });
  api.fetchReportRun.mockImplementation(async (runId: string) => run({ run_id: runId }));
  api.fetchReportRunCandidate.mockResolvedValue({
    candidate_version: 1,
    snapshot_digest: "snapshot-1",
    candidate_report: { version: 1, report_markdown: "候选报告", claims: [] },
    review_result: { issues: [] },
    display_status: "candidate",
  });
  api.fetchAuthorizationPreview.mockResolvedValue(authorizationPreview());
  api.authorizeReportRun.mockImplementation(async (runId: string) => run({ run_id: runId, state: "generating" }));
  api.executeReportRunStream.mockResolvedValue(undefined);
  api.resumeReportRunStream.mockResolvedValue(undefined);
  api.cancelReportRun.mockResolvedValue(run({ state: "cancelled" }));
  api.downloadReportRunExport.mockResolvedValue({ blob: new Blob(["report"]), fileName: "report.md" });
  api.saveReportEvidence.mockImplementation(async (_sessionId: string, revision: number, records: ReportEvidenceRecord[]) => ({
    revision: revision + 1,
    records,
  }));
  api.formatApiErrorMessage.mockImplementation((error: unknown, fallback: string) => (
    error instanceof Error ? error.message : fallback
  ));
});

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});

describe("IntegratedReport 真实组件交互", () => {
  it.each([
    ["needs_review", "invalid_review_or_candidate"],
    ["failed", "retrieval_policy_exceeded"],
  ] as const)("只对服务端确认的%s协议故障展示继续按钮，复用恢复接口且不允许未知重试", async (state, reason) => {
    const user = userEvent.setup();
    const stopped = run({
      state, review_status: "failed", report: null,
      terminal_reason: reason, can_resume_protocol: true,
      budget: { unknown_requests: 0, remaining: 100 }, state_version: 21,
    });
    api.listReportRuns.mockResolvedValue({ runs: [stopped], next_cursor: null });
    api.fetchReportRun.mockResolvedValue(stopped);
    api.authorizeReportRun.mockResolvedValue(stopped);
    const stream = deferred<void>();
    api.resumeReportRunStream.mockReturnValue(stream.promise);
    render(<IntegratedReport sessionId="session-1" onPersistDraft={vi.fn()} onBusyChange={vi.fn()} />);
    await waitForInitialLoad();
    expect(screen.queryByText(/确认重试未收到结果的请求/)).toBeNull();
    await user.click(screen.getByRole("button", { name: "继续生成" }));
    await waitFor(() => expect(api.resumeReportRunStream).toHaveBeenCalledTimes(1));
    expect(api.resumeReportRunStream.mock.calls[0].slice(0, 3)).toEqual(["run-1", 21, false]);
    expect(api.createReportRun).not.toHaveBeenCalled();
    expect(api.executeReportRunStream).not.toHaveBeenCalled();
    const beforePoll = api.fetchReportRun.mock.calls.length;
    await waitFor(() => expect(api.fetchReportRun.mock.calls.length).toBeGreaterThan(beforePoll), {
      timeout: 3500,
    });
    stream.resolve();
    await waitFor(() => expect(screen.getByRole("button", { name: "继续生成" })).toBeDefined());
  });

  it.each([
    ["needs_review", "invalid_review_or_candidate"],
    ["failed", "retrieval_policy_exceeded"],
  ] as const)("未获服务端资格的%s终态不能恢复", async (state, reason) => {
    const stopped = run({
      state, review_status: "failed", report: null,
      terminal_reason: reason, can_resume_protocol: false,
    });
    api.listReportRuns.mockResolvedValue({ runs: [stopped], next_cursor: null });
    api.fetchReportRun.mockResolvedValue(stopped);
    render(<IntegratedReport sessionId="session-1" onPersistDraft={vi.fn()} onBusyChange={vi.fn()} />);
    await waitForInitialLoad();
    expect(screen.queryByRole("button", { name: "继续生成" })).toBeNull();
    expect(api.resumeReportRunStream).not.toHaveBeenCalled();
  });

  it("补证支持新增、编辑、删除，并阻止空必填提交", async () => {
    const user = userEvent.setup();
    const initial = evidence();
    api.fetchReportEvidence.mockResolvedValue({ revision: 3, records: [initial] });
    render(
      <IntegratedReport
        sessionId="session-1"
        onPersistDraft={vi.fn().mockResolvedValue(undefined)}
        onBusyChange={vi.fn()}
      />,
    );

    await waitForInitialLoad();
    await user.click(screen.getByText("补充材料（1）", { selector: "summary" }));
    const saveButton = screen.getByRole("button", { name: "保存补充材料" });
    expect((saveButton as HTMLButtonElement).disabled).toBe(true);

    const source = screen.getByLabelText("材料来源");
    const locator = screen.getByLabelText("页码或位置");
    const text = screen.getByLabelText("补充内容");
    await user.type(source, "  交警笔录  ");
    await user.type(locator, " 第 8 页 ");
    await user.type(text, " 车辆在路口前方停车。 ");
    await user.click(screen.getByLabelText("已人工核实"));
    expect((saveButton as HTMLButtonElement).disabled).toBe(false);

    await user.click(saveButton);
    await waitFor(() => expect(api.saveReportEvidence).toHaveBeenCalledTimes(1));
    expect(api.saveReportEvidence.mock.calls[0][2]).toEqual([
      initial,
      expect.objectContaining({
        source_label: "交警笔录",
        source_locator: "第 8 页",
        text: "车辆在路口前方停车。",
        verification_status: "human_confirmed",
      }),
    ]);

    const initialArticle = screen.getByText("现场照片").closest("article");
    expect(initialArticle).not.toBeNull();
    await user.click(within(initialArticle as HTMLElement).getByRole("button", { name: "编辑" }));
    expect((source as HTMLInputElement).value).toBe("现场照片");
    await user.clear(text);
    await user.type(text, "修改后的现场记录");
    await user.click(saveButton);
    await waitFor(() => expect(api.saveReportEvidence).toHaveBeenCalledTimes(2));
    expect(api.saveReportEvidence.mock.calls[1][2]).toEqual([
      expect.objectContaining({ evidence_id: "evidence-1", text: "修改后的现场记录" }),
      expect.objectContaining({ source_label: "交警笔录" }),
    ]);

    const editedArticle = screen.getByText("现场照片").closest("article");
    expect(editedArticle).not.toBeNull();
    await user.click(within(editedArticle as HTMLElement).getByRole("button", { name: "删除" }));
    await waitFor(() => expect(api.saveReportEvidence).toHaveBeenCalledTimes(3));
    expect(api.saveReportEvidence.mock.calls[2][2]).toEqual([
      expect.objectContaining({ source_label: "交警笔录" }),
    ]);
    expect(screen.queryByText("现场照片")).toBeNull();
  });

  it("活动运行期间禁用补证编辑控件，但保留停止生成操作", async () => {
    const user = userEvent.setup();
    const activeRun = run({ run_id: "active-run", state: "generating", formal_export_eligible: false });
    api.listReportRuns.mockResolvedValue({ runs: [activeRun], next_cursor: null });
    api.fetchReportRun.mockResolvedValue(activeRun);
    api.fetchReportEvidence.mockResolvedValue({ revision: 1, records: [evidence()] });
    render(
      <IntegratedReport
        sessionId="session-1"
        onPersistDraft={vi.fn().mockResolvedValue(undefined)}
        onBusyChange={vi.fn()}
      />,
    );

    await waitFor(() => expect(screen.getByRole("status").textContent).toContain("撰写报告"));
    await user.click(screen.getByText("补充材料（1）", { selector: "summary" }));
    expect((screen.getByLabelText("材料来源") as HTMLInputElement).disabled).toBe(true);
    expect((screen.getByLabelText("页码或位置") as HTMLInputElement).disabled).toBe(true);
    expect((screen.getByLabelText("补充内容") as HTMLTextAreaElement).disabled).toBe(true);
    expect((screen.getByLabelText("已人工核实") as HTMLInputElement).disabled).toBe(true);
    expect((screen.getByRole("button", { name: "编辑" }) as HTMLButtonElement).disabled).toBe(true);
    expect((screen.getByRole("button", { name: "删除" }) as HTMLButtonElement).disabled).toBe(true);
    expect((screen.getByRole("button", { name: "保存补充材料" }) as HTMLButtonElement).disabled).toBe(true);
    expect((screen.getByRole("button", { name: "停止生成" }) as HTMLButtonElement).disabled).toBe(false);
  });

  it("历史报告的迟到读取响应不会覆盖当前选中的 run", async () => {
    const user = userEvent.setup();
    const oldRun = run({ run_id: "run-old", report: null });
    const newRun = run({ run_id: "run-new", report: { ...run().report!, report_markdown: "新报告内容" } });
    const oldResponse = deferred<ReportRunView>();
    api.listReportRuns.mockResolvedValue({ runs: [oldRun, newRun], next_cursor: null });
    api.fetchReportRun.mockImplementation((runId: string) => (
      runId === "run-old" ? oldResponse.promise : Promise.resolve(newRun)
    ));

    render(
      <IntegratedReport
        sessionId="session-1"
        onPersistDraft={vi.fn().mockResolvedValue(undefined)}
        onBusyChange={vi.fn()}
      />,
    );

    const history = await screen.findByRole("combobox", { name: "历史报告" });
    await user.selectOptions(history, "run-new");
    await waitFor(() => expect(screen.getByText("新报告内容")).not.toBeNull());

    oldResponse.resolve({
      ...oldRun,
      report: { ...run().report!, report_markdown: "旧报告内容" },
      state_version: 9,
    });
    await waitFor(() => expect(screen.getByText("新报告内容")).not.toBeNull());
    expect(screen.queryByText("旧报告内容")).toBeNull();
  });

  it("未知请求确认在切换 run 时清空，并且一次继续操作只消费一次", async () => {
    const user = userEvent.setup();
    const first = run({ run_id: "run-first", state: "suspended", budget: { unknown_requests: 2 } });
    const second = run({ run_id: "run-second", state: "suspended", budget: { unknown_requests: 1 } });
    api.listReportRuns.mockResolvedValue({ runs: [first, second], next_cursor: null });
    api.fetchReportRun.mockImplementation(async (runId: string) => (runId === first.run_id ? first : second));
    api.authorizeReportRun.mockImplementation(async (runId: string) => ({
      ...(runId === second.run_id ? second : first),
      state_version: 2,
    }));

    render(
      <IntegratedReport
        sessionId="session-1"
        onPersistDraft={vi.fn().mockResolvedValue(undefined)}
        onBusyChange={vi.fn()}
      />,
    );

    const history = await screen.findByRole("combobox", { name: "历史报告" });
    const retry = screen.getByLabelText("确认重试未收到结果的请求，可能重复计费");
    await user.click(retry);
    expect((retry as HTMLInputElement).checked).toBe(true);

    await user.selectOptions(history, "run-second");
    const secondRetry = screen.getByLabelText("确认重试未收到结果的请求，可能重复计费");
    expect((secondRetry as HTMLInputElement).checked).toBe(false);
    expect((screen.getByRole("button", { name: "继续生成" }) as HTMLButtonElement).disabled).toBe(true);

    await user.click(secondRetry);
    await user.click(screen.getByRole("button", { name: "继续生成" }));
    await waitFor(() => expect(api.resumeReportRunStream).toHaveBeenCalledTimes(1));
    expect(api.resumeReportRunStream.mock.calls[0][0]).toBe("run-second");
    expect(api.resumeReportRunStream.mock.calls[0][2]).toBe(true);
    expect((screen.getByLabelText("确认重试未收到结果的请求，可能重复计费") as HTMLInputElement).checked).toBe(false);
    expect((screen.getByRole("button", { name: "继续生成" }) as HTMLButtonElement).disabled).toBe(true);

    await user.click(screen.getByRole("button", { name: "继续生成" }));
    expect(api.resumeReportRunStream).toHaveBeenCalledTimes(1);
  });

  it("只有正式导出资格会展示下载按钮，导出期间所有格式按钮禁用", async () => {
    const user = userEvent.setup();
    const exportRun = run({
      state: "published",
      formal_export_eligible: true,
      report: { ...run().report!, report_markdown: "正式报告" },
    });
    api.listReportRuns.mockResolvedValue({ runs: [exportRun], next_cursor: null });
    api.fetchReportRun.mockResolvedValue(exportRun);
    const exportResponse = deferred<{ blob: Blob; fileName: string }>();
    api.downloadReportRunExport.mockReturnValue(exportResponse.promise);

    render(
      <IntegratedReport
        sessionId="session-1"
        onPersistDraft={vi.fn().mockResolvedValue(undefined)}
        onBusyChange={vi.fn()}
      />,
    );

    await waitFor(() => expect(screen.getByRole("button", { name: "下载Word" })).not.toBeNull());
    const word = screen.getByRole("button", { name: "下载Word" });
    const pdf = screen.getByRole("button", { name: "下载PDF" });
    const markdown = screen.getByRole("button", { name: "下载MD" });
    await user.click(word);
    await waitFor(() => expect((word as HTMLButtonElement).disabled).toBe(true));
    expect((pdf as HTMLButtonElement).disabled).toBe(true);
    expect((markdown as HTMLButtonElement).disabled).toBe(true);
    expect(api.downloadReportRunExport).toHaveBeenCalledTimes(1);

    exportResponse.resolve({ blob: new Blob(["formal"]), fileName: "formal.docx" });
    await waitFor(() => expect((word as HTMLButtonElement).disabled).toBe(false));
  });

  it("没有正式导出资格时不渲染正式下载按钮", async () => {
    const ineligibleRun = run({ state: "published", formal_export_eligible: false });
    api.listReportRuns.mockResolvedValue({ runs: [ineligibleRun], next_cursor: null });
    api.fetchReportRun.mockResolvedValue(ineligibleRun);
    render(
      <IntegratedReport
        sessionId="session-1"
        onPersistDraft={vi.fn().mockResolvedValue(undefined)}
        onBusyChange={vi.fn()}
      />,
    );

    await waitForInitialLoad();
    expect(screen.queryByRole("button", { name: "下载Word" })).toBeNull();
    expect(screen.queryByRole("button", { name: "下载PDF" })).toBeNull();
  });

  it("下载失败后重试成功清除旧错误", async () => {
    const user = userEvent.setup();
    const exportRun = run({ formal_export_eligible: true });
    api.listReportRuns.mockResolvedValue({ runs: [exportRun], next_cursor: null });
    api.fetchReportRun.mockResolvedValue(exportRun);
    api.downloadReportRunExport.mockRejectedValueOnce(new Error("下载暂时失败"));
    render(<IntegratedReport sessionId="session-1" onPersistDraft={vi.fn()} onBusyChange={vi.fn()} />);
    const button = await screen.findByRole("button", { name: "下载Word" });
    await user.click(button);
    await waitFor(() => expect(screen.getByRole("alert").textContent).toContain("下载暂时失败"));
    await user.click(button);
    await waitFor(() => expect(api.downloadReportRunExport).toHaveBeenCalledTimes(2));
    await waitFor(() => expect((button as HTMLButtonElement).disabled).toBe(false));
    expect(screen.queryByRole("alert")).toBeNull();
  });

  it("预算或时长耗尽时显示用户可读的未发布说明", async () => {
    const exhaustedRun = run({ state: "budget_exhausted", terminal_reason: "budget_exhausted" });
    api.listReportRuns.mockResolvedValue({ runs: [exhaustedRun], next_cursor: null });
    api.fetchReportRun.mockResolvedValue(exhaustedRun);
    render(
      <IntegratedReport
        sessionId="session-1"
        onPersistDraft={vi.fn().mockResolvedValue(undefined)}
        onBusyChange={vi.fn()}
      />,
    );

    await waitFor(() => expect(screen.getByText("本次运行达到预算或时长上限，报告未发布。")).not.toBeNull());
  });

  it("停止接口失败时显示错误，重试成功后清除旧错误", async () => {
    const user = userEvent.setup();
    const activeRun = run({ run_id: "active-run", state: "generating" });
    api.listReportRuns.mockResolvedValue({ runs: [activeRun], next_cursor: null });
    api.fetchReportRun.mockResolvedValue(activeRun);
    api.cancelReportRun.mockRejectedValue(new Error("停止接口不可用"));
    render(
      <IntegratedReport
        sessionId="session-1"
        onPersistDraft={vi.fn().mockResolvedValue(undefined)}
        onBusyChange={vi.fn()}
      />,
    );

    await waitFor(() => expect(screen.getByRole("button", { name: "停止生成" })).not.toBeNull());
    await user.click(screen.getByRole("button", { name: "停止生成" }));
    await waitFor(() => expect(screen.getByRole("alert").textContent).toContain("停止接口不可用"));
    const cancelledRun = run({ run_id: "active-run", state: "cancelled", state_version: 2 });
    api.cancelReportRun.mockResolvedValue(cancelledRun);
    api.fetchReportRun.mockResolvedValue(cancelledRun);
    await user.click(screen.getByRole("button", { name: "停止生成" }));
    await waitFor(() => expect(screen.getByRole("status").textContent).toBe("已停止"));
    expect(screen.queryByRole("alert")).toBeNull();
  });

  it("报告记录读取失败不阻止离开档案", async () => {
    const ref = createRef<IntegratedReportHandle>();
    api.listReportRuns.mockRejectedValueOnce(new Error("暂时无法读取记录"));
    render(<IntegratedReport ref={ref} sessionId="session-1" onPersistDraft={vi.fn()} onBusyChange={vi.fn()} />);
    await screen.findByRole("alert");
    expect(ref.current?.isTransitionBlocked()).toBe(false);
  });

  it.each(["listReportRuns", "fetchReportEvidence"] as const)("初载%s失败不显示空报告，可显式重新读取", async (method) => {
    const user = userEvent.setup();
    api[method].mockRejectedValueOnce(new Error("暂时无法读取记录"));
    render(<GenerateSurface onPersistDraft={vi.fn().mockResolvedValue(undefined)} />);
    await waitFor(() => expect(screen.getByRole("alert").textContent).toContain("暂时无法读取记录"));
    expect(screen.queryByText("尚未生成报告")).toBeNull();
    expect((screen.getByRole("button", { name: "确认事故信息并生成报告" }) as HTMLButtonElement).disabled).toBe(true);
    await user.click(screen.getByRole("button", { name: "重新读取报告记录" }));
    await screen.findByText("尚未生成报告");
    expect(screen.queryByRole("alert")).toBeNull();
    expect((screen.getByRole("button", { name: "确认事故信息并生成报告" }) as HTMLButtonElement).disabled).toBe(false);
  });

  it("两处停止按钮同步等待状态，连续点击只发出一次取消请求", async () => {
    const user = userEvent.setup();
    const activeRun = run({ run_id: "active-run", state: "generating" });
    const cancelledRun = run({ run_id: "active-run", state: "cancelled" });
    const cancelResponse = deferred<ReportRunView>();
    api.listReportRuns.mockResolvedValue({ runs: [activeRun], next_cursor: null });
    api.fetchReportRun.mockResolvedValue(activeRun);
    api.cancelReportRun.mockReturnValue(cancelResponse.promise);
    render(
      <GenerateSurface onPersistDraft={vi.fn().mockResolvedValue(undefined)} />,
    );

    const stop = await screen.findByRole("button", { name: "停止生成" });
    const topStop = screen.getByRole("button", { name: "停止" });
    await user.click(topStop);
    await waitFor(() => expect((stop as HTMLButtonElement).disabled).toBe(true));
    expect((topStop as HTMLButtonElement).disabled).toBe(true);
    expect(topStop.textContent).toBe("正在停止");
    expect(stop.textContent).toBe("正在停止");
    expect(stop.getAttribute("aria-busy")).toBe("true");
    await user.click(stop);
    await user.click(topStop);
    expect(api.cancelReportRun).toHaveBeenCalledTimes(1);

    api.fetchReportRun.mockResolvedValue(cancelledRun);
    cancelResponse.resolve(cancelledRun);
    await waitFor(() => expect(api.cancelReportRun).toHaveBeenCalledTimes(1));
    await waitFor(() => expect(screen.queryByRole("button", { name: "正在停止" })).toBeNull());
  });

  it("读取会话期间禁用确认但不冒充生成中或显示停止按钮", async () => {
    const response = deferred<{ runs: ReportRunView[]; next_cursor: null }>();
    api.listReportRuns.mockReturnValue(response.promise);
    render(<GenerateSurface onPersistDraft={vi.fn().mockResolvedValue(undefined)} />);
    expect((screen.getByRole("button", { name: "确认事故信息并生成报告" }) as HTMLButtonElement).disabled).toBe(true);
    expect(screen.queryByRole("button", { name: "停止" })).toBeNull();
    expect(screen.queryByText("正在生成报告")).toBeNull();
    response.resolve({ runs: [], next_cursor: null });
    await waitForInitialLoad();
  });

  it("空报告使用单独的状态提示，已有 run 时不与运行状态并存", async () => {
    render(
      <IntegratedReport
        sessionId="session-1"
        onPersistDraft={vi.fn().mockResolvedValue(undefined)}
        onBusyChange={vi.fn()}
      />,
    );
    await waitForInitialLoad();
    expect(screen.getByRole("status").textContent).toContain("尚未生成报告");

    cleanup();
    const publishedRun = run({ report: null });
    api.listReportRuns.mockResolvedValue({ runs: [publishedRun], next_cursor: null });
    api.fetchReportRun.mockResolvedValue(publishedRun);
    render(
      <IntegratedReport
        sessionId="session-1"
        onPersistDraft={vi.fn().mockResolvedValue(undefined)}
        onBusyChange={vi.fn()}
      />,
    );
    await waitFor(() => expect(screen.getByRole("status").textContent).toContain("报告已完成"));
    expect(screen.queryByText("尚未生成报告")).toBeNull();
    expect(screen.getAllByRole("status")).toHaveLength(1);
  });

  it("草稿保存失败时不创建运行，确认按钮连续触发也只进入一次生成动作", async () => {
    const user = userEvent.setup();
    const failedPersist = vi.fn().mockRejectedValue(new Error("草稿保存失败"));
    api.listReportRuns.mockResolvedValue({ runs: [], next_cursor: null });
    render(<GenerateSurface onPersistDraft={failedPersist} />);
    await waitForInitialLoad();

    const confirm = screen.getByRole("button", { name: "确认事故信息并生成报告" });
    await user.click(confirm);
    await waitFor(() => expect(screen.getByRole("alert").textContent).toContain("草稿保存失败"));
    expect(api.createReportRun).not.toHaveBeenCalled();

    const persistDeferred = deferred<void>();
    const persist = vi.fn(() => persistDeferred.promise);
    cleanup();
    vi.clearAllMocks();
    api.listReportRuns.mockResolvedValue({ runs: [], next_cursor: null });
    api.fetchReportEvidence.mockResolvedValue({ revision: 1, records: [] });
    render(<GenerateSurface onPersistDraft={persist} />);
    await waitForInitialLoad();
    const secondConfirm = screen.getByRole("button", { name: "确认事故信息并生成报告" });
    await user.dblClick(secondConfirm);
    expect(persist).toHaveBeenCalledTimes(1);
    expect(api.createReportRun).not.toHaveBeenCalled();
    persistDeferred.resolve();
  });
});
