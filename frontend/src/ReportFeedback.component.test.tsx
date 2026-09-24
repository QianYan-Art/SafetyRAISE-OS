// @vitest-environment jsdom

import { cleanup, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { ApiError } from "./api";
import { AdminFeedback } from "./AdminFeedback";
import { ReportFeedback } from "./ReportFeedback";
import type { AdminReportFeedbackItem } from "./types";

const api = vi.hoisted(() => ({
  downloadAdminReportFeedback: vi.fn(),
  fetchReportRunFeedback: vi.fn(),
  formatApiErrorMessage: vi.fn((_err: unknown, fallback: string) => fallback),
  listAdminReportFeedback: vi.fn(),
  saveReportRunFeedback: vi.fn(),
}));

vi.mock("./api", async () => ({
  ...(await vi.importActual<typeof import("./api")>("./api")),
  ...api,
}));

const saved = {
  run_id: "run-1", revision: 2, reviewer_name: "王老师", verdict: "needs_revision" as const,
  issue_tags: ["liability" as const], comment: "责任划分依据不足。", updated_at: "2026-09-24T02:30:00Z",
};

beforeEach(() => {
  vi.clearAllMocks();
  window.localStorage.clear();
});

afterEach(() => cleanup());

describe("ReportFeedback", () => {
  it("载入已保存的反馈，未修改时不可重复保存", async () => {
    api.fetchReportRunFeedback.mockResolvedValue(saved);
    render(<ReportFeedback runId="run-1" />);
    await screen.findByText(/已保存第 2 版/);
    expect((screen.getByRole("radio", { name: "修改后可用" }) as HTMLInputElement).checked).toBe(true);
    expect((screen.getByRole("checkbox", { name: "责任认定不当" }) as HTMLInputElement).checked).toBe(true);
    expect((screen.getByRole("button", { name: "保存反馈" }) as HTMLButtonElement).disabled).toBe(true);
  });

  it("选择结论并填写反馈人后按当前版本保存，并记住反馈人", async () => {
    const user = userEvent.setup();
    api.fetchReportRunFeedback.mockResolvedValue({ run_id: "run-1", revision: 0 });
    api.saveReportRunFeedback.mockResolvedValue({ ...saved, revision: 1, verdict: "usable", issue_tags: [] });
    render(<ReportFeedback runId="run-1" />);
    await screen.findByText("尚未填写");
    const save = screen.getByRole("button", { name: "保存反馈" }) as HTMLButtonElement;
    expect(save.disabled).toBe(true);

    await user.click(screen.getByRole("radio", { name: "可直接使用" }));
    expect(save.disabled).toBe(true);
    await user.type(screen.getByRole("textbox", { name: "反馈人" }), "王老师");
    await user.type(screen.getByRole("textbox", { name: "具体意见" }), "结论清楚。");
    await user.click(save);

    await waitFor(() => expect(api.saveReportRunFeedback).toHaveBeenCalledWith("run-1", 0, {
      reviewer_name: "王老师", verdict: "usable", issue_tags: [], comment: "结论清楚。",
    }));
    await screen.findByText(/已保存第 1 版/);
    expect(window.localStorage.getItem("safetyraise.feedback.reviewer")).toBe("王老师");
  });

  it("版本冲突时载入最新内容并提示确认", async () => {
    const user = userEvent.setup();
    api.fetchReportRunFeedback
      .mockResolvedValueOnce({ run_id: "run-1", revision: 0 })
      .mockResolvedValueOnce(saved);
    api.saveReportRunFeedback.mockRejectedValue(
      new ApiError(409, "feedback_revision_conflict", { code: "feedback_revision_conflict" }),
    );
    window.localStorage.setItem("safetyraise.feedback.reviewer", "李老师");
    render(<ReportFeedback runId="run-1" />);
    await screen.findByText("尚未填写");
    expect((screen.getByRole("textbox", { name: "反馈人" }) as HTMLInputElement).value).toBe("李老师");
    await user.click(screen.getByRole("radio", { name: "不可用" }));
    await user.click(screen.getByRole("button", { name: "保存反馈" }));

    await screen.findByText(/已在其他页面更新/);
    expect((screen.getByRole("radio", { name: "修改后可用" }) as HTMLInputElement).checked).toBe(true);
  });
});

describe("AdminFeedback", () => {
  const item: AdminReportFeedbackItem = {
    ...saved, author_username: "safetyraise",
    run_context: { session_title: "国道240线追尾事故", run_created_at: "2026-09-24T01:00:00Z", state: "published" },
  };

  it("展示最新反馈，筛选条件同时用于列表和导出", async () => {
    const user = userEvent.setup();
    api.listAdminReportFeedback.mockResolvedValue({ total: 1, items: [item] });
    api.downloadAdminReportFeedback.mockResolvedValue({ blob: new Blob(["a"]), fileName: "report-feedback.csv" });
    vi.spyOn(HTMLAnchorElement.prototype, "click").mockImplementation(() => undefined);
    render(<AdminFeedback />);

    await screen.findByText("国道240线追尾事故");
    const table = within(screen.getByRole("region", { name: "质量反馈列表" }));
    expect(table.getByText("修改后可用")).not.toBeNull();
    expect(table.getByText("责任认定不当")).not.toBeNull();
    expect(table.getByText(/第 2 版/)).not.toBeNull();

    await user.selectOptions(screen.getByRole("combobox", { name: "筛选总体结论" }), "needs_revision");
    await waitFor(() => expect(api.listAdminReportFeedback).toHaveBeenLastCalledWith({
      verdict: "needs_revision", tag: undefined, limit: 20, offset: 0,
    }));
    await user.click(screen.getByRole("button", { name: "导出表格" }));
    await waitFor(() => expect(api.downloadAdminReportFeedback).toHaveBeenCalledWith({
      verdict: "needs_revision", tag: undefined,
    }));
  });

  it("首次读取完成前不显示反馈计数", async () => {
    let resolveList: (value: { total: number; items: AdminReportFeedbackItem[] }) => void = () => undefined;
    api.listAdminReportFeedback.mockReturnValue(new Promise((resolve) => { resolveList = resolve; }));
    render(<AdminFeedback />);

    expect(screen.getByText("正在读取质量反馈...")).not.toBeNull();
    expect(screen.queryByText(/份报告有反馈/)).toBeNull();
    resolveList({ total: 0, items: [] });
    expect(await screen.findByText("0 份报告有反馈")).not.toBeNull();
    expect(screen.getByText("还没有质量反馈")).not.toBeNull();
  });
});
