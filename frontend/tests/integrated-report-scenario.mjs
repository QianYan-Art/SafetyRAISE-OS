import assert from "node:assert/strict";
import path from "node:path";
import { readFile } from "node:fs/promises";

// 真实浏览器、HTTP 和独立数据库；模型仅合成角色，不证明真实业务报告质量。
export async function verifyIntegratedReport({ page, bootstrap, api, output, results, waitUntil, request }) {
  const headers = { Authorization: `Bearer ${bootstrap.token}`, "Content-Type": "application/json" };
  const sessionPath = `/api/v1/chat-sessions/${bootstrap.session_id}`;
  const evidenceUrl = `${api}${sessionPath}/report-evidence`;
  const sessionButton = page.locator(`[data-session-id="${bootstrap.session_id}"] button.session-title`);
  await sessionButton.focus();
  await sessionButton.press("Enter");
  const panel = page.getByRole("region", { name: "事故分析报告" });
  await panel.waitFor();
  assert.equal(await sessionButton.getAttribute("aria-current"), "true");
  results.push("会话通过原生按钮键盘进入，并标识当前会话");
  assert.equal(await page.getByRole("tab", { name: "证据报告", exact: true }).count(), 0);
  await panel.locator("summary").first().click();
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
  results.push("原页面补充材料新增、修改核实状态、删除均经真实HTTP持久化");

  const editor = page.locator(".json-table-editor .value-input").first();
  await editor.fill("合成案例一：原表单确认后生成。");
  await editor.blur();
  const confirm = page.getByRole("button", { name: /确认.*生成/ }).first();
  await confirm.click();
  await waitUntil(async () => (await panel.getByRole("status").innerText()) === "报告已完成", 45000);
  let runs = await request(`${api}/api/v1/report-runs?session_id=${bootstrap.session_id}`, { headers });
  assert.equal(runs.runs.length, 1);
  const firstId = runs.runs[0].run_id;
  const saved = await request(`${api}${sessionPath}`, { headers });
  assert(saved.draft_json.includes("合成案例一"));
  assert.equal(runs.runs[0].review_status, "passed");
  assert.equal(runs.runs[0].formal_export_eligible, false);
  assert.equal(await panel.getByRole("button", { name: "下载Word" }).count(), 0);
  results.push("原确认按钮依次完成保存、创建、授权、合成生成和独立合成审查；禁止冒充正式导出");

  await editor.fill("合成案例二：验证历史报告切换。");
  await editor.blur();
  await confirm.click();
  await waitUntil(async () => {
    runs = await request(`${api}/api/v1/report-runs?session_id=${bootstrap.session_id}`, { headers });
    return runs.runs.length === 2 && runs.runs[0].state === "published";
  }, 45000);
  const secondId = runs.runs[0].run_id;
  await waitUntil(async () => !(await panel.getByLabel("历史报告").isDisabled()));
  await panel.getByLabel("历史报告").selectOption(firstId);
  await waitUntil(async () => (await panel.getByLabel("历史报告").inputValue()) === firstId);
  await panel.getByLabel("历史报告").selectOption(secondId);
  await page.reload();
  await page.locator(`[data-session-id="${bootstrap.session_id}"] .session-item`).click();
  await waitUntil(async () => (await panel.getByRole("status").innerText()) === "报告已完成");
  assert.equal(await panel.getByLabel("历史报告").inputValue(), secondId);
  results.push("历史报告可切换，刷新后恢复最新报告");

  for (const viewport of [
    { width: 1440, height: 900 }, { width: 1024, height: 900 },
    { width: 768, height: 1024 }, { width: 390, height: 844 }, { width: 320, height: 720 },
  ]) {
    await page.setViewportSize(viewport);
    await page.waitForFunction(() => {
      const sidebar = document.querySelector(".sidebar:not(.mobile-open)");
      return innerWidth > 768 || !sidebar || sidebar.getBoundingClientRect().right <= 1;
    });
    if (viewport.width <= 1024) {
      await page.getByRole("button", { name: "分析与审阅", exact: true }).click();
    }
    await panel.scrollIntoViewIfNeeded();
    await page.waitForFunction(() => !document.getAnimations().some((animation) =>
      animation.playState === "running" && animation.effect?.getTiming().iterations !== Infinity));
    await page.waitForFunction(() => [...document.querySelectorAll(".json-table-editor .value-input")]
      .every((input) => input.scrollHeight <= input.clientHeight + 1));
    const layout = await panel.evaluate((node) => ({
      width: document.documentElement.scrollWidth,
      viewport: innerWidth,
      contained: innerWidth > 1024 || node.getBoundingClientRect().bottom
        <= node.closest(".panel").getBoundingClientRect().bottom + 1,
      overflowing: [...node.querySelectorAll("button,input,textarea,select")].filter((element) => {
        const box = element.getBoundingClientRect();
        return element.checkVisibility() && box.width > 0 && (box.left < -1 || box.right > innerWidth + 1);
      }).map((element) => ({
        tag: element.tagName, label: element.getAttribute("aria-label"),
        left: element.getBoundingClientRect().left, right: element.getBoundingClientRect().right,
      })),
    }));
    assert(layout.width <= layout.viewport + 1, JSON.stringify(layout));
    assert(layout.contained, "手机报告内容必须位于审阅区内");
    assert.deepEqual(layout.overflowing, []);
    const alignment = await page.locator(".report-action-dock").evaluate((node) => {
      const box = node.getBoundingClientRect();
      const body = node.closest(".panel-body").getBoundingClientRect();
      const button = node.querySelector(".report-submit-btn").getBoundingClientRect();
      return {
        inside: box.left >= body.left && box.right <= body.right,
        buttonInside: button.left >= box.left - 1 && button.right <= box.right + 1,
        height: box.height,
      };
    });
    assert(alignment.inside && alignment.buttonInside, JSON.stringify(alignment));
    assert(alignment.height >= 44 && alignment.height <= 48, JSON.stringify(alignment));
    assert.equal(await page.locator(".artifact-wall-watermark:visible").count(), 0);
    await page.screenshot({ path: path.join(output, `integrated-${viewport.width}.png`), fullPage: true });
    await panel.locator("summary").first().click();
    const editorOverflow = await panel.locator(".report-evidence-editor").evaluate((node) =>
      [...node.querySelectorAll("button,input,textarea")].filter((element) => {
        const box = element.getBoundingClientRect();
        return element.checkVisibility() && (box.left < -1 || box.right > innerWidth + 1);
      }).map((element) => element.outerHTML.slice(0, 180)));
    assert.deepEqual(editorOverflow, [], `补证展开后控件越界：${viewport.width}`);
    await panel.locator("summary").first().click();
  }
  results.push("1440/1024/768/390/320布局无横向溢出，主按钮44至48px并对齐且无水印遮挡");

  await page.getByRole("button", { name: "切换为深色模式", exact: true }).click();
  for (const width of [1440, 390]) {
    await page.setViewportSize({ width, height: 900 });
    await page.waitForFunction(() => {
      const sidebar = document.querySelector(".sidebar:not(.mobile-open)");
      return innerWidth > 768 || !sidebar || sidebar.getBoundingClientRect().right <= 1;
    });
    if (width <= 1024) await page.getByRole("button", { name: "分析与审阅", exact: true }).click();
    await panel.scrollIntoViewIfNeeded();
    await page.waitForFunction(() => !document.getAnimations().some((animation) =>
      animation.playState === "running" && animation.effect?.getTiming().iterations !== Infinity));
    assert.equal(await page.locator(".safety-workbench.theme-dark").count(), 1);
    const colors = await panel.evaluate((node) => ({
      color: getComputedStyle(node).color,
      surface: getComputedStyle(node.closest(".panel")).backgroundColor,
      overflow: document.documentElement.scrollWidth > innerWidth + 1,
    }));
    assert.notEqual(colors.color, colors.surface);
    assert.equal(colors.overflow, false);
    await page.screenshot({ path: path.join(output, `integrated-dark-${width}.png`), fullPage: true });
  }
  await page.getByRole("button", { name: "切换为浅色模式", exact: true }).click();
  results.push("深色主题桌面与手机无溢出，正文与工作面颜色不同并保留状态文字");

  await confirm.click();
  const busyDock = page.locator(".report-action-dock.is-generating");
  await busyDock.waitFor();
  const busyGeometry = await busyDock.evaluate((node) => {
    const main = node.querySelector(".report-submit-btn").getBoundingClientRect();
    const stop = node.querySelector(".report-stop-btn").getBoundingClientRect();
    const dock = node.getBoundingClientRect();
    return { aligned: main.left >= dock.left - 1 && stop.right <= dock.right + 1
      && main.right <= stop.left && main.height >= 44 && stop.height >= 44,
      main: { left: main.left, right: main.right, height: main.height },
      stop: { left: stop.left, right: stop.right, height: stop.height },
      dock: { left: dock.left, right: dock.right } };
  });
  assert(busyGeometry.aligned, `生成中主按钮和停止按钮必须对齐且不重叠：${JSON.stringify(busyGeometry)}`);
  let releaseCancel;
  let cancelRequests = 0;
  const cancelBarrier = new Promise((resolve) => { releaseCancel = resolve; });
  await page.route("**/api/v1/report-runs/*/cancel", async (route) => {
    cancelRequests += 1;
    await cancelBarrier;
    await route.continue();
  });
  try {
    await page.locator(".report-stop-btn").click();
    await waitUntil(async () => await panel.getByRole("button", { name: "正在停止", exact: true }).isDisabled());
    await waitUntil(() => page.locator(".report-stop-btn").isDisabled());
    assert.equal(await page.locator(".report-stop-btn").innerText(), "正在停止");
    const waitingGeometry = await page.locator(".report-action-dock").evaluate((node) => {
      const stop = node.querySelector(".report-stop-btn");
      return stop.scrollWidth <= stop.clientWidth;
    });
    assert(waitingGeometry, "正在停止文案必须完整容纳于固定宽度按钮");
    assert.equal(cancelRequests, 1);
    await page.screenshot({ path: path.join(output, "integrated-cancelling.png") });
    for (const width of [1440, 390]) {
      await page.setViewportSize({ width, height: 900 });
      await page.waitForFunction(() => !document.getAnimations().some((animation) =>
        animation.playState === "running" && animation.effect?.getTiming().iterations !== Infinity));
      const sizes = await page.evaluate(() => {
        const top = document.querySelector(".report-stop-btn");
        const bottom = document.querySelector(".integrated-report button[aria-busy='true']");
        return {
          top: top.getBoundingClientRect().height,
          bottom: bottom.getBoundingClientRect().height,
          radiusTop: getComputedStyle(top).borderRadius,
          radiusBottom: getComputedStyle(bottom).borderRadius,
        };
      });
      assert.equal(sizes.top, width > 640 ? 44 : 48);
      assert.equal(sizes.top, sizes.bottom, JSON.stringify({ width, ...sizes }));
      assert.equal(sizes.radiusTop, sizes.radiusBottom);
      await page.screenshot({ path: path.join(output, `integrated-cancelling-${width}.png`) });
    }
  } finally {
    releaseCancel();
  }
  await waitUntil(async () => (await panel.getByRole("status").innerText()) === "已停止");
  await page.unroute("**/api/v1/report-runs/*/cancel");
  runs = await request(`${api}/api/v1/report-runs?session_id=${bootstrap.session_id}`, { headers });
  assert.equal(runs.runs[0].state, "cancelled");
  results.push("两处停止按钮等待时同步禁用且文案不溢出，真实取消后数据库保留状态");

  const unknown = await request(`${api}/__harness_test__/scenario/unknown`, {
    method: "POST", headers: { "X-Harness-Control": bootstrap.control_token },
  });
  await page.setViewportSize({ width: 1440, height: 900 });
  await page.reload();
  await page.locator(`[data-session-id="${bootstrap.session_id}"] .session-item`).click();
  await waitUntil(async () => (await panel.getByRole("status").innerText()) === "生成已暂停");
  assert(await panel.getByRole("button", { name: "继续生成", exact: true }).isDisabled());
  const suspended = await request(`${api}/api/v1/report-runs/${unknown.run_id}`, { headers });
  assert.equal(suspended.budget.unknown_requests, 1);
  await panel.getByLabel("确认重试未收到结果的请求，可能重复计费").check();
  await panel.getByLabel("历史报告").selectOption(firstId);
  await waitUntil(async () => (await panel.getByRole("status").innerText()) === "报告已完成");
  await panel.getByLabel("历史报告").selectOption(unknown.run_id);
  await waitUntil(async () => (await panel.getByRole("status").innerText()) === "生成已暂停");
  assert.equal(await panel.getByLabel("确认重试未收到结果的请求，可能重复计费").isChecked(), false);
  assert(await panel.getByRole("button", { name: "继续生成", exact: true }).isDisabled());
  await panel.getByLabel("确认重试未收到结果的请求，可能重复计费").check();
  await panel.getByRole("button", { name: "继续生成", exact: true }).click();
  await waitUntil(async () => (await panel.getByRole("status").innerText()) === "报告已完成", 45000);
  results.push("合成未知请求刷新不自动重试，用户显式确认后才恢复");

  await request(`${api}/__harness_test__/scenario/unknown`, {
    method: "POST", headers: { "X-Harness-Control": bootstrap.control_token },
  });
  await page.reload();
  await page.locator(`[data-session-id="${bootstrap.session_id}"] .session-item`).click();
  await waitUntil(async () => (await panel.getByRole("status").innerText()) === "生成已暂停");
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
  await page.reload();
  await page.locator(`[data-session-id="${bootstrap.session_id}"] .session-item`).click();
  await page.getByText("读取报告服务配置失败，暂不能生成报告，请刷新页面重试。", { exact: true }).waitFor();
  assert(await page.getByRole("button", { name: /确认.*生成/ }).first().isDisabled());
  assert.equal(legacyRequests, 0);
  page.off("request", observe);
  await page.unroute("**/api/v1/app-config");
  results.push("公开配置失败时禁止生成，不回退旧报告接口");

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
  await page.locator(`[data-session-id="${bootstrap.session_id}"] .session-item`).click();
  await waitUntil(async () => await page.locator(".json-table-editor .value-input").count() === 37);
  for (const width of [1440, 320]) {
    await page.setViewportSize({ width, height: 900 });
    if (width <= 1024) await page.getByRole("button", { name: "分析与审阅", exact: true }).click();
    await page.waitForFunction(() => !document.getAnimations().some((animation) =>
      animation.playState === "running" && animation.effect?.getTiming().iterations !== Infinity));
    const fields = await page.locator(".json-table-editor").evaluate((table) =>
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
    await page.locator(".json-table-editor").scrollIntoViewIfNeeded();
    await page.screenshot({ path: path.join(output, `fields-37-${width}.png`), fullPage: true });
  }
  await page.getByRole("button", { name: "切换为深色模式", exact: true }).click();
  await page.locator(".json-table-editor").scrollIntoViewIfNeeded();
  await page.screenshot({ path: path.join(output, "fields-37-dark-320.png") });
  results.push("原模板37字段配合合成长文本在1440/320完整换行可读且不越单元格，并有字段可访问名称");
  await page.getByRole("button", { name: "切换为浅色模式", exact: true }).click();
  await page.setViewportSize({ width: 1440, height: 900 });
  await page.getByRole("button", { name: "新建对话", exact: true }).click();
  await page.getByText("尚未生成报告", { exact: true }).waitFor();
  for (const width of [1440, 390]) {
    await page.setViewportSize({ width, height: 900 });
    if (width <= 1024) await page.getByRole("button", { name: "分析与审阅", exact: true }).click();
    await page.waitForFunction(() => !document.getAnimations().some((animation) =>
      animation.playState === "running" && animation.effect?.getTiming().iterations !== Infinity));
    await page.waitForFunction(() => {
      const sidebar = document.querySelector(".sidebar");
      return innerWidth > 768 || !sidebar || sidebar.getBoundingClientRect().right <= 1;
    });
    assert(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth));
    await page.screenshot({ path: path.join(output, `empty-workbench-${width}.png`) });
  }
  results.push("新建空会话在桌面手机无横向溢出且显示真实空态");
  await page.getByRole("button", { name: "上传事故资料", exact: true }).click();
  const uploadDialog = page.getByRole("dialog", { name: "上传工作台" });
  await uploadDialog.waitFor();
  assert.equal(await uploadDialog.locator(".upload-group-panel").count(), 8);
  const [fileChooser] = await Promise.all([
    page.waitForEvent("filechooser"),
    uploadDialog.getByRole("button", { name: "上传资料", exact: true }).first().click({ timeout: 10000 }),
  ]);
  await fileChooser.setFiles(path.join(output, "empty-workbench-390.png"));
  await uploadDialog.getByText("empty-workbench-390.png", { exact: true }).waitFor();
  assert.equal(await uploadDialog.getByRole("button", { name: "生成事故信息", exact: true }).isDisabled(), false);
  for (const width of [1440, 390]) {
    await page.setViewportSize({ width, height: 900 });
    await page.waitForFunction(() => !document.getAnimations().some((animation) =>
      animation.playState === "running" && animation.effect?.getTiming().iterations !== Infinity));
    const bounds = await uploadDialog.evaluate((node) => {
      const box = node.getBoundingClientRect();
      return { left: box.left, right: box.right, viewport: innerWidth, overflow: node.scrollWidth > node.clientWidth + 1 };
    });
    assert(bounds.left >= 0 && bounds.right <= bounds.viewport && !bounds.overflow, JSON.stringify(bounds));
    await page.screenshot({ path: path.join(output, `upload-group-${width}.png`) });
  }
  await uploadDialog.getByRole("button", { name: "删除", exact: true }).click();
  assert(await uploadDialog.getByRole("button", { name: "生成事故信息", exact: true }).isDisabled());
  await uploadDialog.getByRole("button", { name: "退出满屏", exact: true }).click();
  await uploadDialog.waitFor({ state: "hidden" });
  results.push("上传分组支持实际文件选择与删除，生成启用态正确，满屏桌面手机不越界");
  await page.setViewportSize({ width: 1440, height: 900 });
  const newSessionId = await page.locator("[data-session-id]")
    .filter({ has: page.locator("button.session-title[aria-current='true']") })
    .getAttribute("data-session-id");
  assert(newSessionId && newSessionId !== bootstrap.session_id);
  await page.locator(`[data-session-id="${bootstrap.session_id}"] button.session-title`).click();
  await waitUntil(async () => await page.locator(".json-table-editor .value-input").count() === 37);
  const beforeConflict = await request(`${api}${sessionPath}`, { headers });
  const otherDraft = JSON.stringify({ 事故标题: "另一个客户端已保存的事故" });
  await request(`${api}${sessionPath}`, {
    method: "PUT", headers, body: JSON.stringify({
      expected_updated_at: beforeConflict.updated_at, draft_json: otherDraft,
    }),
  });
  const unsaved = page.locator(".json-table-editor .value-input").first();
  await unsaved.fill("当前客户端尚未保存的事故");
  const conflict = page.waitForResponse((response) => response.url().endsWith(sessionPath)
    && response.request().method() === "PUT" && response.status() === 409);
  await page.locator(`[data-session-id="${newSessionId}"] button.session-title`).click();
  await conflict;
  await page.getByRole("alert").filter({ hasText: "未切换会话" }).waitFor();
  assert.equal(await page.locator(`[data-session-id="${bootstrap.session_id}"] button.session-title`)
    .getAttribute("aria-current"), "true");
  assert.equal(await unsaved.inputValue(), "当前客户端尚未保存的事故");
  const afterConflict = await request(`${api}${sessionPath}`, { headers });
  assert.equal(afterConflict.draft_json, otherDraft);
  results.push("普通失焦保存遇真实409保留本地编辑并阻止切换，不覆盖其他客户端草稿");
}
