// @vitest-environment jsdom

import { cleanup, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { AdminConsole } from "./AdminConsole";
import type { AdminSpaceRecord, AdminUserRecord, UserSummary } from "./types";

const api = vi.hoisted(() => ({
  cleanupAdminOrphanSpaces: vi.fn(),
  createAdminUser: vi.fn(),
  deleteAdminSpace: vi.fn(),
  deleteAdminUser: vi.fn(),
  formatApiErrorMessage: vi.fn(),
  listAdminSpaces: vi.fn(),
  listAdminUsers: vi.fn(),
  updateAdminSpace: vi.fn(),
  updateAdminUser: vi.fn(),
}));

vi.mock("./api", async () => ({
  ...(await vi.importActual<typeof import("./api")>("./api")),
  ...api,
}));

const currentUser: UserSummary = {
  id: "admin-1",
  username: "demo_admin",
  display_name: "管理员",
  role: "admin",
  is_active: true,
  created_at: "2026-09-20T08:00:00Z",
  updated_at: "2026-09-20T08:00:00Z",
};

const users: AdminUserRecord[] = [
  {
    id: "admin-1",
    username: "demo_admin",
    display_name: "管理员",
    role: "admin",
    is_active: true,
    created_at: "2026-09-20T08:00:00Z",
    updated_at: "2026-09-20T08:00:00Z",
  },
  {
    id: "user-1",
    username: "analyst_demo",
    display_name: "分析员",
    role: "user",
    is_active: true,
    created_at: "2026-09-19T08:00:00Z",
    updated_at: "2026-09-19T08:00:00Z",
  },
];

const spaces: AdminSpaceRecord[] = [
  {
    session_id: "session-1",
    owner_user_id: "user-1",
    owner_username: "analyst_demo",
    title: "追尾事故档案",
    created_at: 1726819200000,
    updated_at: 1726819200000,
    session_state: "active",
    source_type: "image",
    source_name: null,
    message_count: 4,
    linked_artifact_count: 1,
    redacted: true,
  },
];

beforeEach(() => {
  vi.clearAllMocks();
  api.listAdminUsers.mockResolvedValue(users);
  api.listAdminSpaces.mockResolvedValue(spaces);
  api.createAdminUser.mockResolvedValue(users[1]);
  api.updateAdminUser.mockResolvedValue(users[1]);
  api.updateAdminSpace.mockResolvedValue(spaces[0]);
  api.deleteAdminUser.mockResolvedValue(undefined);
  api.deleteAdminSpace.mockResolvedValue(undefined);
  api.cleanupAdminOrphanSpaces.mockResolvedValue({ status: "ok", deleted_count: 0 });
  api.formatApiErrorMessage.mockImplementation((_error: unknown, fallback: string) => fallback);
});

afterEach(() => cleanup());

describe("AdminConsole", () => {
  it("当前管理员不能在界面降权或停用自己", async () => {
    const user = userEvent.setup();
    render(<AdminConsole currentUser={currentUser} activeTab="users" />);
    await user.click(await screen.findByRole("button", { name: "编辑 demo_admin" }));
    expect((screen.getByLabelText("角色") as HTMLSelectElement).disabled).toBe(true);
    expect((screen.getByLabelText("允许登录") as HTMLInputElement).disabled).toBe(true);
    await user.click(screen.getByRole("button", { name: "保存" }));
    await waitFor(() => expect(api.updateAdminUser).toHaveBeenCalledWith("admin-1", expect.objectContaining({
      role: "admin", is_active: true,
    })));
  });

  it("保存期间不能关闭用户抽屉，失败后保留草稿", async () => {
    const user = userEvent.setup();
    let rejectSave!: (error: Error) => void;
    api.updateAdminUser.mockImplementation(() => new Promise((_resolve, reject) => { rejectSave = reject; }));
    render(<AdminConsole currentUser={currentUser} activeTab="users" />);
    await user.click(await screen.findByRole("button", { name: "编辑 analyst_demo" }));
    await user.clear(screen.getByLabelText("显示名称"));
    await user.type(screen.getByLabelText("显示名称"), "未保存草稿");
    await user.click(screen.getByRole("button", { name: "保存" }));
    const dialog = screen.getByRole("dialog", { name: "编辑用户" });
    expect((screen.getByRole("button", { name: "关闭用户编辑" }) as HTMLButtonElement).disabled).toBe(true);
    await user.click(dialog.parentElement!);
    expect(screen.getByRole("dialog", { name: "编辑用户" })).toBe(dialog);
    rejectSave(new Error("合成保存失败"));
    await within(dialog).findByText("保存用户失败。");
    expect((screen.getByLabelText("显示名称") as HTMLInputElement).value).toBe("未保存草稿");
  });

  it("保留真实用户创建 API，并以全屏抽屉编辑用户", async () => {
    const user = userEvent.setup();
    render(<AdminConsole currentUser={currentUser} activeTab="users" />);

    expect(await screen.findByRole("heading", { name: "用户管理" })).not.toBeNull();
    expect(screen.getByText("analyst_demo")).not.toBeNull();

    await user.click(screen.getByRole("button", { name: "新增用户" }));
    const drawer = screen.getByRole("dialog", { name: "新增用户" });
    await user.type(screen.getByLabelText("用户名"), "reviewer_demo");
    await user.type(screen.getByLabelText("初始密码"), "password-123");
    await user.type(screen.getByLabelText("显示名称"), "复核员");
    await user.click(screen.getByRole("button", { name: "保存" }));

    await waitFor(() => expect(api.createAdminUser).toHaveBeenCalledWith({
      username: "reviewer_demo",
      password: "password-123",
      display_name: "复核员",
      role: "user",
      is_active: true,
    }));
    expect(drawer).not.toBeNull();
    expect(document.body.contains(drawer)).toBe(false);
  });

  it("禁止删除当前账户，并对其他用户执行二次确认", async () => {
    const user = userEvent.setup();
    render(<AdminConsole currentUser={currentUser} activeTab="users" />);
    await screen.findByText("analyst_demo");

    expect((screen.getByRole("button", { name: "不能删除当前登录账户" }) as HTMLButtonElement).disabled).toBe(true);
    await user.click(screen.getByRole("button", { name: "删除 analyst_demo" }));
    expect(screen.getByRole("dialog", { name: "确认删除用户" })).not.toBeNull();
    expect(screen.getByText(/其个人模型配置/)).not.toBeNull();

    await user.click(screen.getByRole("button", { name: "删除用户" }));
    await waitFor(() => expect(api.deleteAdminUser).toHaveBeenCalledWith("user-1"));
  });

  it("保留空间归属编辑和删除确认 API", async () => {
    const user = userEvent.setup();
    render(<AdminConsole currentUser={currentUser} activeTab="spaces" />);
    expect(await screen.findByRole("heading", { name: "资料空间" })).not.toBeNull();
    expect(screen.getByText("追尾事故档案")).not.toBeNull();

    await user.click(screen.getByRole("button", { name: "编辑 追尾事故档案" }));
    await user.clear(screen.getByLabelText("排序号"));
    await user.type(screen.getByLabelText("排序号"), "3");
    await user.click(screen.getByRole("button", { name: "保存" }));
    await waitFor(() => expect(api.updateAdminSpace).toHaveBeenCalledWith("session-1", { owner_user_id: "user-1", sort_order: 3 }));

    await user.click(screen.getByRole("button", { name: "删除 追尾事故档案" }));
    expect(screen.getByRole("dialog", { name: "确认删除空间" })).not.toBeNull();
    await user.click(screen.getByRole("button", { name: "删除空间" }));
    await waitFor(() => expect(api.deleteAdminSpace).toHaveBeenCalledWith("session-1"));
  });
});
