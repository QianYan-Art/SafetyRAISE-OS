import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import { createHash } from "node:crypto";
import fs from "node:fs/promises";
import os from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { chromium } from "playwright";

const baseUrl = process.env.PRODUCTION_SMOKE_URL || "https://safetyraise.cn";
const sshHost = process.env.PRODUCTION_SMOKE_SSH_HOST;
const sshPort = process.env.PRODUCTION_SMOKE_SSH_PORT || "23333";
const sshKey = process.env.PRODUCTION_SMOKE_SSH_KEY;
const expectedCommit = process.env.PRODUCTION_SMOKE_EXPECTED_COMMIT;
const apiTimeoutMs = Number(process.env.PRODUCTION_SMOKE_API_TIMEOUT_MS || "30000");
const frontendDir = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const repoRoot = path.resolve(frontendDir, "..");
const expectedIndexPath = path.join(frontendDir, "dist", "index.html");
const outputDir = process.env.PRODUCTION_SMOKE_OUTPUT
  || await fs.mkdtemp(path.join(os.tmpdir(), "safetyraise-production-smoke-"));
if (!sshHost || !sshKey) throw new Error("缺少生产烟测SSH连接参数。");
if (!expectedCommit || !/^[0-9a-f]{40}$/i.test(expectedCommit)) {
  throw new Error("PRODUCTION_SMOKE_EXPECTED_COMMIT 必须是待验收提交的完整40位SHA。");
}
if (!Number.isFinite(apiTimeoutMs) || apiTimeoutMs < 1000) {
  throw new Error("PRODUCTION_SMOKE_API_TIMEOUT_MS 必须是不小于 1000 的毫秒数。");
}
const localCommitRun = spawnSync("git", ["rev-parse", "HEAD"], {
  cwd: repoRoot,
  encoding: "utf8",
  timeout: 10_000,
});
if (localCommitRun.status !== 0) throw new Error("无法读取本地待验收提交。");
const localCommit = localCommitRun.stdout.trim();
assert.equal(localCommit, expectedCommit, "烟测期望提交必须等于本地HEAD");
const localStatusRun = spawnSync("git", ["status", "--porcelain"], {
  cwd: repoRoot,
  encoding: "utf8",
  timeout: 10_000,
});
if (localStatusRun.status !== 0) throw new Error("无法核对本地工作区状态。");
assert.equal(localStatusRun.stdout.trim(), "", "生产候选烟测必须从干净工作区运行");

await fs.mkdir(outputDir, { recursive: true });
const suffix = (Date.now().toString(36) + Math.random().toString(36).slice(2, 6)).slice(-12);
const user = {
  username: ("smoke_u_" + suffix).slice(0, 20),
  password: "Safe" + suffix + "9a",
  displayName: "生产烟测普通用户·" + suffix,
};
const admin = {
  username: ("smoke_a_" + suffix).slice(0, 20),
  password: "Safe" + suffix + "8b",
  displayName: "生产烟测管理员·" + suffix,
};
const sessionId = "prod-smoke-" + suffix;
const syntheticApiKey = "synthetic-disposable-key";
const forbiddenRequestPattern = /report-runs|report-evidence|generate-from-upload|reports\/generate/;
const sensitiveKeyPattern = /api.?key|password|token|secret|authorization|cookie/i;
const result = {
  scope: "生产前端与真实后端合成数据烟测，不调用模型",
  baseUrl,
  outputDir,
  build: {
    expected_commit: expectedCommit,
    local_commit: localCommit,
    local_worktree_clean: true,
    assets: [],
  },
  checks: [],
  cleanup: {},
};
const smokeAbortController = new AbortController();
const attemptedUsers = [];
const confirmedUsernames = [];
let userToken = "";
let adminToken = "";
let sessionAttempted = false;
let backendContainer = "";
let activeBrowser = null;
let interruptedSignal = "";

