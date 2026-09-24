import assert from "node:assert/strict";
import { spawn } from "node:child_process";
import { createServer } from "node:net";
import fs from "node:fs/promises";
import path from "node:path";
import os from "node:os";
import { fileURLToPath } from "node:url";
import { chromium } from "playwright";
import { cleanupSucceeded, stopChild, trackChild } from "./process-cleanup.mjs";
import { verifyIntegratedReport } from "./integrated-report-scenario.mjs";

const frontend = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const root = path.dirname(frontend);
const apiPort = Number(process.env.HARNESS_TEST_API_PORT || "18081");
assert(Number.isInteger(apiPort) && apiPort >= 1024 && apiPort <= 65535);
const api = `http://127.0.0.1:${apiPort}`;
// Windows 可能把默认端口划入系统保留段，可用 HARNESS_TEST_UI_PORT 改用其他端口。
const uiPort = Number(process.env.HARNESS_TEST_UI_PORT || "15174");
assert(Number.isInteger(uiPort) && uiPort >= 1024 && uiPort <= 65535);
const ui = `http://127.0.0.1:${uiPort}`;
const output = process.env.REPORT_HARNESS_E2E_OUTPUT
  || await fs.mkdtemp(path.join(os.tmpdir(), "safetyraise-harness-browser-"));
await fs.mkdir(output, { recursive: true });
async function openArchive(target) {
  if (!(await target.getByRole("dialog", { name: "档案导航" }).isVisible())) {
    await target.getByRole("button", { name: "打开档案列表", exact: true }).click();
  }
}
async function closeArchive(target) {
  const drawer = target.getByRole("dialog", { name: "档案导航" });
  if (await drawer.isVisible()) {
    await target.getByTitle("关闭档案导航", { exact: true }).click();
    await drawer.waitFor({ state: "hidden" });
  }
}
// 原编辑器与增强面板共用输入类名，只等输入值会误读即将卸载的原编辑器，须等增强工作台挂载。
async function enterHarnessMode(target) {
  await target.getByLabel("报告模式").selectOption("report-harness");
  await target.locator(".report-harness-workspace").waitFor();
}
assert(process.env.REPORT_HARNESS_TEST_DSN, "必须显式提供独立测试库");

async function requireFreePort(port) {
  const server = createServer();
  await new Promise((resolve, reject) => {
    server.once("error", reject);
    server.listen(port, "127.0.0.1", resolve);
  });
  await new Promise((resolve) => server.close(resolve));
}

async function waitUntil(operation, timeout = 30000) {
  const deadline = Date.now() + timeout;
  let last;
  while (Date.now() < deadline) {
    try {
      const result = await operation();
      if (result) return result;
    } catch (error) {
      last = error;
    }
    await new Promise((resolve) => setTimeout(resolve, 100));
  }
  throw last || new Error("等待可观察状态超时");
}

async function request(url, options = {}) {
  const response = await fetch(url, options);
  assert(response.ok, `${response.status} ${url}: ${await response.clone().text()}`);
  return response.json();
}

const children = [];
const logs = [];
// 预先创建的响应等待可能在前序操作仍卡住时先超时；只记录不终止进程，
// 该等待被 await 时仍会抛出，失败照常进入 catch 留证并由 finally 清理子进程。
process.on("unhandledRejection", (error) => {
  console.error(`等待提前超时：${error?.message ?? error}`);
});
function start(command, args, env) {
  const child = spawn(command, args, {
    cwd: root, env: { ...process.env, ...env }, windowsHide: true,
    stdio: ["ignore", "pipe", "pipe"],
  });
  child.stdout.on("data", (chunk) => logs.push(chunk.toString("utf8")));
  child.stderr.on("data", (chunk) => logs.push(chunk.toString("utf8")));
  children.push(trackChild(child));
  return child;
}

let browser;
let page;
let secondaryContext;
let secondaryPage;
let bootstrap;
const results = [];
const casTrace = [];

