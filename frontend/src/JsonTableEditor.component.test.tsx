// @vitest-environment jsdom

import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";

import { JsonTableEditor } from "./JsonTableEditor";

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});

describe("JsonTableEditor 真实组件交互", () => {
  it("保存等待期间连续确认只触发一次报告", async () => {
    let finish!: () => void;
    const saving = new Promise<void>(resolve => { finish = resolve; });
    const onConfirm = vi.fn();
    render(<JsonTableEditor resetKey="one" initialJson='{"事故类型":"追尾"}' onAutoSave={() => saving} onConfirm={onConfirm} />);
    fireEvent.change(screen.getByRole("textbox", { name: "事故类型" }), { target: { value: "侧碰" } });
    const confirm = screen.getByRole("button", { name: "确认事故信息并生成报告" });
    fireEvent.click(confirm);
    fireEvent.click(confirm);
    expect((confirm as HTMLButtonElement).disabled).toBe(true);
    expect(onConfirm).not.toHaveBeenCalled();
    await act(async () => { finish(); await saving; });
    await waitFor(() => expect(onConfirm).toHaveBeenCalledTimes(1));
  });

  it("等待保存时切换档案，不把旧确认应用到新档案", async () => {
    let finish!: () => void;
    const saving = new Promise<void>(resolve => { finish = resolve; });
    const onConfirm = vi.fn();
    const { rerender } = render(<JsonTableEditor resetKey="one" initialJson='{"事故类型":"追尾"}' onAutoSave={() => saving} onConfirm={onConfirm} />);
    fireEvent.change(screen.getByRole("textbox", { name: "事故类型" }), { target: { value: "侧碰" } });
    fireEvent.click(screen.getByRole("button", { name: "确认事故信息并生成报告" }));
    rerender(<JsonTableEditor resetKey="two" initialJson='{"事故类型":"新档案"}' onAutoSave={() => saving} onConfirm={onConfirm} />);
    await act(async () => { finish(); await saving; });
    expect(onConfirm).not.toHaveBeenCalled();
    expect((screen.getByRole("textbox", { name: "事故类型" }) as HTMLTextAreaElement).value).toBe("新档案");
  });
  it("保存失败保留修改，显式重试后确认才继续", async () => {
    const user = userEvent.setup();
    const onAutoSave = vi.fn().mockRejectedValueOnce(new Error("保存冲突")).mockResolvedValue(undefined);
    const onConfirm = vi.fn();
    render(<JsonTableEditor initialJson='{"事故类型":"追尾"}' onAutoSave={onAutoSave} onConfirm={onConfirm} />);
    await user.clear(screen.getByRole("textbox", { name: "事故类型" }));
    await user.type(screen.getByRole("textbox", { name: "事故类型" }), "侧碰");
    await user.tab();
    await screen.findByText("保存失败，修改已保留");
    expect((screen.getByRole("textbox", { name: "事故类型" }) as HTMLTextAreaElement).value).toBe("侧碰");
    await user.click(screen.getByRole("button", { name: "保存修改" }));
    await screen.findByText("已保存");
    await user.click(screen.getByRole("button", { name: "确认事故信息并生成报告" }));
    expect(onConfirm).toHaveBeenCalledWith(JSON.stringify({ 事故类型: "侧碰" }, null, 2));
  });

  it("确认时保存仍失败则不启动报告", async () => {
    const user = userEvent.setup();
    const onConfirm = vi.fn();
    render(<JsonTableEditor initialJson='{"事故类型":"追尾"}' onAutoSave={vi.fn().mockRejectedValue(new Error("保存冲突"))} onConfirm={onConfirm} />);
    await user.type(screen.getByRole("textbox", { name: "事故类型" }), "待核实");
    await user.click(screen.getByRole("button", { name: "确认事故信息并生成报告" }));
    await screen.findByRole("alert");
    expect(onConfirm).not.toHaveBeenCalled();
  });
  it("非法 JSON 显示可访问错误，而不是渲染表格", async () => {
    render(
      <JsonTableEditor
        initialJson='{"事故类型":'
        onConfirm={vi.fn()}
      />,
    );

    const error = await screen.findByRole("alert");
    expect(error.textContent).toContain("输入草稿格式异常，无法解析为表格。");
    expect(screen.queryByRole("table")).toBeNull();
  });

  it("真实渲染 37 个字段，并保留每个字段的初始值", () => {
    const fields = Object.fromEntries(
      Array.from({ length: 37 }, (_, index) => [`字段${index + 1}`, `值${index + 1}`]),
    );
    render(
      <JsonTableEditor
        initialJson={JSON.stringify(fields)}
        onConfirm={vi.fn()}
      />,
    );

    expect(screen.getAllByRole("row")).toHaveLength(38);
    expect(screen.getAllByRole("textbox")).toHaveLength(37);
    expect(screen.getByDisplayValue("值1")).not.toBeNull();
    expect(screen.getByDisplayValue("值37")).not.toBeNull();
  });

  it("disabled 会锁定字段输入和确认操作", () => {
    render(
      <JsonTableEditor
        initialJson='{"事故类型":"追尾","速度":20}'
        onConfirm={vi.fn()}
        disabled
      />,
    );

    expect(screen.getAllByRole("textbox").every((input) => (input as HTMLInputElement).disabled)).toBe(true);
    expect((screen.getByRole("button", { name: "确认事故信息并生成报告" }) as HTMLButtonElement).disabled).toBe(true);
    const stop = screen.getByText("停止", { selector: "button" });
    expect((stop as HTMLButtonElement).disabled).toBe(true);
    expect(stop.getAttribute("aria-hidden")).toBe("true");
    expect(stop.getAttribute("tabindex")).toBe("-1");
  });

  it("字段失焦时自动保存失败会显示错误", async () => {
    const user = userEvent.setup();
    const onAutoSave = vi.fn().mockRejectedValue(new Error("草稿接口失败"));
    render(
      <JsonTableEditor
        initialJson='{"事故类型":"追尾"}'
        onConfirm={vi.fn()}
        onAutoSave={onAutoSave}
      />,
    );

    const input = screen.getByDisplayValue("追尾");
    await user.clear(input);
    await user.type(input, "侧碰");
    await user.tab();
    await waitFor(() => expect(onAutoSave).toHaveBeenCalledTimes(1));
    expect(screen.getByRole("alert").textContent).toContain("自动保存失败：草稿接口失败");
  });

  it("生成中停止按钮保持键盘可达，并可由 Enter 触发取消", async () => {
    const user = userEvent.setup();
    const onCancelGenerate = vi.fn();
    render(
      <JsonTableEditor
        initialJson='{"事故类型":"追尾"}'
        onConfirm={vi.fn()}
        isGeneratingReport
        onCancelGenerate={onCancelGenerate}
      />,
    );

    const stop = screen.getByRole("button", { name: "停止" });
    expect((stop as HTMLButtonElement).disabled).toBe(false);
    expect(stop.getAttribute("aria-hidden")).toBe("false");
    expect(stop.getAttribute("tabindex")).toBe("0");
    screen.getByDisplayValue("追尾").focus();
    await user.tab();
    expect(document.activeElement).toBe(stop);
    await user.keyboard("{Enter}");
    expect(onCancelGenerate).toHaveBeenCalledTimes(1);
  });
});
