// @vitest-environment jsdom

import { cleanup, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";

import { AuthScreen } from "./AuthScreen";

function renderAuth(errorMessage: string, onClearError = vi.fn()) {
  const props = {
    themeMode: "light" as const, loading: false, errorMessage, onClearError,
    onToggleTheme: vi.fn(),
    onLogin: vi.fn().mockResolvedValue(undefined),
    onRegister: vi.fn().mockResolvedValue(undefined),
  };
  const view = render(<AuthScreen {...props} />);
  return { ...view, props };
}

describe("AuthScreen", () => {
  afterEach(cleanup);

  it("切换登录与注册时清掉上一次请求留下的错误", async () => {
    const user = userEvent.setup();
    const { props, rerender } = renderAuth("用户名或密码错误");
    expect(screen.getByRole("alert").textContent).toBe("用户名或密码错误");

    await user.click(screen.getByRole("button", { name: "注册" }));
    expect(props.onClearError).toHaveBeenCalledTimes(1);
    rerender(<AuthScreen {...props} errorMessage="" />);
    expect(screen.queryByText("用户名或密码错误")).toBeNull();
    expect(screen.getByRole("heading", { name: "注册新账号" })).not.toBeNull();
    expect(screen.getByRole("button", { name: "注册", pressed: true })).not.toBeNull();
  });

  it("只保留一个主题切换按钮", () => {
    renderAuth("");
    expect(screen.getAllByRole("button", { name: /切换为(深|浅)色模式/ })).toHaveLength(1);
  });

  it("在密码框按回车提交登录，并提供浏览器可识别的自动填充字段", async () => {
    const user = userEvent.setup();
    const { props } = renderAuth("");
    const username = screen.getByLabelText("用户名");
    const password = screen.getByLabelText("密码");
    expect(username.getAttribute("autocomplete")).toBe("username");
    expect(password.getAttribute("autocomplete")).toBe("current-password");

    await user.type(username, " analyst ");
    await user.type(password, "secret123{Enter}");
    expect(props.onLogin).toHaveBeenCalledWith({ username: "analyst", password: "secret123" });
  });

  it("忘记密码给出提示而不是错误", async () => {
    const user = userEvent.setup();
    renderAuth("");
    await user.click(screen.getByRole("button", { name: "忘记密码？" }));
    expect(screen.getByRole("status").textContent).toContain("联系管理员重置");
    expect(screen.queryByRole("alert")).toBeNull();
  });

  it("注册校验失败时标记对应字段且不发请求", async () => {
    const user = userEvent.setup();
    const { props } = renderAuth("");
    await user.click(screen.getByRole("button", { name: "注册" }));
    expect(screen.getByLabelText("密码").getAttribute("autocomplete")).toBe("new-password");

    await user.type(screen.getByLabelText("用户名"), "analyst");
    await user.type(screen.getByLabelText("密码"), "abcd1234");
    await user.type(screen.getByLabelText("确认密码"), "abcd12345{Enter}");
    expect(props.onRegister).not.toHaveBeenCalled();
    expect(screen.getByLabelText("确认密码").getAttribute("aria-invalid")).toBe("true");
    expect(screen.getByRole("alert").textContent).toBe("两次输入的密码不一致。");
  });
});