function trackSessionTraffic(targetPage, label, sessionUrl) {
  const sessionPath = new URL(sessionUrl).pathname;
  const isSessionUrl = (url) => new URL(url).pathname === sessionPath;
  targetPage.on("request", (request) => {
    if (!isSessionUrl(request.url()) || !["POST", "PUT"].includes(request.method())) {
      return;
    }
    let payload = null;
    try {
      payload = request.postDataJSON();
    } catch {
      payload = request.postData();
    }
    casTrace.push({ label, direction: "request", method: request.method(), payload });
  });
  targetPage.on("response", (response) => {
    if (isSessionUrl(response.url()) && ["POST", "PUT"].includes(response.request().method())) {
      casTrace.push({ label, direction: "response", method: response.request().method(), status: response.status() });
    }
  });
}
try {
  await requireFreePort(apiPort);
  await requireFreePort(uiPort);
  start(path.join(root, ".venv", "Scripts", "python.exe"),
    ["-m", "tests.harness_dev_server"],
    { PYTHONPATH: path.join(root, "backend"), PYTHONIOENCODING: "utf-8" });
  await waitUntil(async () => {
    const response = await fetch(`${api}/api/v1/health`);
    return response.ok;
  });
  bootstrap = await request(`${api}/__harness_test__/bootstrap`);
  start(process.execPath, [
    path.join(frontend, "node_modules/vite/bin/vite.js"),
    "--host", "127.0.0.1", "--port", String(uiPort), "--strictPort",
    "--config", path.join(frontend, "vite.config.ts"), frontend,
  ], { BACKEND_PROXY_TARGET: api });
  await waitUntil(async () => (await fetch(ui)).ok);
  browser = await chromium.launch({ headless: true });
  const context = await browser.newContext({ viewport: { width: 1440, height: 900 } });
  await context.addInitScript((token) => {
    localStorage.setItem("traffic-accident-auth-token", token);
  }, bootstrap.token);
  page = await context.newPage();
  const pageErrors = [];
  page.on("pageerror", (error) => pageErrors.push(error.message));
  await page.goto(ui);
  if (process.env.HARNESS_INTEGRATED_UI_TEST === "1") {
    await verifyIntegratedReport({ page, bootstrap, api, output, results, waitUntil, request });
    assert.deepEqual(pageErrors, []);
    await fs.writeFile(path.join(output, "results.json"), JSON.stringify({ results }, null, 2));
    console.log(JSON.stringify({ passed: results.length, results, output }));
  } else {
  await openArchive(page);
  await page.locator(`[data-session-id="${bootstrap.session_id}"] .session-item`).click();
  const [primaryModeSave] = await Promise.all([
    page.waitForResponse((response) => response.url().endsWith(`/api/v1/chat-sessions/${bootstrap.session_id}`)
      && response.request().method() === "PUT"),
    enterHarnessMode(page),
  ]);
  assert.equal(primaryModeSave.status(), 200);
  const headers = { Authorization: `Bearer ${bootstrap.token}`, "Content-Type": "application/json" };
  const chatSessionUrl = `${api}/api/v1/chat-sessions/${bootstrap.session_id}`;
  await waitUntil(async () => (await page.locator(".json-table-editor .value-input").count()) === 1);

  secondaryContext = await browser.newContext({ viewport: { width: 1440, height: 900 } });
  await secondaryContext.addInitScript((token) => {
    localStorage.setItem("traffic-accident-auth-token", token);
  }, bootstrap.token);
  secondaryPage = await secondaryContext.newPage();
  await secondaryPage.goto(ui);
  await openArchive(secondaryPage);
  await secondaryPage.locator(`[data-session-id="${bootstrap.session_id}"] .session-item`).click();
  const [secondaryModeSave] = await Promise.all([
    secondaryPage.waitForResponse((response) => response.url().endsWith(`/api/v1/chat-sessions/${bootstrap.session_id}`)
      && response.request().method() === "PUT"),
    enterHarnessMode(secondaryPage),
  ]);
  assert.equal(secondaryModeSave.status(), 200);
  await waitUntil(async () => (await secondaryPage.locator(".json-table-editor .value-input").count()) === 1);
  trackSessionTraffic(page, "client-a", chatSessionUrl);
  trackSessionTraffic(secondaryPage, "client-b", chatSessionUrl);

  const primaryRefresh = page.waitForResponse((response) => response.url().endsWith(`/api/v1/chat-sessions/${bootstrap.session_id}`)
    && response.request().method() === "GET");
  await openArchive(page);
  await page.locator(`[data-session-id="${bootstrap.session_id}"] .session-item`).click();
  assert.equal((await primaryRefresh).status(), 200);
  const secondaryRefresh = secondaryPage.waitForResponse((response) => response.url().endsWith(`/api/v1/chat-sessions/${bootstrap.session_id}`)
    && response.request().method() === "GET");
  await openArchive(secondaryPage);
  await secondaryPage.locator(`[data-session-id="${bootstrap.session_id}"] .session-item`).click();
  assert.equal((await secondaryRefresh).status(), 200);

  const clientAInitial = await request(chatSessionUrl, { headers });
  const clientBInitial = await request(chatSessionUrl, { headers });
  assert.equal(clientAInitial.updated_at, clientBInitial.updated_at);
  assert.equal(typeof clientAInitial.updated_at, "number");

  const clientADraft = page.locator(".json-table-editor .value-input").first();
  await clientADraft.fill("客户端A保存");
  const clientAPutRequest = page.waitForRequest((request) => request.url().endsWith(`/api/v1/chat-sessions/${bootstrap.session_id}`)
    && request.method() === "PUT");
  const clientASave = page.waitForResponse((response) => response.url().endsWith(`/api/v1/chat-sessions/${bootstrap.session_id}`)
    && response.request().method() === "PUT");
  await clientADraft.blur();
  const [clientARequest, clientAResponse] = await Promise.all([clientAPutRequest, clientASave]);
  assert.equal(clientAResponse.status(), 200);
  const clientAPayload = clientARequest.postDataJSON();
  assert.equal(clientAPayload.expected_updated_at, clientAInitial.updated_at);
  const clientASaved = await clientAResponse.json();
  assert.equal(typeof clientASaved.updated_at, "number");
  const storedAfterClientA = await request(chatSessionUrl, { headers });
  assert(storedAfterClientA.draft_json.includes("客户端A保存"));
  assert.equal(storedAfterClientA.updated_at, clientASaved.updated_at);

  await page.reload();
  await openArchive(page);
  await page.locator(`[data-session-id="${bootstrap.session_id}"] .session-item`).click();
  await enterHarnessMode(page);
  await waitUntil(async () => (await page.locator(".json-table-editor .value-input").first().inputValue()) === "客户端A保存");
  results.push("真实后端会话保存后刷新仍读取客户端A编辑");

  const raceBaseline = await request(chatSessionUrl, { headers });
  const browserSessionPattern = `**/api/v1/chat-sessions/${bootstrap.session_id}`;
  let releaseConcurrentStrict;
  let resolveConcurrentStrict;
  const concurrentStrictSeen = new Promise((resolve) => {
    resolveConcurrentStrict = resolve;
  });
  await page.route(browserSessionPattern, async (route) => {
    const request = route.request();
    if (request.method() === "PUT") {
      let payload = null;
      try {
        payload = request.postDataJSON();
      } catch {
        payload = null;
      }
      if (payload && Object.hasOwn(payload, "expected_updated_at") && !releaseConcurrentStrict) {
        await new Promise((resolve) => {
          releaseConcurrentStrict = resolve;
          resolveConcurrentStrict({ payload });
        });
      }
    }
    await route.continue();
  });
  try {
    const raceDraft = page.locator(".json-table-editor .value-input").first();
    await raceDraft.fill("严格保存期间的新事故");
    const strictResponse = page.waitForResponse((response) => {
      if (!response.url().endsWith(`/api/v1/chat-sessions/${bootstrap.session_id}`)
        || response.request().method() !== "PUT") {
        return false;
      }
      try {
        return Object.hasOwn(response.request().postDataJSON(), "expected_updated_at");
      } catch {
        return false;
      }
    });
    await raceDraft.blur();
    let strictTimer;
    const heldStrict = await Promise.race([
      concurrentStrictSeen,
      new Promise((_resolve, reject) => {
        strictTimer = setTimeout(() => reject(new Error("失焦后30秒内未发出带版本的严格保存")), 30000);
      }),
    ]).finally(() => clearTimeout(strictTimer));
    assert.equal(heldStrict.payload.expected_updated_at, raceBaseline.updated_at);

    const ordinaryRequest = page.waitForRequest((request) => {
      if (request.url() !== `${ui}/api/v1/chat-sessions/${bootstrap.session_id}`
        || request.method() !== "PUT") {
        return false;
      }
      try {
        const payload = request.postDataJSON();
        return Object.hasOwn(payload, "expected_updated_at") && payload.title === "并发普通重命名";
      } catch {
        return false;
      }
    });
    const ordinaryResponse = page.waitForResponse((response) => {
      if (!response.url().endsWith(`/api/v1/chat-sessions/${bootstrap.session_id}`)
        || response.request().method() !== "PUT") {
        return false;
      }
      try {
        const payload = response.request().postDataJSON();
        return Object.hasOwn(payload, "expected_updated_at") && payload.title === "并发普通重命名";
      } catch {
        return false;
      }
    });
    // 档案导航为覆盖抽屉，抽屉内以铅笔按钮进入重命名。
    await openArchive(page);
    await page.locator(`[data-session-id="${bootstrap.session_id}"] .session-rename-button`).click();
    const titleEditor = page.locator(`[data-session-id="${bootstrap.session_id}"] input`).first();
    await titleEditor.fill("并发普通重命名");
    await titleEditor.press("Tab");
    releaseConcurrentStrict();
    const [strictResponseValue, ordinaryRequestValue, ordinaryResponseValue] = await Promise.all([
      strictResponse,
      ordinaryRequest,
      ordinaryResponse,
    ]);
    assert.equal(strictResponseValue.status(), 200);
    assert.equal(ordinaryResponseValue.status(), 200);
    const strictSaved = await strictResponseValue.json();
    assert.equal(ordinaryRequestValue.postDataJSON().expected_updated_at, strictSaved.updated_at);
    assert.match(ordinaryRequestValue.postDataJSON().draft_json, /严格保存期间的新事故/);
    const storedAfterConcurrentSave = await request(chatSessionUrl, { headers });
    assert.equal(storedAfterConcurrentSave.title, "并发普通重命名");
    assert.match(storedAfterConcurrentSave.draft_json, /严格保存期间的新事故/);
    await fs.writeFile(path.join(output, "concurrent-save-evidence.json"), JSON.stringify({
      initial_updated_at: raceBaseline.updated_at,
      strict_expected_updated_at: heldStrict.payload.expected_updated_at,
      strict_response_status: strictResponseValue.status(),
      ordinary_response_status: ordinaryResponseValue.status(),
      ordinary_expected_updated_at: ordinaryRequestValue.postDataJSON().expected_updated_at,
      ordinary_payload_draft_json: ordinaryRequestValue.postDataJSON().draft_json,
      stored_title: storedAfterConcurrentSave.title,
      stored_draft_json: storedAfterConcurrentSave.draft_json,
    }, null, 2));
    results.push("严格保存期间并发普通更新保留，后继整记录PUT不覆盖新事故");
    await closeArchive(page);
  } finally {
    releaseConcurrentStrict?.();
    await page.unroute(browserSessionPattern);
  }

  const clientBDraft = secondaryPage.locator(".json-table-editor .value-input").first();
  await clientBDraft.fill("客户端B冲突");
  const clientBPutRequest = secondaryPage.waitForRequest((request) => request.url().endsWith(`/api/v1/chat-sessions/${bootstrap.session_id}`)
    && request.method() === "PUT");
  const clientBConflict = secondaryPage.waitForResponse((response) => response.url().endsWith(`/api/v1/chat-sessions/${bootstrap.session_id}`)
    && response.request().method() === "PUT");
  await clientBDraft.blur();
  const [clientBRequest, clientBConflictResponse] = await Promise.all([clientBPutRequest, clientBConflict]);
  assert.equal(clientBConflictResponse.status(), 409);
  assert.equal(clientBRequest.postDataJSON().expected_updated_at, clientAInitial.updated_at);
  assert.match(await clientBConflictResponse.text(), /SESSION_VERSION_CONFLICT/);
  assert.equal(await clientBDraft.inputValue(), "客户端B冲突");
  let failedClientRunPosts = 0;
  secondaryPage.on("request", (request) => {
    if (request.method() === "POST" && request.url().endsWith("/api/v1/report-runs")) {
      failedClientRunPosts += 1;
    }
  });
  const failedCreateRequest = secondaryPage.waitForRequest((request) => request.url().endsWith(`/api/v1/chat-sessions/${bootstrap.session_id}`)
    && request.method() === "PUT");
  const failedCreateSave = secondaryPage.waitForResponse((response) => response.url().endsWith(`/api/v1/chat-sessions/${bootstrap.session_id}`)
    && response.request().method() === "PUT");
  await secondaryPage.getByRole("button", { name: "创建证据报告运行", exact: true }).click();
  const [failedCreateRequestData, failedCreateResponse] = await Promise.all([failedCreateRequest, failedCreateSave]);
  assert.equal(failedCreateResponse.status(), 409);
  assert.equal(failedCreateRequestData.postDataJSON().expected_updated_at, clientAInitial.updated_at);
  await secondaryPage.waitForTimeout(300);
  assert.equal(failedClientRunPosts, 0);
  assert.equal(await clientBDraft.inputValue(), "客户端B冲突");
  await fs.writeFile(path.join(output, "cas-evidence.json"), JSON.stringify({
    initial: {
      client_a_updated_at: clientAInitial.updated_at,
      client_b_updated_at: clientBInitial.updated_at,
    },
    client_a: {
      request_expected_updated_at: clientAPayload.expected_updated_at,
      response_status: clientAResponse.status(),
      response_updated_at: clientASaved.updated_at,
    },
    client_b: {
      request_expected_updated_at: clientBRequest.postDataJSON().expected_updated_at,
      response_status: clientBConflictResponse.status(),
      response_code: "SESSION_VERSION_CONFLICT",
      run_post_count_after_conflict: failedClientRunPosts,
    },
  }, null, 2));
  results.push("真实双客户端同版本先成功后409，冲突编辑保留且不创建运行");
  await secondaryContext.close();
  secondaryContext = null;
  secondaryPage = null;

  await page.getByRole("button", { name: "添加证据", exact: true }).click();
  await page.getByLabel("证据内容", { exact: true }).fill("仅合成案例：双方对信号灯状态陈述不一致。");
  await page.getByLabel("来源标签", { exact: true }).fill("合成访谈");
  await page.getByLabel("来源定位", { exact: true }).fill("第1段");
  await page.getByLabel("核实状态", { exact: true }).selectOption("unverified");
  let saved = page.waitForResponse((response) => response.url().endsWith("/report-evidence")
    && response.request().method() === "PUT");
  await page.getByRole("button", { name: "保存补充证据", exact: true }).click();
  assert.equal((await saved).status(), 200);
  await page.reload();
  await enterHarnessMode(page);
  await waitUntil(async () => (await page.getByLabel("证据内容", { exact: true }).inputValue()).includes("合成案例"));
  results.push("证据保存与刷新保持");

  const evidenceUrl = `${api}/api/v1/chat-sessions/${bootstrap.session_id}/report-evidence`;
  const evidence = await request(evidenceUrl, { headers });
  await request(evidenceUrl, {
    method: "PUT", headers,
    body: JSON.stringify({ expected_revision: evidence.revision, records: evidence.records.map((record) => {
      const { recorded_by, updated_at, ...value } = record;
      return value;
    }) }),
  });
  await page.getByLabel("证据内容", { exact: true }).fill("本地未保存的冲突编辑");
  saved = page.waitForResponse((response) => response.url().endsWith("/report-evidence")
    && response.request().method() === "PUT");
  await page.getByRole("button", { name: "保存补充证据", exact: true }).click();
  assert.equal((await saved).status(), 409);
  assert.equal(await page.getByLabel("证据内容", { exact: true }).inputValue(), "本地未保存的冲突编辑");
  await page.getByRole("button", { name: "放弃本地编辑并重新读取", exact: true }).click();
  await waitUntil(async () => (await page.getByLabel("证据内容", { exact: true }).inputValue()).includes("合成案例"));
  results.push("证据CAS冲突不静默覆盖");

  const draftBeforeSwitch = page.locator(".json-table-editor .value-input").first();
  await draftBeforeSwitch.fill("切换前保存A");
  const switchSave = page.waitForResponse((response) => response.url().endsWith(`/api/v1/chat-sessions/${bootstrap.session_id}`)
    && response.request().method() === "PUT");
  await openArchive(page);
  await page.getByRole("button", { name: "新建事故档案", exact: true }).click();
  assert.equal((await switchSave).status(), 200);
  const newSessionId = await waitUntil(async () => {
    const ids = await page.locator("[data-session-id]").evaluateAll((nodes) => nodes.map((node) => node.getAttribute("data-session-id")));
    return ids.find((id) => id && id !== bootstrap.session_id);
  });
  assert(newSessionId);
  const savedAfterSwitch = await request(chatSessionUrl, { headers });
  assert(savedAfterSwitch.draft_json.includes("切换前保存A"));
  await openArchive(page);
  await page.locator(`[data-session-id="${bootstrap.session_id}"] .session-item`).click();
  await enterHarnessMode(page);
  await waitUntil(async () => (await page.locator(".json-table-editor .value-input").first().inputValue()) === "切换前保存A");
  results.push("聚焦编辑切换到新会话前先保存，返回原会话无串写");

  await page.getByRole("button", { name: "添加证据", exact: true }).click();
  const pendingEvidenceText = "草稿保存期间仍保留的未保存补证";
  await page.getByLabel("证据内容", { exact: true }).last().fill(pendingEvidenceText);
  await page.getByLabel("来源标签", { exact: true }).last().fill("合成访谈");
  await page.getByLabel("来源定位", { exact: true }).last().fill("第2段");
  const draftSaveWithEvidenceDirty = page.waitForResponse((response) => response.url().endsWith(`/api/v1/chat-sessions/${bootstrap.session_id}`)
    && response.request().method() === "PUT");
  await draftBeforeSwitch.fill("草稿保存不重置补证");
  await page.getByLabel("证据内容", { exact: true }).last().click();
  assert.equal((await draftSaveWithEvidenceDirty).status(), 200);
  const evidenceSection = page.locator(".harness-section").filter({ hasText: "补充证据" }).first();
  assert.equal(await page.getByLabel("证据内容", { exact: true }).last().inputValue(), pendingEvidenceText);
  assert((await evidenceSection.locator(".harness-status-pill").innerText()).includes("未保存"));
  results.push("事故草稿保存不会重置补证编辑或dirty状态");
  const finalEvidenceSave = page.waitForResponse((response) => response.url().endsWith("/report-evidence")
    && response.request().method() === "PUT");
  await page.getByRole("button", { name: "保存补充证据", exact: true }).click();
  const finalEvidenceResponse = await finalEvidenceSave;
  if (finalEvidenceResponse.status() !== 200) {
    throw new Error(`补充证据保存失败 ${finalEvidenceResponse.status()}: ${await finalEvidenceResponse.text()}`);
  }

  let createdResponse = page.waitForResponse((response) => response.url().endsWith("/api/v1/report-runs")
    && response.request().method() === "POST");
  await page.getByRole("button", { name: "创建证据报告运行", exact: true }).click();
  const created = await (await createdResponse).json();
  await page.getByRole("button", { name: "显式执行", exact: true }).click();
  await waitUntil(async () => (await request(`${api}/api/v1/report-runs/${created.run_id}`, { headers })).state === "published");
  await page.getByRole("button", { name: "刷新状态", exact: true }).click();
  await waitUntil(async () => await page.getByRole("button", { name: "工程样本", exact: true }).first().isEnabled());
  for (const button of await page.getByRole("button", { name: "正式导出", exact: true }).all()) {
    assert(await button.isDisabled());
  }
  const downloadEvent = page.waitForEvent("download");
  await page.getByRole("button", { name: "工程样本", exact: true }).first().click();
  const download = await downloadEvent;
  assert(download.suggestedFilename().includes("engineering"));
  const downloadPath = path.join(output, download.suggestedFilename());
  await download.saveAs(downloadPath);
  assert((await fs.readFile(downloadPath, "utf8")).includes("工程验证样本，不具正式导出资格"));
  results.push("独立审查后发布与工程样本下载，正式导出禁用");
  const savedEvents = [];
  let sequence = 0;
  while (true) {
    const eventPage = await request(`${api}/api/v1/report-runs/${created.run_id}/events?after_seq=${sequence}`, { headers });
    savedEvents.push(...eventPage.events);
    if (!eventPage.events.length) break;
    sequence = eventPage.next_seq;
  }
  await fs.writeFile(path.join(output, "events.json"), JSON.stringify(savedEvents, null, 2));

  createdResponse = page.waitForResponse((response) => response.url().endsWith("/api/v1/report-runs")
    && response.request().method() === "POST");
  await page.getByRole("button", { name: "创建证据报告运行", exact: true }).click();
  const cancelTarget = await (await createdResponse).json();
  await page.getByRole("button", { name: "显式执行", exact: true }).click();
  await waitUntil(async () => ["preparing", "generating", "checking"].includes(
    (await request(`${api}/api/v1/report-runs/${cancelTarget.run_id}`, { headers })).state,
  ));
  await page.getByRole("button", { name: "取消运行", exact: true }).click();
  await waitUntil(async () => (await request(`${api}/api/v1/report-runs/${cancelTarget.run_id}`, { headers })).state === "cancelled");
  await page.reload();
  await waitUntil(async () => (await page.locator(`[data-session-id="${bootstrap.session_id}"] .session-item`).count()) === 1);
  const reloadedSessionRefresh = page.waitForResponse((response) => response.url().endsWith(`/api/v1/chat-sessions/${bootstrap.session_id}`)
    && response.request().method() === "GET");
  await openArchive(page);
  await page.locator(`[data-session-id="${bootstrap.session_id}"] .session-item`).click();
  assert.equal((await reloadedSessionRefresh).status(), 200);
  await waitUntil(async () => (await page.locator(`[data-session-id="${bootstrap.session_id}"] .session-item.active`).count()) === 1);
  await enterHarnessMode(page);
  await waitUntil(async () => (await page.locator(".harness-run-list-item").filter({ hasText: cancelTarget.run_id }).count()) === 1);
  await page.locator(".harness-run-list-item").filter({ hasText: cancelTarget.run_id }).click();
  const afterReload = await request(`${api}/api/v1/report-runs/${cancelTarget.run_id}`, { headers });
  assert.equal(afterReload.state, "cancelled");
  assert(await page.getByRole("button", { name: "显式执行", exact: true }).isDisabled());
  assert.equal(await page.getByRole("button", { name: "显式恢复", exact: true }).count(), 0);
  results.push("真实控制流显式取消，刷新不复活终态");

  createdResponse = page.waitForResponse((response) => response.url().endsWith("/api/v1/report-runs")
    && response.request().method() === "POST");
  await page.getByRole("button", { name: "创建证据报告运行", exact: true }).click();
  const queued = await (await createdResponse).json();
  await page.reload();
  await waitUntil(async () => (await page.locator(`[data-session-id="${bootstrap.session_id}"] .session-item`).count()) === 1);
  const queuedSessionRefresh = page.waitForResponse((response) => response.url().endsWith(`/api/v1/chat-sessions/${bootstrap.session_id}`)
    && response.request().method() === "GET");
  await openArchive(page);
  await page.locator(`[data-session-id="${bootstrap.session_id}"] .session-item`).click();
  assert.equal((await queuedSessionRefresh).status(), 200);
  await waitUntil(async () => (await page.locator(`[data-session-id="${bootstrap.session_id}"] .session-item.active`).count()) === 1);
  await enterHarnessMode(page);
  await waitUntil(async () => (await page.locator(".harness-run-list-item").filter({ hasText: queued.run_id }).count()) === 1);
  await page.locator(".harness-run-list-item").filter({ hasText: queued.run_id }).click();
  const queuedAfterReload = await request(`${api}/api/v1/report-runs/${queued.run_id}`, { headers });
  assert.equal(queuedAfterReload.state, "queued");
  assert.equal(queuedAfterReload.budget.physical_requests, 0);
  await page.getByRole("button", { name: "取消运行", exact: true }).click();
  await waitUntil(async () => (await request(`${api}/api/v1/report-runs/${queued.run_id}`, { headers })).state === "cancelled");
  results.push("刷新只读，待执行运行零请求");
  const controlHeaders = { "X-Harness-Control": bootstrap.control_token };
  const unknown = await request(`${api}/__harness_test__/scenario/unknown`, {
    method: "POST", headers: controlHeaders,
  });
  await page.getByRole("button", { name: "刷新只读数据", exact: true }).click();
  await page.locator(".harness-run-list-item").filter({ hasText: unknown.run_id }).click();
  let resumedResponse = page.waitForResponse((response) => response.url().endsWith("/resume/stream"));
  await page.getByRole("button", { name: "显式恢复", exact: true }).click();
  assert.equal((await resumedResponse).status(), 409);
  let unknownView = await request(`${api}/api/v1/report-runs/${unknown.run_id}`, { headers });
  assert.equal(unknownView.state, "suspended");
  assert.equal(unknownView.budget.unknown_reserved, 100);
  await page.getByRole("checkbox", { name: /允许恢复时重试未知请求/ }).check();
  resumedResponse = page.waitForResponse((response) => response.url().endsWith("/resume/stream"));
  await page.getByRole("button", { name: "显式恢复", exact: true }).click();
  const resumed = await resumedResponse;
  assert.equal(resumed.status(), 200);
  assert.equal(resumed.request().postDataJSON().retry_unknown_requests, true);
  await waitUntil(async () => (await request(`${api}/api/v1/report-runs/${unknown.run_id}`, { headers })).state === "published");
  unknownView = await request(`${api}/api/v1/report-runs/${unknown.run_id}`, { headers });
  assert.equal(unknownView.budget.unknown_reserved, 100);
  results.push("未知请求默认拒绝与显式恢复");

  const historical = await request(`${api}/__harness_test__/scenario/historical`, {
    method: "POST", headers: controlHeaders,
  });
  await page.getByRole("button", { name: "刷新只读数据", exact: true }).click();
  await page.locator(".harness-run-list-item").filter({ hasText: historical.run_id }).click();
  await waitUntil(async () => page.getByRole("button", { name: "正式导出", exact: true }).first().isEnabled());
  const historicalDownloadEvent = page.waitForEvent("download");
  await page.getByRole("button", { name: "正式导出", exact: true }).first().click();
  const historicalDownload = await historicalDownloadEvent;
  assert(historicalDownload.suggestedFilename().includes("engineering"));
  await request(`${api}/__harness_test__/scenario/revoke`, { method: "POST", headers: controlHeaders });
  const revokedResponse = page.waitForResponse((response) => response.url().includes(`/report-runs/${historical.run_id}/exports/md`));
  await page.getByRole("button", { name: "正式导出", exact: true }).first().click();
  assert.equal((await revokedResponse).status(), 409);
  await waitUntil(async () => page.getByRole("button", { name: "正式导出", exact: true }).first().isDisabled());
  assert.equal((await request(`${api}/api/v1/report-runs/${historical.run_id}`, { headers })).state, "published");
  results.push("历史批准撤销阻断正式下载");

  await page.locator(".harness-run-list-item").filter({ hasText: created.run_id }).click();
  await waitUntil(async () => (await page.locator(".harness-detail-section .harness-run-id").innerText()) === created.run_id);
  await waitUntil(async () => await page.getByRole("button", { name: "工程样本", exact: true }).first().isEnabled());

  for (const viewport of [{ width: 1440, height: 900 }, { width: 390, height: 844 }]) {
    await page.setViewportSize(viewport);
    await page.waitForFunction(() => {
      const sidebar = document.querySelector(".sidebar:not(.mobile-open)");
      return innerWidth > 768 || !sidebar || sidebar.getBoundingClientRect().right <= 1;
    });
    await page.locator(".report-harness-workspace").evaluate((node) => { node.scrollTop = 0; });
    if (viewport.width === 390) {
      await page.getByLabel("证据内容", { exact: true }).first().fill("移动端合成补充，不含真实资料。");
      const mobileSaved = page.waitForResponse((response) => response.url().endsWith("/report-evidence")
        && response.request().method() === "PUT");
      await page.getByRole("button", { name: "保存补充证据", exact: true }).click();
      assert.equal((await mobileSaved).status(), 200);
      await page.locator(".harness-run-list-item").filter({ hasText: created.run_id }).click();
      await waitUntil(async () => (await page.locator(".harness-detail-section .harness-run-id").innerText()) === created.run_id);
      await page.getByRole("button", { name: "刷新状态", exact: true }).click();
      assert(await page.getByRole("button", { name: "正式导出", exact: true }).first().isDisabled());
      results.push("移动端证据编辑与操作");
      await page.locator(".report-harness-workspace").evaluate((node) => { node.scrollTop = 0; });
    }
    const layout = await page.evaluate(() => ({
      viewport: innerWidth, width: document.documentElement.scrollWidth,
      overflowing: [...document.querySelectorAll(".report-harness-panel button,.report-harness-panel input,.report-harness-panel select")]
        .filter((node) => {
          const box = node.getBoundingClientRect();
          return box.width && (box.left < -1 || box.right > innerWidth + 1);
        }).map((node) => ({
          text: node.textContent?.slice(0, 60),
          ancestors: [node, node.parentElement, node.parentElement?.parentElement,
            node.closest(".harness-section")].filter(Boolean).map((element) => ({
            class: element.className, box: element.getBoundingClientRect().toJSON(),
            clientWidth: element.clientWidth, scrollWidth: element.scrollWidth,
            display: getComputedStyle(element).display,
          })),
        })),
    }));
    await fs.writeFile(path.join(output, `layout-${viewport.width}.json`), JSON.stringify(layout, null, 2));
    assert(layout.width <= layout.viewport + 1, JSON.stringify(layout));
    assert.equal(layout.overflowing.length, 0, `控件越界，详见 layout-${viewport.width}.json`);
    await page.screenshot({ path: path.join(output, `harness-${viewport.width}.png`), fullPage: true });
    await page.locator(".harness-detail-section").scrollIntoViewIfNeeded();
    await page.screenshot({ path: path.join(output, `harness-detail-${viewport.width}.png`), fullPage: true });
  }
  assert.deepEqual(pageErrors, []);
  results.push("1440×900与390×844布局、无页面脚本异常");
  await fs.writeFile(path.join(output, "results.json"), JSON.stringify({ results }, null, 2));
  console.log(JSON.stringify({ passed: results.length, results, output }));
  }
} catch (error) {
  await fs.writeFile(path.join(output, "cas-trace.json"), JSON.stringify(casTrace, null, 2)).catch(() => {});
  if (page) {
    await page.screenshot({ path: path.join(output, "failure.png"), fullPage: true }).catch(() => {});
    await fs.writeFile(path.join(output, "failure.txt"), await page.locator("body").innerText()).catch(() => {});
  }
  throw error;
} finally {
  if (browser) await browser.close();
  if (bootstrap?.control_token) {
    await fetch(`${api}/__harness_test__/shutdown`, {
      method: "POST", headers: { "Content-Type": "application/json",
        "X-Harness-Control": bootstrap.control_token },
      body: JSON.stringify({ control_token: bootstrap.control_token }),
      signal: AbortSignal.timeout(5000),
    }).catch(() => {});
  }
  const cleanupErrors = [];
  for (const tracked of children) {
    try {
      const status = await stopChild(tracked, tracked === children[0] ? 5000 : 0);
      if (tracked === children[0] && bootstrap && !cleanupSucceeded(status)) {
        cleanupErrors.push("隔离后端未正常退出，不能确认资源清理");
      }
      if (tracked !== children[0] && !cleanupSucceeded(status, true)) {
        cleanupErrors.push("前端服务异常退出，不能确认浏览器验收");
      }
    } catch (error) {
      cleanupErrors.push(error.message);
    }
  }
  await fs.writeFile(path.join(output, "service.log"), logs.join(""));
  assert.deepEqual(cleanupErrors, []);
}
