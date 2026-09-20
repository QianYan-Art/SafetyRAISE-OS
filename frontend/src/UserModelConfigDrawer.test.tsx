// @vitest-environment jsdom

import { cleanup, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";

import { UserModelConfigDrawer } from "./UserModelConfigDrawer";
import type { CapabilityConfigState } from "./types";

function state(overrides: Partial<CapabilityConfigState> = {}): CapabilityConfigState {
  return {
    role: "user",
    capabilities: [
      {
        capability: "vision",
        configured: false,
        base_url: null,
        model_name: null,
        api_key_masked: "••••old",
        params: {},
      },
      {
        capability: "report",
        configured: true,
        base_url: "https://report.example/v1",
        model_name: "report-model",
        api_key_masked: "••••report",
        params: {},
      },
      {
        capability: "embedding",
        configured: true,
        base_url: "https://embed.example/v1",
        model_name: "embed-model",
        api_key_masked: "••••embed",
        params: { top_k: 8, dense_top_k_chunks: 20, dense_top_k_rules: 10 },
      },
    ],
    system_defaults: { embedding: { top_k: 6, dense_top_k_chunks: 12, dense_top_k_rules: 8 } },
    ...overrides,
  };
}

function renderDrawer(overrides: Partial<React.ComponentProps<typeof UserModelConfigDrawer>> = {}) {
  return render(
    <UserModelConfigDrawer
      open
      saving={false}
      state={state()}
      errorMessage=""
      username="demo_user"
      returnLabel="返回工作区"
      onClose={vi.fn()}
      onSave={vi.fn().mockResolvedValue(undefined)}
      {...overrides}
    />,
  );
}

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});

describe("UserModelConfigDrawer", () => {
  it("显示真实用户名，并支持管理中心返回文案", () => {
    renderDrawer({ username: "real_user", returnLabel: "返回管理中心" });

    expect(screen.getAllByText("real_user · 个人账户配置")).toHaveLength(2);
    expect(screen.getByRole("button", { name: "返回管理中心" })).not.toBeNull();
  });

  it("按后端字段提交配置，留空密钥时发送 null 以保留原值", async () => {
    const user = userEvent.setup();
    const onSave = vi.fn().mockResolvedValue(undefined);
    renderDrawer({ onSave });

    const baseUrlInput = screen.getByPlaceholderText("例如 https://api.openai.com/v1");
    await user.clear(baseUrlInput);
    await user.type(baseUrlInput, "https://vision.example/v1");
    await user.type(screen.getByLabelText("模型名称"), "vision-model");
    expect(screen.getByPlaceholderText("已配置 ••••old，留空保留")).not.toBeNull();

    await user.click(screen.getByRole("button", { name: "保存配置" }));

    await waitFor(() => expect(onSave).toHaveBeenCalledTimes(1));
    expect(onSave).toHaveBeenCalledWith({
      items: [
        expect.objectContaining({
          capability: "vision",
          base_url: "https://vision.example/v1",
          model_name: "vision-model",
          api_key: null,
        }),
      ],
    });
  });

  it("首次配置可单独保存视觉用途，不要求另一个标签页已配置", async () => {
    const user = userEvent.setup();
    const onSave = vi.fn().mockResolvedValue(undefined);
    renderDrawer({ onSave, state: { role: "user", capabilities: [], system_defaults: {} } });
    await user.type(screen.getByPlaceholderText("例如 https://api.openai.com/v1"), "https://vision.example/v1");
    await user.type(screen.getByLabelText("模型名称"), "vision");
    await user.click(screen.getByRole("button", { name: "保存配置" }));
    expect(onSave).toHaveBeenCalledWith({ items: [{ capability: "vision", base_url: "https://vision.example/v1", model_name: "vision", api_key: null }] });
  });

  it("切换用途和关闭页面时确认放弃未保存修改", async () => {
    const user = userEvent.setup();
    const onClose = vi.fn();
    const confirm = vi.spyOn(window, "confirm");
    renderDrawer({ onClose });

    await user.type(screen.getByLabelText("模型名称"), "draft-model");
    confirm.mockReturnValue(false);
    await user.click(screen.getByRole("tab", { name: "报告生成" }));
    expect(screen.getByRole("tab", { name: "视觉识别" }).getAttribute("aria-selected")).toBe("true");
    expect(screen.getByDisplayValue("draft-model")).not.toBeNull();

    confirm.mockReturnValue(true);
    await user.click(screen.getByRole("tab", { name: "报告生成" }));
    expect(screen.getByRole("tab", { name: "报告生成" }).getAttribute("aria-selected")).toBe("true");
    expect(screen.getByRole("heading", { name: "报告模型" })).not.toBeNull();

    await user.click(screen.getByRole("button", { name: "返回工作区" }));
    expect(onClose).toHaveBeenCalledTimes(1);
  });

  it("保存失败时保留当前草稿", async () => {
    const user = userEvent.setup();
    const onSave = vi.fn().mockRejectedValue(new Error("保存失败"));
    renderDrawer({ onSave });

    const baseUrlInput = screen.getByPlaceholderText("例如 https://api.openai.com/v1");
    await user.clear(baseUrlInput);
    await user.type(baseUrlInput, "https://vision.example/v1");
    await user.click(screen.getByRole("button", { name: "保存配置" }));

    await waitFor(() => expect(screen.getByRole("alert").textContent).toContain("当前修改仍保留"));
    expect(screen.getByDisplayValue("https://vision.example/v1")).not.toBeNull();
    expect(onSave).toHaveBeenCalledTimes(1);
  });
});
