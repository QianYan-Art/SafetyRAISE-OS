// @vitest-environment jsdom
import { describe, expect, it, beforeEach } from "vitest";
import { safeMarkdownUrlTransform } from "./markdownSafety";
import { clearStoredSessions } from "./useChatHistory";

describe("markdownSafety", () => {
  it("allows https, mailto, and relative URLs while blocking dangerous protocols", () => {
    expect(safeMarkdownUrlTransform("https://example.com/report")).toBe("https://example.com/report");
    expect(safeMarkdownUrlTransform("mailto:security@example.com")).toBe("mailto:security@example.com");
    expect(safeMarkdownUrlTransform("/docs/guide")).toBe("/docs/guide");
    expect(safeMarkdownUrlTransform("#section-1")).toBe("#section-1");

    expect(safeMarkdownUrlTransform("javascript:alert(1)")).toBe("");
    expect(safeMarkdownUrlTransform("data:text/html,<script>alert(1)</script>")).toBe("");
    expect(safeMarkdownUrlTransform("http://example.com/insecure")).toBe("");
    expect(safeMarkdownUrlTransform("ircs://example.com/chan")).toBe("");
    expect(safeMarkdownUrlTransform("//evil.example/phish")).toBe("");
  });
});

describe("clearStoredSessions", () => {
  beforeEach(() => {
    window.localStorage.clear();
  });

  it("removes all user session PII buckets on logout while keeping unrelated keys", () => {
    window.localStorage.setItem("traffic_accident_chat_sessions:user-1", '{"draft":"pii"}');
    window.localStorage.setItem("traffic_accident_chat_sessions:user-2", '{"draft":"pii2"}');
    window.localStorage.setItem("theme-mode", "dark");

    clearStoredSessions();

    expect(window.localStorage.getItem("traffic_accident_chat_sessions:user-1")).toBeNull();
    expect(window.localStorage.getItem("traffic_accident_chat_sessions:user-2")).toBeNull();
    expect(window.localStorage.getItem("theme-mode")).toBe("dark");
  });
});
