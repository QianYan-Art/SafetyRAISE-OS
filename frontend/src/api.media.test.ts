// @vitest-environment jsdom
import { afterEach, expect, it, vi } from "vitest";
import {
  authorizeChatSessionMedia, buildChatSessionLinkedArtifactAssetUrl,
  clearChatSessionMediaAccess, generateInputFromUploads, login, persistAuthToken, register,
} from "./api";

afterEach(() => { vi.unstubAllGlobals(); localStorage.clear(); });

it.each([["登录", login, "login"], ["注册", register, "register"]] as const)(
  "%s新账号前清除旧媒体凭据",
  async (_label, authenticate, endpoint) => {
    const fetcher = vi.fn().mockImplementation(async () => new Response(JSON.stringify({
      status: "success", access_token: "next-test-token",
    })));
    vi.stubGlobal("fetch", fetcher);
    await authenticate("test-user", "test-password");
    expect(fetcher.mock.calls[0][0]).toContain("/chat-sessions/media-access");
    expect(fetcher.mock.calls[0][1].method).toBe("DELETE");
    expect(fetcher.mock.calls[1][0]).toContain(`/auth/${endpoint}`);
  },
);

it("媒体授权带登录头和Cookie，媒体URL不含登录token", async () => {
  const fetcher = vi.fn().mockImplementation(async () => new Response(JSON.stringify({ status: "success" })));
  vi.stubGlobal("fetch", fetcher);
  persistAuthToken("test-private-token");
  await authorizeChatSessionMedia("session-1");
  const [url, init] = fetcher.mock.calls[0];
  expect(url).toContain("/session-1/media-access");
  expect(init.credentials).toBe("include");
  expect(init.headers.get("Authorization")).toBe("Bearer test-private-token");
  const assetUrl = buildChatSessionLinkedArtifactAssetUrl("session-1", "images_and_keyframes", "asset-1");
  expect(assetUrl).not.toContain("test-private-token");
  expect(assetUrl).not.toContain("?");
  await clearChatSessionMediaAccess();
  expect(fetcher.mock.calls[1][1].method).toBe("DELETE");
});

it("分组上传携带会话归属，不由浏览器传服务器文件路径", async () => {
  const fetcher = vi.fn().mockResolvedValue(new Response(JSON.stringify({ status: "success" })));
  vi.stubGlobal("fetch", fetcher);
  await generateInputFromUploads({ files: [], uploadManifest: { groups: [], items: [] } }, "session-1");
  const body = fetcher.mock.calls[0][1].body as FormData;
  expect(body.get("session_id")).toBe("session-1");
  expect(body.has("workspace_dir")).toBe(false);
});
