import assert from "node:assert/strict";
import { spawn } from "node:child_process";
import { createServer } from "node:net";
import fs from "node:fs/promises";
import path from "node:path";
import os from "node:os";
import { fileURLToPath } from "node:url";
import { chromium } from "playwright";
import { cleanupSucceeded, stopChild, trackChild } from "./process-cleanup.mjs";

const frontend = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const root = path.dirname(frontend);
const apiPort = Number(process.env.HARNESS_TEST_API_PORT || "18081");
assert(Number.isInteger(apiPort) && apiPort >= 1024 && apiPort <= 65535);
const api = `http://127.0.0.1:${apiPort}`;
const ui = "http://127.0.0.1:15174";
const output = process.env.REPORT_HARNESS_E2E_OUTPUT
  || await fs.mkdtemp(path.join(os.tmpdir(), "safetyraise-harness-browser-"));
await fs.mkdir(output, { recursive: true });
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
let bootstrap;
const results = [];
try {
  await requireFreePort(apiPort);
  await requireFreePort(15174);
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
    "--host", "127.0.0.1", "--port", "15174", "--strictPort",
    "--config", path.join(frontend, "vite.config.ts"), frontend,
  ], { BACKEND_PROXY_TARGET: api });
  await waitUntil(async () => (await fetch(ui)).ok);
  browser = await chromium.launch({ headless: true });
  const context = await browser.newContext({ viewport: { width: 1440, height: 900 } });
  await context.addInitScript((token) => {
    localStorage.setItem("traffic-accident-auth-token", token);
  }, bootstrap.token);
  page = await context.newPage();
  await page.addLocatorHandler(
    page.getByRole("button", { name: "关闭抽屉", exact: true }),
    async (locator) => locator.click(),
  );
  const pageErrors = [];
  page.on("pageerror", (error) => pageErrors.push(error.message));
  await page.goto(ui);
  await page.locator(`[data-session-id="${bootstrap.session_id}"] .session-item`).click();
  await page.getByRole("tab", { name: "证据报告", exact: true }).click();
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
  await page.getByRole("tab", { name: "证据报告", exact: true }).click();
  await waitUntil(async () => (await page.getByLabel("证据内容", { exact: true }).inputValue()).includes("合成案例"));
  results.push("证据保存与刷新保持");

  const headers = { Authorization: `Bearer ${bootstrap.token}`, "Content-Type": "application/json" };
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
  await page.getByRole("tab", { name: "证据报告", exact: true }).click();
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
  await page.getByRole("tab", { name: "证据报告", exact: true }).click();
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
      await page.getByLabel("证据内容", { exact: true }).fill("移动端合成补充，不含真实资料。");
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
} catch (error) {
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
