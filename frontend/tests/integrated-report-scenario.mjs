import assert from "node:assert/strict";
import path from "node:path";
import { readFile } from "node:fs/promises";

// 真实浏览器、HTTP 和独立数据库；模型仅合成角色，不证明真实业务报告质量。
export async function verifyIntegratedReport({ page, bootstrap, api, output, results, waitUntil, request }) {
  const headers = { Authorization: `Bearer ${bootstrap.token}`, "Content-Type": "application/json" };
  const sessionPath = `/api/v1/chat-sessions/${bootstrap.session_id}`;
  const evidenceUrl = `${api}${sessionPath}/report-evidence`;
  const panel = page.getByRole("region", { name: "事故分析报告" });
  const materialsHost = page.locator(".materials-stage-host");
  const factsHost = page.locator(".facts-stage-host");
  const reportHost = page.locator(".report-stage-host");

  async function openArchive() {
    const drawer = page.getByRole("dialog", { name: "档案导航" });
    if (!(await drawer.isVisible())) {
      await page.getByRole("button", { name: "打开档案列表", exact: true }).click();
    }
    await drawer.waitFor({ state: "visible" });
    return drawer;
  }

  async function selectSession(sessionId) {
    await openArchive();
    const sessionButton = page.locator(`[data-session-id="${sessionId}"] button.session-title`);
    await sessionButton.waitFor();
    await sessionButton.click();
    await waitUntil(async () => (await sessionButton.getAttribute("aria-current")) === "true");
  }

  async function selectStage(label) {
    const stageButton = page.locator(".workspace-stages button").filter({ hasText: label }).first();
    await stageButton.click();
    await waitUntil(async () => (await stageButton.getAttribute("aria-current")) === "step");
  }

  async function waitForReportStatus(label, timeout = 45000) {
    await selectStage("查看报告");
    await panel.waitFor({ state: "visible" });
    await waitUntil(async () => (await panel.locator(".report-status").innerText()) === label, timeout);
  }

  async function openEvidenceSection() {
    const section = panel.locator(".report-evidence-section");
    await section.waitFor({ state: "visible" });
    if (!(await section.evaluate((node) => node.open))) {
      await section.locator("summary").click();
    }
  }

  async function assertStageLayout(selector, label) {
    const layout = await page.locator(selector).evaluate((node) => {
      const isVisible = (element) => {
        if (element.closest("[hidden]") || element.getAttribute("aria-hidden") === "true") return false;
        const style = getComputedStyle(element);
        const box = element.getBoundingClientRect();
        return style.display !== "none" && style.visibility !== "hidden" && box.width > 0 && box.height > 0
          && (typeof element.checkVisibility !== "function" || element.checkVisibility());
      };
      const isInHorizontalScroller = (element) => {
        for (let parent = element.parentElement; parent && parent !== node; parent = parent.parentElement) {
          const style = getComputedStyle(parent);
          if ((style.overflowX === "auto" || style.overflowX === "scroll")
            && parent.scrollWidth > parent.clientWidth + 1) {
            return true;
          }
        }
        return false;
      };
      const hostBox = node.getBoundingClientRect();
      const controls = [...node.querySelectorAll("button,input,textarea,select")]
        .filter((element) => isVisible(element) && !isInHorizontalScroller(element));
      // 复选框与单选框的可点区域是包裹它的标签，按标签计量尺寸与遮挡。
      const hitTarget = (element) => (element.matches('input[type="checkbox"], input[type="radio"]')
        && element.closest("label")) || element;
      const boxes = controls.map((element) => ({
        element,
        box: hitTarget(element).getBoundingClientRect(),
        label: element.getAttribute("aria-label") || element.textContent?.trim().slice(0, 40) || element.tagName,
      }));
      const outside = boxes.filter(({ box }) => box.left < -1 || box.right > innerWidth + 1)
        .map(({ label, box }) => ({ label, left: box.left, right: box.right, top: box.top }));
      const overlaps = [];
      for (let leftIndex = 0; leftIndex < boxes.length; leftIndex += 1) {
        for (let rightIndex = leftIndex + 1; rightIndex < boxes.length; rightIndex += 1) {
          const left = boxes[leftIndex].box;
          const right = boxes[rightIndex].box;
          const width = Math.min(left.right, right.right) - Math.max(left.left, right.left);
          const height = Math.min(left.bottom, right.bottom) - Math.max(left.top, right.top);
          if (width > 2 && height > 2) {
            overlaps.push([boxes[leftIndex].label, boxes[rightIndex].label]);
          }
        }
      }
      return {
        documentOverflow: document.documentElement.scrollWidth > innerWidth + 1,
        hostVisible: !node.closest("[hidden]") && hostBox.width > 0 && hostBox.height > 0,
        hostWithinViewport: hostBox.left >= -1 && hostBox.right <= innerWidth + 1,
        outside,
        overlaps,
        minimumControlHeight: boxes.length > 0 ? Math.min(...boxes.map(({ box }) => box.height)) : 0,
      };
    });
    assert(layout.hostVisible, `${label}阶段必须可见：${JSON.stringify(layout)}`);
    assert(layout.hostWithinViewport, `${label}阶段容器不能越出视口：${JSON.stringify(layout)}`);
    assert.equal(layout.documentOverflow, false, `${label}阶段不能产生横向溢出：${JSON.stringify(layout)}`);
    assert.deepEqual(layout.outside, [], `${label}阶段控件不能越出视口：${JSON.stringify(layout)}`);
    assert.deepEqual(layout.overlaps, [], `${label}阶段控件不能互相遮挡：${JSON.stringify(layout)}`);
    if (layout.minimumControlHeight > 0) {
      assert(layout.minimumControlHeight >= 28, `${label}阶段控件尺寸过小：${JSON.stringify(layout)}`);
    }
  }

  async function assertStableLayout(viewport, screenshotName) {
    await page.waitForFunction(() => !document.getAnimations().some((animation) =>
      animation.playState === "running" && animation.effect?.getTiming().iterations !== Infinity));
    await assertStageLayout(".facts-stage-host", "核对事实");
    await selectStage("查看报告");
    await assertStageLayout(".report-stage-host", "查看报告");
    await page.screenshot({ path: path.join(output, screenshotName), fullPage: true });
    results.push(`${viewport.width}px 阶段布局无横向溢出且关键控件不重叠`);
  }

  await page.getByRole("navigation", { name: "事故处理阶段" }).waitFor();
  assert.equal(await page.getByRole("tab", { name: "证据报告", exact: true }).count(), 0);
  assert.equal(await page.getByRole("button", { name: "分析与审阅", exact: true }).count(), 0);
  assert.equal(await page.getByRole("button", { name: "上传事故资料", exact: true }).count(), 0);
  assert.equal(await page.getByRole("button", { name: "退出满屏", exact: true }).count(), 0);
  assert.equal(await page.getByRole("button", { name: "切换为深色模式", exact: true }).count(), 0);

  await openArchive();
  const sessionButton = page.locator(`[data-session-id="${bootstrap.session_id}"] button.session-title`);
  await sessionButton.focus();
  await sessionButton.press("Enter");
  await waitUntil(async () => (await sessionButton.getAttribute("aria-current")) === "true");
  assert.equal(await sessionButton.getAttribute("aria-current"), "true");
  await waitUntil(async () => (await page.locator(".integrated-report").count()) === 1);
  assert.equal(await page.locator(".integrated-report").count(), 1);
  results.push("档案抽屉内会话按钮支持原生键盘进入，并标识当前会话");

  await selectStage("核对事实");
  assert.equal(await factsHost.isVisible(), true);
  assert.equal(await reportHost.isVisible(), false);
  assert.equal(await panel.isVisible(), false);
  await assertStageLayout(".facts-stage-host", "核对事实");

  await selectStage("查看报告");
  await panel.waitFor({ state: "visible" });
  await openEvidenceSection();
  await panel.getByLabel("材料来源", { exact: true }).fill("合成现场记录");
  await panel.getByLabel("页码或位置", { exact: true }).fill("第1页");
  await panel.getByLabel("补充内容", { exact: true }).fill("仅工程验证：道路干燥。");
  await panel.getByLabel("已人工核实", { exact: true }).check();
  await panel.getByRole("button", { name: "保存补充材料", exact: true }).click();
  await waitUntil(async () => (await request(evidenceUrl, { headers })).records.length === 1);
  await panel.getByRole("button", { name: "编辑", exact: true }).click();
  await panel.getByLabel("补充内容", { exact: true }).fill("仅工程验证：道路湿滑。");
  await panel.getByLabel("已人工核实", { exact: true }).uncheck();
  await panel.getByRole("button", { name: "保存补充材料", exact: true }).click();
  await waitUntil(async () => {
    const value = await request(evidenceUrl, { headers });
    return value.records[0]?.text.includes("湿滑") && value.records[0]?.verification_status === "unverified";
  });
  await panel.getByRole("button", { name: "删除", exact: true }).click();
  await waitUntil(async () => (await request(evidenceUrl, { headers })).records.length === 0);
  results.push("查看报告阶段补充材料新增、修改核实状态、删除均经真实HTTP持久化");

  await selectStage("核对事实");
  const editor = factsHost.locator(".json-table-editor .value-input").first();
  await editor.fill("合成案例一：新三阶段确认后生成。");
  await editor.blur();
  const confirm = factsHost.getByRole("button", { name: "确认事故信息并生成报告", exact: true });
  await confirm.click();
  await waitForReportStatus("报告已完成");
  let runs = await request(`${api}/api/v1/report-runs?session_id=${bootstrap.session_id}`, { headers });
  assert.equal(runs.runs.length, 1);
  const firstId = runs.runs[0].run_id;
  const saved = await request(`${api}${sessionPath}`, { headers });
  assert(saved.draft_json.includes("合成案例一"));
  assert.equal(runs.runs[0].review_status, "passed");
  assert.equal(runs.runs[0].formal_export_eligible, false);
  // 工程样本可下载，但必须附“未经质量验收”说明，不能呈现为正式导出。
  await panel.getByRole("button", { name: "下载Word" }).waitFor();
  assert.equal(await panel.getByText("演示样本：报告未经质量验收", { exact: false }).count(), 1);
  results.push("核对事实阶段确认后完成真实保存、创建、授权、合成生成和独立审查，工程样本带标记下载，未冒充正式导出");

  await selectStage("核对事实");
  await editor.fill("合成案例二：验证新阶段历史切换。");
  await editor.blur();
  await confirm.click();
  await waitUntil(async () => {
    runs = await request(`${api}/api/v1/report-runs?session_id=${bootstrap.session_id}`, { headers });
    return runs.runs.length === 2 && runs.runs[0].state === "published";
  }, 45000);
  const secondId = runs.runs[0].run_id;
  await waitForReportStatus("报告已完成");
  await waitUntil(async () => !(await panel.getByLabel("历史报告").isDisabled()));
  await panel.getByLabel("历史报告").selectOption(firstId);
  await waitUntil(async () => (await panel.getByLabel("历史报告").inputValue()) === firstId);
  await panel.getByLabel("历史报告").selectOption(secondId);
  await page.reload();
  await selectSession(bootstrap.session_id);
  await waitForReportStatus("报告已完成");
  assert.equal(await panel.getByLabel("历史报告").inputValue(), secondId);
  results.push("历史报告可在查看报告阶段切换，刷新档案后恢复最新报告");

  for (const viewport of [
    { width: 1440, height: 900 }, { width: 1024, height: 900 },
    { width: 768, height: 1024 }, { width: 390, height: 844 }, { width: 320, height: 720 },
  ]) {
    await page.setViewportSize(viewport);
    await selectStage("核对事实");
    await assertStableLayout(viewport, `integrated-${viewport.width}.png`);
    await panel.locator(".report-evidence-section summary").click();
    await assertStageLayout(".report-stage-host", "查看报告补证展开");
    await panel.locator(".report-evidence-section summary").click();
  }

  async function startReportRun(value) {
    await selectStage("核对事实");
    const currentEditor = factsHost.locator(".json-table-editor .value-input").first();
    await currentEditor.fill(value);
    await currentEditor.blur();
    await factsHost.getByRole("button", { name: "确认事故信息并生成报告", exact: true }).click();
  }

  let releaseFactsCancel;
  let factsCancelRequests = 0;
  const factsCancelBarrier = new Promise((resolve) => { releaseFactsCancel = resolve; });
  await page.route("**/api/v1/report-runs/*/cancel", async (route) => {
    factsCancelRequests += 1;
    await factsCancelBarrier;
    await route.continue();
  });
  try {
    await startReportRun("合成案例三：在核对事实阶段停止。");
    await selectStage("核对事实");
    const factsStop = factsHost.locator(".report-stop-btn");
    await waitUntil(async () => (await factsStop.isVisible()) && !(await factsStop.isDisabled()));
    assert.equal(await panel.isVisible(), false);
    await factsStop.click();
    await waitUntil(async () => (await factsStop.isDisabled()) && (await factsStop.innerText()) === "正在停止");
    assert.equal(factsCancelRequests, 1);
    await selectStage("查看报告");
    assert.equal(await factsStop.isVisible(), false);
    releaseFactsCancel();
    await waitUntil(async () => (await panel.locator(".report-status").innerText()) === "已停止");
  } finally {
    releaseFactsCancel();
    await page.unroute("**/api/v1/report-runs/*/cancel");
  }
  runs = await request(`${api}/api/v1/report-runs?session_id=${bootstrap.session_id}`, { headers });
  assert.equal(runs.runs[0].state, "cancelled");
  results.push("核对事实阶段停止按钮单独可用，报告阶段隐藏时仍以真实HTTP取消并保留数据库状态");

  let releaseReportCancel;
  let reportCancelRequests = 0;
  const reportCancelBarrier = new Promise((resolve) => { releaseReportCancel = resolve; });
  await page.route("**/api/v1/report-runs/*/cancel", async (route) => {
    reportCancelRequests += 1;
    await reportCancelBarrier;
    await route.continue();
  });
  try {
    await startReportRun("合成案例四：在查看报告阶段停止。");
    await selectStage("查看报告");
    const reportStop = panel.locator(".integrated-report-toolbar button.report-danger");
    await waitUntil(async () => (await reportStop.isVisible()) && !(await reportStop.isDisabled()));
    assert.equal(await factsHost.isVisible(), false);
    await reportStop.click();
    await waitUntil(async () => (await reportStop.isDisabled()) && (await reportStop.innerText()) === "正在停止");
    assert.equal(reportCancelRequests, 1);
    releaseReportCancel();
    await waitUntil(async () => (await panel.locator(".report-status").innerText()) === "已停止");
  } finally {
    releaseReportCancel();
    await page.unroute("**/api/v1/report-runs/*/cancel");
  }
  runs = await request(`${api}/api/v1/report-runs?session_id=${bootstrap.session_id}`, { headers });
  assert.equal(runs.runs[0].state, "cancelled");
  results.push("查看报告阶段停止按钮单独可用，核对事实阶段隐藏时按钮不重叠且真实取消落库");

  const deniedControl = await fetch(`${api}/__harness_test__/scenario/unknown`, { method: "POST" });
  assert.equal(deniedControl.status, 403);
  results.push("未携带测试控制令牌的未知场景请求真实返回403");

  const unknown = await request(`${api}/__harness_test__/scenario/unknown`, {
    method: "POST", headers: { "X-Harness-Control": bootstrap.control_token },
  });
  await page.reload();
  await selectSession(bootstrap.session_id);
  await waitForReportStatus("生成已暂停");
  assert(await panel.getByRole("button", { name: "继续生成", exact: true }).isDisabled());
  const suspended = await request(`${api}/api/v1/report-runs/${unknown.run_id}`, { headers });
  assert.equal(suspended.budget.unknown_requests, 1);
  await panel.getByLabel("确认重试未收到结果的请求，可能重复计费").check();
  await panel.getByLabel("历史报告").selectOption(firstId);
  await waitUntil(async () => (await panel.locator(".report-status").innerText()) === "报告已完成");
  await panel.getByLabel("历史报告").selectOption(unknown.run_id);
  await waitUntil(async () => (await panel.locator(".report-status").innerText()) === "生成已暂停");
  assert.equal(await panel.getByLabel("确认重试未收到结果的请求，可能重复计费").isChecked(), false);
  assert(await panel.getByRole("button", { name: "继续生成", exact: true }).isDisabled());
  await panel.getByLabel("确认重试未收到结果的请求，可能重复计费").check();
  await panel.getByRole("button", { name: "继续生成", exact: true }).click();
  await waitUntil(async () => (await panel.locator(".report-status").innerText()) === "报告已完成", 45000);
  results.push("未知请求刷新不自动重试，历史切换会清除确认状态，用户显式确认后才恢复");

  await request(`${api}/__harness_test__/scenario/unknown`, {
    method: "POST", headers: { "X-Harness-Control": bootstrap.control_token },
  });
  await page.reload();
  await selectSession(bootstrap.session_id);
  await waitForReportStatus("生成已暂停");
  assert.equal(await panel.getByLabel("确认重试未收到结果的请求，可能重复计费").isChecked(), false);
  assert(await panel.getByRole("button", { name: "继续生成", exact: true }).isDisabled());
  results.push("上一次未知请求的重复计费确认不沿用到新任务");

  let legacyRequests = 0;
  const observe = (incoming) => {
    if (incoming.url().includes("/reports/generate")) legacyRequests += 1;
  };
  page.on("request", observe);
  await page.route("**/api/v1/app-config", (route) => route.fulfill({
    status: 503, contentType: "application/json", body: '{"error":"unavailable"}',
  }));
  try {
    await page.reload();
    await selectSession(bootstrap.session_id);
    await page.getByText("读取报告服务配置失败，暂不能生成报告，请刷新页面重试。", { exact: true }).waitFor();
    await selectStage("核对事实");
    assert(await factsHost.getByRole("button", { name: /确认.*生成/ }).first().isDisabled());
    assert.equal(legacyRequests, 0);
  } finally {
    page.off("request", observe);
    await page.unroute("**/api/v1/app-config");
  }
  results.push("公开配置失败时禁止确认生成，不回退旧报告接口");

  const template = JSON.parse(await readFile(
    new URL("../../backend/config/input_accident_template.json", import.meta.url), "utf8"));
  const stressData = Object.fromEntries(Object.keys(template).map((key, index) => [
    key, index % 2 ? "仅合成布局验证，含较长描述；不代表实际事故事实。".repeat(6) : "合成待核",
  ]));
  const currentSession = await request(`${api}${sessionPath}`, { headers });
  await request(`${api}${sessionPath}`, {
    method: "PUT", headers, body: JSON.stringify({
      expected_updated_at: currentSession.updated_at, draft_json: JSON.stringify(stressData),
    }),
  });
  await page.reload();
  await selectSession(bootstrap.session_id);
  await selectStage("核对事实");
  await waitUntil(async () => await factsHost.locator(".json-table-editor .value-input").count() === 37);
  for (const width of [1440, 320]) {
    await page.setViewportSize({ width, height: 900 });
    await selectStage("核对事实");
    await page.waitForFunction(() => !document.getAnimations().some((animation) =>
      animation.playState === "running" && animation.effect?.getTiming().iterations !== Infinity));
    const fields = await factsHost.locator(".json-table-editor").evaluate((table) =>
      [...table.querySelectorAll(".value-input")].map((input) => {
        const box = input.getBoundingClientRect();
        const cell = input.closest("td").getBoundingClientRect();
        return {
          named: Boolean(input.getAttribute("aria-label")),
          readable: input.scrollHeight <= input.clientHeight + 1 && input.scrollWidth <= input.clientWidth + 1,
          contained: box.left >= cell.left && box.right <= cell.right
            && box.left >= 0 && box.right <= innerWidth,
        };
      }));
    assert(fields.every((field) => field.named && field.contained && field.readable), JSON.stringify(fields));
    await assertStageLayout(".facts-stage-host", "长文本核对事实");
    await factsHost.scrollIntoViewIfNeeded();
    await page.screenshot({ path: path.join(output, `fields-37-${width}.png`), fullPage: true });
  }
  results.push("原模板37字段配合合成长文本在1440/320完整换行可读、不越单元格且保留字段可访问名称");

  await page.setViewportSize({ width: 1440, height: 900 });
  const archive = await openArchive();
  await archive.getByRole("button", { name: "新建事故档案", exact: true }).click();
  const newSessionId = await waitUntil(async () => {
    await openArchive();
    const active = page.locator("[data-session-id]").filter({
      has: page.locator("button.session-title[aria-current='true']"),
    });
    // 新档案创建并切换完成前，当前项仍是原会话；等到当前项换成新会话再继续。
    const current = (await active.count()) > 0 ? await active.first().getAttribute("data-session-id") : null;
    return current && current !== bootstrap.session_id ? current : null;
  });
  assert(newSessionId && newSessionId !== bootstrap.session_id);
  await page.keyboard.press("Escape");
  await selectStage("整理资料");
  await page.getByText("从事故资料开始", { exact: true }).waitFor();
  for (const width of [1440, 390]) {
    await page.setViewportSize({ width, height: 900 });
    await selectStage("整理资料");
    await assertStageLayout(".materials-stage-host", "整理资料空态");
    assert(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth));
    await page.screenshot({ path: path.join(output, `empty-workbench-${width}.png`), fullPage: true });
  }
  const fixture = path.join(output, "empty-workbench-390.png");
  await page.setViewportSize({ width: 1440, height: 900 });
  await selectStage("整理资料");
  const materials = materialsHost.locator(".materials-workspace");
  await materials.waitFor({ state: "visible" });
  const [firstChooser] = await Promise.all([
    page.waitForEvent("filechooser"),
    materials.getByRole("button", { name: /添加资料到/ }).first().click(),
  ]);
  await firstChooser.setFiles(fixture);
  await waitUntil(async () => await materials.locator(".materials-media-item").count() === 1);
  const categoryTitles = await materials.locator(".materials-category-button").evaluateAll((buttons) =>
    buttons.map((button) => button.getAttribute("title")));
  assert.deepEqual(categoryTitles, [
    "全部资料", "事故参与方总体概况和损坏照片", "视频", "损伤信息照片",
    "车辆外部及外部损伤情况", "车辆内部及内部损伤情况", "其它信息", "现场", "隐私处理与截图",
  ]);
  assert.match(await materials.locator(".materials-add-destination").innerText(), /事故概况/);
  await materials.getByRole("button", { name: "列表视图", exact: true }).click();
  assert.equal(await materials.locator(".materials-media-grid.is-list").count(), 1);
  await materials.getByRole("button", { name: "缩略图视图", exact: true }).click();
  const firstMaterial = materials.locator(".materials-media-item").first();
  await firstMaterial.click();
  const materialDialog = page.locator(".materials-dialog");
  await materialDialog.waitFor({ state: "visible" });
  await waitUntil(async () => await materialDialog.locator("img").isVisible());
  await materialDialog.getByRole("button", { name: "关闭资料详情", exact: true }).click();
  await firstMaterial.click();
  await materialDialog.getByRole("button", { name: "删除资料", exact: true }).click();
  await waitUntil(async () => await materials.locator(".materials-media-item").count() === 0);

  const videoCategory = materials.locator('.materials-category-button[title="视频"]');
  await videoCategory.click();
  await waitUntil(async () => (await videoCategory.getAttribute("aria-pressed")) === "true");
  assert.match(await materials.locator(".materials-add-destination").innerText(), /视频/);
  const [secondChooser] = await Promise.all([
    page.waitForEvent("filechooser"),
    materials.getByRole("button", { name: /添加资料到/ }).first().click(),
  ]);
  await secondChooser.setFiles(fixture);
  await waitUntil(async () => await materials.locator(".materials-media-item").count() === 1);
  assert.equal(await materials.locator(".materials-media-category").innerText(), "视频");
  await materials.locator(".materials-media-item").click();
  await materialDialog.getByRole("button", { name: "删除资料", exact: true }).click();
  await waitUntil(async () => await materials.locator(".materials-media-item").count() === 0);
  assert(await materials.getByRole("button", { name: "生成事故事实", exact: true }).isDisabled());
  results.push("新建事故档案从档案抽屉进入真实资料空态，默认事故概况、八类真实分类、缩略图/列表、预览与删除路径均可用");

  await selectSession(bootstrap.session_id);
  await selectStage("核对事实");
  await waitUntil(async () => await factsHost.locator(".json-table-editor .value-input").count() === 37);
  const beforeConflict = await request(`${api}${sessionPath}`, { headers });
  const otherDraft = JSON.stringify({ 事故标题: "另一个客户端已保存的事故" });
  await request(`${api}${sessionPath}`, {
    method: "PUT", headers, body: JSON.stringify({
      expected_updated_at: beforeConflict.updated_at, draft_json: otherDraft,
    }),
  });
  const unsaved = factsHost.locator(".json-table-editor .value-input").first();
  await unsaved.fill("当前客户端尚未保存的事故");
  const conflict = page.waitForResponse((response) => response.url().endsWith(sessionPath)
    && response.request().method() === "PUT" && response.status() === 409);
  await openArchive();
  await page.locator(`[data-session-id="${newSessionId}"] button.session-title`).click();
  await conflict;
  await page.getByRole("alert").filter({ hasText: "未切换会话" }).waitFor();
  assert.equal(await page.locator(`[data-session-id="${bootstrap.session_id}"] button.session-title`)
    .getAttribute("aria-current"), "true");
  assert.equal(await unsaved.inputValue(), "当前客户端尚未保存的事故");
  const afterConflict = await request(`${api}${sessionPath}`, { headers });
  assert.equal(afterConflict.draft_json, otherDraft);
  results.push("新档案切换遇真实409时保留本地编辑并阻止切换，不覆盖其他客户端草稿");
}
