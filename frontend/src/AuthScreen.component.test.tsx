// @vitest-environment jsdom

import { cleanup, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";

import { AuthScreen } from "./AuthScreen";

function renderAuth(errorMessage: string, onClearError = vi.fn()) {
  const props = {
    themeMode: "light" as const, loading: false, errorMessage, onClearError,
    onToggleTheme: vi.fn(), onLogin: vi.fn(), onRegister: vi.fn(),
  };
  const view = render(<AuthScreen {...props} />);
  return { ...view, props };
}

describe("AuthScreen", () => {
  afterEach(cleanup);

  it("切换登录与注册时清掉上一次请求留下的错误", async () => {
    const user = userEvent.setup();
    const { props, rerender } = renderAuth("用户名或密码错误");
    expect(screen.getByText("用户名或密码错误")).not.toBeNull();

    await user.click(screen.getByRole("button", { name: "注册" }));
    expect(props.onClearError).toHaveBeenCalledTimes(1);
    rerender(<AuthScreen {...props} errorMessage="" />);
    expect(screen.queryByText("用户名或密码错误")).toBeNull();
    expect(screen.getByRole("heading", { name: "注册新账号" })).not.toBeNull();
  });

  it("只保留一个主题切换按钮", () => {
    renderAuth("");
    expect(screen.getAllByRole("button", { name: /切换为(深|浅)色模式/ })).toHaveLength(1);
  });
});
