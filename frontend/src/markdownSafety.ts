import { createElement } from "react";
import type { Components } from "react-markdown";

const ALLOWED_PROTOCOLS = new Set(["https:", "mailto:"]);

/**
 * 显式限制 Markdown 渲染中的链接/资源协议，仅放行 https:、mailto: 及同源相对路径。
 */
export function safeMarkdownUrlTransform(rawUrl: string): string {
  const trimmed = String(rawUrl || "").trim();
  if (!trimmed) {
    return "";
  }
  if (
    (trimmed.startsWith("/") && !trimmed.startsWith("//"))
    || trimmed.startsWith("#")
    || trimmed.startsWith("./")
    || trimmed.startsWith("../")
  ) {
    return trimmed;
  }
  try {
    const parsed = new URL(trimmed);
    if (ALLOWED_PROTOCOLS.has(parsed.protocol.toLowerCase())) {
      return trimmed;
    }
  } catch {
    return "";
  }
  return "";
}

export const safeMarkdownComponents: Components = {
  a: ({ node: _node, children, ...props }) =>
    createElement("a", { ...props, rel: "noopener noreferrer" }, children),
};