function redactKnownSecrets(text) {
  let redacted = String(text);
  for (const secret of [user.password, admin.password, userToken, adminToken, syntheticApiKey]) {
    if (secret) redacted = redacted.split(secret).join("[redacted]");
  }
  return redacted.replace(/(Bearer\s+)[^\s"']+/gi, "$1[redacted]");
}

function sanitizeForOutput(value, key = "") {
  if (sensitiveKeyPattern.test(key)) return value ? "[redacted]" : value;
  if (typeof value === "string") return redactKnownSecrets(value);
  if (Array.isArray(value)) return value.map(item => sanitizeForOutput(item));
  if (value && typeof value === "object") {
    return Object.fromEntries(
      Object.entries(value).map(([entryKey, entryValue]) => [
        entryKey,
        sanitizeForOutput(entryValue, entryKey),
      ]),
    );
  }
  return value;
}

function safeError(error) {
  const raw = error instanceof Error ? (error.stack || error.message) : String(error);
  return redactKnownSecrets(raw).slice(0, 4000);
}

function sha256(value) {
  return createHash("sha256").update(value).digest("hex");
}

function runSshCommand(command, options = {}) {
  return spawnSync("ssh", [
    "-o", "BatchMode=yes", "-i", sshKey, "-p", sshPort, sshHost, command,
  ], {
    encoding: "utf8",
    timeout: options.timeout || 45_000,
    input: options.input,
  });
}

function resolveBackendContainer() {
  const run = runSshCommand(
    "docker ps --filter label=com.docker.compose.service=backend --format '{{.Names}}'",
  );
  if (run.status !== 0) {
    throw new Error("无法解析backend容器：" + (run.error?.message || run.stderr || run.stdout));
  }
  const names = run.stdout.split(/\r?\n/).map(item => item.trim()).filter(Boolean);
  assert.equal(names.length, 1, "生产环境必须且只能解析到一个运行中的backend服务容器");
  assert.match(names[0], /^[A-Za-z0-9_.-]+$/, "backend容器名包含非法字符");
  return names[0];
}

function extractAssetPaths(html) {
  const assets = [];
  for (const match of String(html).matchAll(/\b(?:src|href)=["']([^"']+)["']/g)) {
    const pathname = new URL(match[1], baseUrl).pathname;
    if (pathname.startsWith("/assets/")) assets.push(pathname);
  }
  return [...new Set(assets)].sort();
}

function maskUsername(username) {
  if (username.length <= 2) return username[0] + "*";
  if (username.length <= 6) return username[0] + "***" + username.at(-1);
  return username.slice(0, 2) + "***" + username.slice(-2);
}

async function api(route, options = {}) {
  const method = options.method || "GET";
  const requestAbort = new AbortController();
  let timedOut = false;
  const timeoutId = setTimeout(() => {
    timedOut = true;
    requestAbort.abort(new Error("生产API请求超时"));
  }, apiTimeoutMs);
  const forwardAbort = () => requestAbort.abort(smokeAbortController.signal.reason);
  if (!options.ignoreInterrupt) {
    if (smokeAbortController.signal.aborted) forwardAbort();
    else smokeAbortController.signal.addEventListener("abort", forwardAbort, { once: true });
  }

  try {
    const response = await fetch(baseUrl + route, {
      method,
      signal: requestAbort.signal,
      headers: {
        ...(options.token ? { Authorization: "Bearer " + options.token } : {}),
        ...(options.body === undefined ? {} : { "Content-Type": "application/json" }),
      },
      body: options.body === undefined ? undefined : JSON.stringify(options.body),
    });
    const text = await response.text();
    let payload = null;
    try { payload = text ? JSON.parse(text) : null; } catch { payload = text; }
    if (!response.ok && options.allowNotFound && response.status === 404) {
      return { not_found: true };
    }
    if (!response.ok) {
      const summary = redactKnownSecrets(JSON.stringify(sanitizeForOutput(payload))).slice(0, 1000);
      throw new Error(method + " " + route + " -> " + response.status + ": " + summary);
    }
    return payload;
  } catch (error) {
    if (timedOut) throw new Error(method + " " + route + " 在 " + apiTimeoutMs + "ms 内无响应");
    if (!options.ignoreInterrupt && smokeAbortController.signal.aborted) {
      throw new Error("生产烟测已中断：" + (interruptedSignal || "主动终止"));
    }
    throw error;
  } finally {
    clearTimeout(timeoutId);
    smokeAbortController.signal.removeEventListener("abort", forwardAbort);
  }
}

async function fetchAssetBytes(assetPath) {
  const requestAbort = new AbortController();
  let timedOut = false;
  const timeoutId = setTimeout(() => {
    timedOut = true;
    requestAbort.abort(new Error("生产静态资源请求超时"));
  }, apiTimeoutMs);
  const forwardAbort = () => requestAbort.abort(smokeAbortController.signal.reason);
  if (smokeAbortController.signal.aborted) forwardAbort();
  else smokeAbortController.signal.addEventListener("abort", forwardAbort, { once: true });
  try {
    const response = await fetch(baseUrl + assetPath, {
      signal: requestAbort.signal,
      headers: { "Cache-Control": "no-cache" },
    });
    if (!response.ok) throw new Error("GET " + assetPath + " -> " + response.status);
    return Buffer.from(await response.arrayBuffer());
  } catch (error) {
    if (timedOut) throw new Error("GET " + assetPath + " 在 " + apiTimeoutMs + "ms 内无响应");
    if (smokeAbortController.signal.aborted) {
      throw new Error("生产烟测已中断：" + (interruptedSignal || "主动终止"));
    }
    throw error;
  } finally {
    clearTimeout(timeoutId);
    smokeAbortController.signal.removeEventListener("abort", forwardAbort);
  }
}

function runAdminOperation(action, targets) {
  const source = [
    "import json, os, urllib.request",
    "base = 'http://127.0.0.1:8000'",
    "def request(path, method='GET', body=None, token=None):",
    "    data = None if body is None else json.dumps(body).encode()",
    "    headers = {'Content-Type': 'application/json'} if body is not None else {}",
    "    if token: headers['Authorization'] = 'Bearer ' + token",
    "    req = urllib.request.Request(base + path, data=data, headers=headers, method=method)",
    "    with urllib.request.urlopen(req, timeout=20) as response:",
    "        raw = response.read().decode()",
    "        return json.loads(raw) if raw else None",
    "token = request('/api/v1/auth/login', 'POST', {",
    "    'username': os.environ['BOOTSTRAP_ADMIN_USERNAME'],",
    "    'password': os.environ['BOOTSTRAP_ADMIN_PASSWORD'],",
    "})['access_token']",
    "targets = " + JSON.stringify(targets),
    "target_names = targets if " + JSON.stringify(action) + " == 'promote' else [item['username'] for item in targets]",
    "users = request('/api/v1/admin/users', token=token)",
    "found = {row['username']: row for row in users if row['username'] in target_names}",
    "result = {'action': " + JSON.stringify(action) + ", 'deleted': [], 'missing': [], 'failed': []}",
    "if " + JSON.stringify(action) + " == 'promote':",
    "    target = found[targets[0]]",
    "    request('/api/v1/admin/users/' + target['id'], 'PUT', {'role': 'admin', 'is_active': True}, token)",
    "    result['promoted'] = targets[0]",
    "else:",
    "    for spec in targets:",
    "        username = spec['username']",
    "        row = found.get(username)",
    "        if not row:",
    "            result['missing'].append(username)",
    "            continue",
    "        if row.get('display_name') != spec['display_name']:",
    "            result['failed'].append({'username': username, 'error': 'identity_mismatch'})",
    "            continue",
    "        try:",
    "            request('/api/v1/admin/users/' + row['id'], 'DELETE', token=token)",
    "        except Exception as exc:",
    "            result['failed'].append({'username': username, 'error': type(exc).__name__})",
    "        else:",
    "            result['deleted'].append(username)",
    "print(json.dumps(result, ensure_ascii=False))",
  ].join("\n");
  const run = runSshCommand(
    "docker exec -i " + backendContainer + " python -",
    { input: source, timeout: 45_000 },
  );
  if (run.status !== 0) {
    throw new Error("管理操作失败：" + (run.error?.message || run.stderr || run.stdout));
  }
  return JSON.parse(run.stdout.trim());
}

function watchPage(page, label) {
  const pageErrors = [];
  const forbiddenRequests = [];
  page.on("pageerror", error => pageErrors.push(error.message));
  page.on("request", request => {
    if (forbiddenRequestPattern.test(request.url())) {
      forbiddenRequests.push(request.method() + " " + request.url());
    }
  });
  return () => {
    assert.deepEqual(pageErrors, [], label + "不应出现页面脚本错误");
    assert.deepEqual(forbiddenRequests, [], label + "在harness关闭时不应发起报告增强请求");
  };
}

async function loginPage(page, credentials) {
  await page.goto(baseUrl, { waitUntil: "networkidle" });
  await page.getByLabel("用户名", { exact: true }).fill(credentials.username);
  await page.getByLabel("密码", { exact: true }).fill(credentials.password);
  await page.locator("button.auth-submit-btn").click();
  await page.getByRole("button", { name: "打开档案列表", exact: true }).waitFor();
}

async function verifyProductionBuild() {
  const npmCommand = process.platform === "win32" ? "npm.cmd" : "npm";
  const build = spawnSync(npmCommand, ["run", "build"], {
    cwd: frontendDir,
    encoding: "utf8",
    timeout: 120_000,
  });
  if (build.status !== 0) {
    throw new Error("无法从待验收提交重建前端：" + redactKnownSecrets(
      build.error?.message || build.stderr || build.stdout,
    ).slice(0, 2000));
  }
  result.build.rebuilt_from_clean_head = true;
  const expectedHtml = await fs.readFile(expectedIndexPath, "utf8");
  const productionHtml = await api("/?production-smoke-build=" + suffix);
  assert.equal(typeof productionHtml, "string", "生产首页必须返回HTML");
  const expectedAssets = extractAssetPaths(expectedHtml);
  const productionAssets = extractAssetPaths(productionHtml);
  assert(expectedAssets.length >= 2, "本地候选构建必须包含JS和CSS资源");
  assert.deepEqual(productionAssets, expectedAssets, "线上静态资源必须与本地候选构建一致");
  const fingerprints = [];
  for (const assetPath of expectedAssets) {
    const localBytes = await fs.readFile(path.join(frontendDir, "dist", assetPath.slice(1)));
    const productionBytes = await fetchAssetBytes(assetPath);
    const localSha256 = sha256(localBytes);
    const productionSha256 = sha256(productionBytes);
    assert.equal(productionSha256, localSha256, assetPath + "线上内容必须与本地重建产物一致");
    fingerprints.push({ path: assetPath, sha256: localSha256 });
  }
  result.build.assets = fingerprints;
  result.checks.push("发布版本：干净提交、本地重建及线上JS/CSS内容SHA-256一致");
}

async function runSmoke() {
  backendContainer = resolveBackendContainer();
  result.infrastructure = { backend_container: backendContainer };
  await verifyProductionBuild();

  attemptedUsers.push({ username: user.username, display_name: user.displayName });
  const userAuth = await api("/api/v1/auth/register", {
    method: "POST",
    body: { username: user.username, password: user.password, display_name: user.displayName },
  });
  confirmedUsernames.push(user.username);
  userToken = userAuth.access_token;
  attemptedUsers.push({ username: admin.username, display_name: admin.displayName });
  const adminAuth = await api("/api/v1/auth/register", {
    method: "POST",
    body: { username: admin.username, password: admin.password, display_name: admin.displayName },
  });
  confirmedUsernames.push(admin.username);
  adminToken = adminAuth.access_token;
  runAdminOperation("promote", [admin.username]);
  result.checks.push("真实认证：注册、登录与管理员提权接口通过");

  const appConfig = await api("/api/v1/app-config", { token: userToken });
  assert.equal(appConfig.report_harness?.enabled, false, "未通过质量门的harness必须保持关闭");
  sessionAttempted = true;
  const created = await api("/api/v1/chat-sessions", {
    method: "POST",
    token: userToken,
    body: {
      id: sessionId,
      title: "线上合成验收档案",
      source_type: "image",
      source_name: "synthetic-production-smoke",
      messages: [],
      draft_json: JSON.stringify({
        事故时间: "待核实",
        事故地点: "生产烟测道路",
        事故经过: "仅用于真实后端和前端契约验证，不是真实事故。",
      }),
      draft_meta: { smoke_test: true },
    },
  });
  assert.equal(created.id, sessionId);
  const linkedArtifacts = await api(
    "/api/v1/chat-sessions/" + sessionId + "/linked-artifacts",
    { token: userToken },
  );
  const structuredArtifact = linkedArtifacts.find(item => item.category === "structured_accident_info");
  assert(structuredArtifact && structuredArtifact.item_count >= 1);

  const configState = await api("/api/v1/user/model-configs", { token: userToken });
  assert(Array.isArray(configState.capabilities));
  const updatedConfig = await api("/api/v1/user/model-configs", {
    method: "PUT",
    token: userToken,
    body: {
      items: [{
        capability: "vision",
        base_url: "https://example.invalid/v1",
        model_name: "synthetic-never-called",
        api_key: syntheticApiKey,
        params: null,
      }],
    },
  });
  const updatedVision = updatedConfig.capabilities.find(item => item.capability === "vision");
  assert.equal(updatedVision?.model_name, "synthetic-never-called");
  assert.equal(Object.hasOwn(updatedVision || {}, "api_key"), false, "读响应不得返回API key明文字段");
  assert.equal(updatedVision?.api_key_masked, "••••-key", "读响应只能返回打码API key");
  const syntheticCapabilities = ["vision", "report"].map(capability => ({
    capability,
    base_url: "https://example.invalid/v1",
    model_name: "synthetic-never-called",
    api_key: syntheticApiKey,
    params: null,
  }));
  await api("/api/v1/user/model-configs", {
    method: "PUT",
    token: userToken,
    body: { items: [syntheticCapabilities[1]] },
  });
  await api("/api/v1/user/model-configs", {
    method: "PUT",
    token: adminToken,
    body: { items: syntheticCapabilities },
  });
  const adminSpaces = await api("/api/v1/admin/spaces", { token: adminToken });
  const smokeSpace = adminSpaces.find(item => item.session_id === sessionId);
  assert(smokeSpace);
  assert.equal(smokeSpace.title, "#" + sessionId.slice(-6));
  assert.equal(smokeSpace.owner_username, maskUsername(user.username));
  result.checks.push("真实数据层：会话、结构化关联产物、单用途模型配置与密钥脱敏通过");

  activeBrowser = await chromium.launch({ headless: true });
  try {
    const userContext = await activeBrowser.newContext({ viewport: { width: 1440, height: 960 } });
    const userPage = await userContext.newPage();
    const assertUserPage = watchPage(userPage, "普通用户桌面页");
    await loginPage(userPage, user);
    await userPage.getByRole("button", { name: /核对事实/ }).first().click();
    const location = userPage.getByRole("textbox", { name: "事故地点", exact: true });
    await location.waitFor();
    await location.fill("生产烟测道路-已保存");
    await userPage.getByRole("button", { name: "保存修改", exact: true }).click();
    await userPage.getByText("已保存", { exact: true }).waitFor();
    await userPage.reload({ waitUntil: "networkidle" });
    await userPage.getByRole("button", { name: /核对事实/ }).first().click();
    assert.equal(
      await userPage.getByRole("textbox", { name: "事故地点", exact: true }).inputValue(),
      "生产烟测道路-已保存",
    );
    await userPage.getByTitle("模型配置调整", { exact: true }).click();
    assert.equal(
      await userPage.getByLabel("模型名称", { exact: true }).first().inputValue(),
      "synthetic-never-called",
    );
    await userPage.screenshot({ path: path.join(outputDir, "production-user-1440.png"), fullPage: true });
    assertUserPage();
    await userContext.close();

    const mobileContext = await activeBrowser.newContext({ viewport: { width: 390, height: 844 } });
    const mobilePage = await mobileContext.newPage();
    const assertMobilePage = watchPage(mobilePage, "普通用户手机页");
    await loginPage(mobilePage, user);
    assert.equal(await mobilePage.evaluate(() => document.documentElement.scrollWidth > innerWidth + 1), false);
    await mobilePage.getByRole("button", { name: "打开档案列表", exact: true }).click();
    const archiveDrawer = mobilePage.getByRole("dialog", { name: "档案导航", exact: true });
    await archiveDrawer.waitFor();
    await archiveDrawer.getByText("线上合成验收档案", { exact: true }).waitFor();
    await mobilePage.getByTitle("关闭档案导航", { exact: true }).click();
    await mobilePage.screenshot({ path: path.join(outputDir, "production-user-390.png"), fullPage: true });
    assertMobilePage();
    await mobileContext.close();
    result.checks.push("真实普通用户界面：桌面与手机、事实保存刷新、档案导航及模型配置展示通过");

    const adminContext = await activeBrowser.newContext({ viewport: { width: 1440, height: 960 } });
    const adminPage = await adminContext.newPage();
    const assertAdminPage = watchPage(adminPage, "管理员桌面页");
    await loginPage(adminPage, admin);
    await adminPage.getByTitle("打开管理控制台", { exact: true }).click();
    await adminPage.getByRole("heading", { name: "用户管理", exact: true }).waitFor();
    await adminPage.getByText(user.username, { exact: true }).waitFor();
    await adminPage.getByRole("button", { name: "编辑 " + user.username, exact: true }).click();
    const userDialog = adminPage.getByRole("dialog", { name: "编辑用户", exact: true });
    assert.equal(await userDialog.getByLabel("显示名称", { exact: true }).inputValue(), user.displayName);
    await userDialog.getByRole("button", { name: "关闭用户编辑", exact: true }).click();
    await adminPage.getByRole("tab", { name: "空间管理", exact: true }).click();
    await adminPage.getByRole("heading", { name: "资料空间", exact: true }).waitFor();
    await adminPage.getByText(smokeSpace.title, { exact: true }).waitFor();
    await adminPage.getByRole("button", { name: "编辑 " + smokeSpace.title, exact: true }).click();
    const spaceDialog = adminPage.getByRole("dialog", { name: "空间元数据编辑", exact: true });
    assert.equal(await spaceDialog.locator("select.account-input").inputValue(), smokeSpace.owner_user_id);
    await spaceDialog.getByRole("button", { name: "关闭空间编辑", exact: true }).click();
    await adminPage.screenshot({ path: path.join(outputDir, "production-admin-1440.png"), fullPage: true });
    assertAdminPage();
    await adminContext.close();
    result.checks.push("真实管理员界面：用户编辑、空间归属、脱敏标识及harness关闭契约通过");
  } finally {
    await activeBrowser.close().catch(() => undefined);
    activeBrowser = null;
  }
}

async function cleanupSyntheticData() {
  const cleanupErrors = [];
  if (sessionAttempted && userToken) {
    try {
      const cleanup = await api("/api/v1/chat-sessions/" + sessionId, {
        method: "DELETE",
        token: userToken,
        ignoreInterrupt: true,
        allowNotFound: true,
      });
      result.cleanup.session = cleanup?.not_found ? "missing" : true;
    } catch (error) {
      const message = safeError(error);
      result.cleanup.session = "failed: " + message;
      cleanupErrors.push("会话清理失败：" + message);
    }
  }

  if (attemptedUsers.length && backendContainer) {
    try {
      const cleanup = runAdminOperation("cleanup", attemptedUsers);
      result.cleanup.users = cleanup;
      const accounted = [
        ...cleanup.deleted,
        ...cleanup.missing,
        ...cleanup.failed.map(item => item.username),
      ].sort();
      const attemptedUsernames = attemptedUsers.map(item => item.username).sort();
      assert.deepEqual(accounted, attemptedUsernames, "每个尝试注册的烟测账号都必须有清理结果");
      assert.deepEqual(cleanup.failed, [], "烟测账号清理不得存在失败项");
      const absent = new Set([...cleanup.deleted, ...cleanup.missing]);
      for (const username of confirmedUsernames) {
        assert(absent.has(username), "已确认注册的烟测账号必须删除或确认不存在");
      }
    } catch (error) {
      const message = safeError(error);
      if (result.cleanup.users && typeof result.cleanup.users === "object") {
        result.cleanup.users.validation_error = message;
      } else {
        result.cleanup.users = "failed: " + message;
      }
      cleanupErrors.push("账号清理失败：" + message);
    }
  }

  if (cleanupErrors.length) {
    result.passed = false;
    result.cleanup.failed = true;
    result.error = [result.error, ...cleanupErrors].filter(Boolean).join("\n");
    process.exitCode = 1;
  }
}

let rejectInterrupt;
const interruptPromise = new Promise((_resolve, reject) => { rejectInterrupt = reject; });
function handleSignal(signal) {
  if (interruptedSignal) return;
  interruptedSignal = signal;
  smokeAbortController.abort(new Error("收到 " + signal));
  if (activeBrowser) void activeBrowser.close().catch(() => undefined);
  rejectInterrupt(new Error("收到 " + signal + "，开始清理生产烟测数据"));
}
const onSigint = () => handleSignal("SIGINT");
const onSigterm = () => handleSignal("SIGTERM");
process.once("SIGINT", onSigint);
process.once("SIGTERM", onSigterm);

const runPromise = runSmoke();
try {
  await Promise.race([runPromise, interruptPromise]);
  result.passed = true;
} catch (error) {
  result.passed = false;
  result.error = safeError(error);
  if (interruptedSignal) result.signal = interruptedSignal;
  process.exitCode = 1;
} finally {
  smokeAbortController.abort(new Error("生产烟测结束"));
  if (activeBrowser) await activeBrowser.close().catch(() => undefined);
  await Promise.race([
    runPromise.catch(() => undefined),
    new Promise(resolve => setTimeout(resolve, 5000)),
  ]);
  await cleanupSyntheticData();
  process.removeListener("SIGINT", onSigint);
  process.removeListener("SIGTERM", onSigterm);
  const publicResult = sanitizeForOutput(result);
  await fs.writeFile(path.join(outputDir, "result.json"), JSON.stringify(publicResult, null, 2));
  console.log(JSON.stringify(publicResult));
}
