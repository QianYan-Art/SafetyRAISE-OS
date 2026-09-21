import assert from "node:assert/strict";
import fs from "node:fs/promises";
import os from "node:os";
import path from "node:path";
import { chromium } from "playwright";

// 使用真实前端组件与HTTP拦截样例，不调用模型或生产服务。
const base = process.env.WORKSPACE_TEST_URL || "http://127.0.0.1:15175";
const output = process.env.WORKSPACE_TEST_OUTPUT || await fs.mkdtemp(path.join(os.tmpdir(), "safetyraise-workspace-"));
await fs.mkdir(output, { recursive: true });
const browser = await chromium.launch({ headless: true });
const result = { scope: "真实前端浏览器测试，后端为拦截样例；不证明生产端到端", output, checks: [] };
try {
  for (const role of ["user", "admin"]) for (const width of [1440, 1024, 390, 320]) {
    const context = await browser.newContext({ viewport: { width, height: width > 600 ? 960 : 844 } });
    await context.addInitScript(() => localStorage.setItem("traffic-accident-auth-token", "synthetic-ui-test"));
    const page = await context.newPage();
    const errors = [], unexpected = [], traffic = [];
    let releaseGeneration;
    const generationGate = new Promise(resolve => { releaseGeneration = resolve; });
    page.on("pageerror", error => errors.push(error.message));
    let session = { id: "ui-session", title: "未命名事故", created_at: 1000, updated_at: 1000, messages: [], draft_json: "", draft_meta: null, report_result: null, linked_files: [], linked_artifacts: [], session_state: "draft" };
    const configuration = { role, capabilities: ["vision", "report", "embedding"].map(capability => ({ capability, configured: true, base_url: "https://example.invalid/v1", model_name: "synthetic-model", api_key_masked: "****TEST", params: {} })), system_defaults: { embedding: { top_k: 5 } } };
    await page.route("**/api/**", async route => {
      const req = route.request(), url = new URL(req.url()), method = req.method();
      const endpoint = url.pathname;
      traffic.push(`${method} ${endpoint}`);
      let body;
      if (endpoint === "/api/v1/auth/me") body = { id: "ui-user", username: `${role}_test`, display_name: "不能替代用户名", role, is_active: true };
      else if (endpoint === "/api/v1/app-config") body = { upload_limits: {}, report_model: {}, report_harness: { enabled: false, online_enabled: true } };
      else if (endpoint === "/api/v1/admin/users") body = [
        { id: "ui-user", username: "admin_test", display_name: "", role: "admin", is_active: true, created_at: "2026-09-20T00:00:00Z", updated_at: "2026-09-20T00:00:00Z" },
        { id: "analyst", username: "analyst_test", display_name: "", role: "user", is_active: true, created_at: "2026-09-20T00:00:00Z", updated_at: "2026-09-20T00:00:00Z" },
      ];
      else if (endpoint === "/api/v1/admin/spaces") body = Array.from({ length: 36 }, (_, index) => ({
        session_id: `${session.id}-${index + 1}`,
        title: index === 0 ? "测试档案" : `测试档案 ${String(index + 1).padStart(2, "0")}`,
        owner_user_id: "analyst",
        owner_username: "analyst_test",
        created_at: 1000 - index,
        updated_at: 1000 - index,
        session_state: "draft",
        source_type: index % 2 === 0 ? "image" : "video",
        source_name: index % 2 === 0 ? "logo.png" : "sample.mp4",
        message_count: index + 1,
        linked_artifact_count: index % 3,
        redacted: false,
      }));
      else if (endpoint === "/api/v1/user/model-configs") {
        if (method === "PUT") for (const item of req.postDataJSON().items) {
          const previous = configuration.capabilities.find(value => value.capability === item.capability);
          Object.assign(previous, { base_url: item.base_url, model_name: item.model_name, params: item.params || {} });
        }
        body = configuration;
      } else if (endpoint.endsWith("/linked-artifacts")) body = [];
      else if (endpoint === "/api/v1/chat-sessions" && method === "GET") body = [session];
      else if (endpoint.startsWith("/api/v1/chat-sessions")) {
        if (["PUT", "POST"].includes(method)) session = { ...session, ...req.postDataJSON(), updated_at: session.updated_at + 1 };
        body = session;
      } else if (endpoint === "/api/v1/inputs/generate-from-upload") {
        assert(req.postData().includes("accident_overview"));
        await generationGate;
        const stressFields = width === 320 ? Object.fromEntries(Array.from({ length: 34 }, (_, index) => [`测试字段${index + 1}`, "仅用于长文本布局检查，不是真实事故材料。".repeat(5)])) : {};
        body = { trace_id: "synthetic-input", generated_input: { 事故时间: "待核实", 事故地点: "测试道路", 事故经过: "界面测试样例，不是真实事故", ...stressFields }, media_type: "image", source_count: 1, frame_manifest: [], yolo_summary_preview: null };
      } else {
        unexpected.push(`${method} ${endpoint}`);
        return route.fulfill({ status: 404, json: { message: "未定义的测试接口" } });
      }
      await route.fulfill({ json: body });
    });
    await page.goto(base);
    await page.getByRole("button", { name: "打开档案列表", exact: true }).waitFor();
    async function screenshot(name) {
      assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth + 1), false, `${role}/${width}/${name}横向溢出`);
      await page.screenshot({ path: path.join(output, `${role}-${width}-${name}.png`), fullPage: true });
    }
    await screenshot("home");
    assert.equal(await page.getByRole("dialog", { name: "档案导航" }).isVisible(), false);
    assert.equal(await page.getByLabel("报告模式").count(), 0);
    assert.equal(await page.getByTitle("打开管理控制台").isVisible(), role === "admin");
    assert.equal(await page.getByRole("region", { name: "处理记录" }).isVisible(), false);
    const before = await page.locator(".review-panel").boundingBox();
    await page.getByRole("button", { name: "打开档案列表", exact: true }).click();
    assert.deepEqual(await page.locator(".review-panel").boundingBox(), before);
    assert((await page.locator(".sidebar-account-copy strong").textContent()) === `${role}_test`);
    await screenshot("archive");
    await page.keyboard.press("Escape");
    assert(await page.locator(".archive-trigger").evaluate(node => node === document.activeElement));
    await page.locator('input[type="file"]').setInputFiles(path.resolve("public/logo.png"));
    // 文件选择前必须已指定业务分组，直接设置隐藏input不应隐式分类。
    assert.equal(traffic.some(item => item.includes("generate-from-upload")), false);
    const chooserPromise = page.waitForEvent("filechooser");
    await page.getByRole("button", { name: /添加图片或视频|添加资料/ }).first().click();
    const chooser = await chooserPromise;
    await chooser.setFiles(path.resolve("public/logo.png"));
    await page.getByText("logo.png", { exact: true }).first().waitFor();
    const storageNote = page.locator(".materials-category-note");
    if (width > 600) {
      assert(await storageNote.isVisible(), `${role}/${width}桌面端应显示本机暂存说明`);
      const noteAlignment = await storageNote.evaluate(node => {
        const icon = node.querySelector(":scope > svg");
        const copy = node.querySelector(":scope > span");
        if (!icon || !copy) return null;
        const iconBox = icon.getBoundingClientRect();
        const copyBox = copy.getBoundingClientRect();
        return Math.abs((iconBox.top + iconBox.height / 2) - (copyBox.top + copyBox.height / 2));
      });
      assert(noteAlignment !== null && noteAlignment <= 1.5, `${role}/${width}本机暂存锁图标未相对整段文字居中`);
    } else {
      assert.equal(await storageNote.isVisible(), false, `${role}/${width}移动端应沿用紧凑分类栏`);
    }
    await screenshot("materials");
    const materialTrigger = page.getByRole("button", { name: "打开资料详情：logo.png" });
    await materialTrigger.click();
    const materialDialog = page.getByRole("dialog", { name: "logo.png", exact: true });
    await page.keyboard.press("Shift+Tab");
    assert(await materialDialog.evaluate(node => node.contains(document.activeElement)));
    assert(await materialDialog.evaluate(node => {
      let branch = node;
      while (branch.parentElement && branch.parentElement !== document.body) {
        if ([...branch.parentElement.children].some(sibling => sibling !== branch && sibling.inert)) return true;
        branch = branch.parentElement;
      }
      return false;
    }), "资料预览必须隔离背景");
    await page.keyboard.press("Escape");
    assert(await materialTrigger.evaluate(node => node === document.activeElement));
    const generationRequest = page.waitForRequest("**/api/v1/inputs/generate-from-upload");
    await page.getByRole("button", { name: /生成事故事实|生成事故信息/ }).click();
    await generationRequest;
    const sessionWrites = () => traffic.filter(item => /^(POST|DELETE) .*chat-sessions/.test(item)).length;
    const writesBefore = sessionWrites();
    await page.getByRole("button", { name: "打开档案列表", exact: true }).click();
    await page.getByRole("button", { name: "新建事故档案", exact: true }).click();
    assert.equal(sessionWrites(), writesBefore, "生成中不得创建新档案");
    await page.getByRole("button", { name: "打开账号菜单", exact: true }).click();
    const accountHeader = page.locator(".account-popover-header");
    const accountThemeToggle = accountHeader.getByRole("button", { name: /切换为(深|浅)色模式/ });
    assert.equal(await accountThemeToggle.count(), 1, "主题按钮必须位于账号菜单头部");
    assert(await accountHeader.evaluate(node => {
      const username = node.querySelector(":scope > span");
      const toggle = node.querySelector(":scope > button");
      if (!username || !toggle) return false;
      const usernameBox = username.getBoundingClientRect();
      const toggleBox = toggle.getBoundingClientRect();
      return usernameBox.left < toggleBox.left
        && Math.abs((usernameBox.top + usernameBox.height / 2) - (toggleBox.top + toggleBox.height / 2)) <= 1.5;
    }), `${role}/${width}用户名与主题按钮未左右居中对齐`);
    await screenshot("account-menu");
    await accountThemeToggle.click();
    assert(await page.locator(".evidence-workspace").evaluate(node => node.classList.contains("theme-dark")), "主题按钮应切换到深色模式");
    assert(await accountHeader.evaluate(node => getComputedStyle(node.parentElement).backgroundColor !== "rgba(255, 255, 255, 0.96)"), "深色账号菜单不能保留浅色背景");
    await page.waitForFunction(() => getComputedStyle(document.querySelector(".account-popover-item")).backgroundColor === "rgb(41, 44, 49)");
    assert.equal(await page.locator(".account-popover-item").first().evaluate(node => getComputedStyle(node).backgroundColor), "rgb(41, 44, 49)", "深色账号菜单操作项应使用深色表面");
    await screenshot("account-menu-dark");
    await accountHeader.getByRole("button", { name: "切换为浅色模式", exact: true }).click();
    await page.getByRole("button", { name: "退出登录", exact: true }).click();
    assert(await page.evaluate(() => Boolean(localStorage.getItem("traffic-accident-auth-token"))), "生成中不得退出");
    await page.keyboard.press("Escape");
    await page.getByText("当前档案正在生成，请等待完成或停止报告后再切换、新建、删除档案或退出登录。", { exact: true }).waitFor();
    releaseGeneration();
    const fact = page.getByRole("textbox", { name: "事故地点", exact: true });
    await fact.waitFor();
    if (width === 320) {
      assert.equal(await page.locator(".json-table-editor textarea").count(), 37);
      await page.waitForFunction(() => [...document.querySelectorAll(".json-table-editor textarea")].every(node => node.scrollHeight <= node.clientHeight + 1));
      assert(await page.locator(".json-table-editor").evaluate(table => [...table.querySelectorAll("textarea")].every(node => {
        const box = node.getBoundingClientRect();
        return box.x >= 0 && box.right <= innerWidth + 1 && node.scrollWidth <= node.clientWidth + 1;
      })), "37字段长文本应完整换行且不越出320px屏幕");
    }
    await fact.fill("已编辑的测试道路");
    await page.getByRole("heading", { name: "事故信息", exact: true }).click();
    await page.getByText("已保存", { exact: true }).waitFor();
    await page.getByRole("button", { name: /整理资料/ }).first().click();
    await page.getByRole("button", { name: /核对事实/ }).first().click();
    assert.equal(await fact.inputValue(), "已编辑的测试道路");
    assert(JSON.parse(session.draft_json).事故地点 === "已编辑的测试道路");
    const saveButton = page.getByRole("button", { name: "保存修改", exact: true });
    await saveButton.scrollIntoViewIfNeeded();
    assert(await saveButton.evaluate(node => {
      const box = node.getBoundingClientRect();
      return document.elementFromPoint(box.x + box.width / 2, box.y + box.height / 2)?.closest("button") === node;
    }), "保存按钮不能被生成按钮覆盖");
    await screenshot("facts");
    await page.getByRole("button", { name: /查看报告/ }).click();
    await screenshot("report-empty");
    await page.getByTitle("处理记录", { exact: true }).click();
    assert(await page.getByRole("region", { name: "处理记录" }).isVisible());
    await page.getByTitle("关闭处理记录", { exact: true }).click();
    await page.getByTitle("模型配置调整", { exact: true }).click();
    const modelDialog = page.getByRole("dialog", { name: "模型配置", exact: true });
    assert(await modelDialog.evaluate(node => node.contains(document.activeElement)));
    await page.keyboard.press("Shift+Tab");
    assert(await modelDialog.evaluate(node => node.contains(document.activeElement)));
    await page.getByLabel("模型名称", { exact: true }).fill("updated-model");
    await page.getByRole("button", { name: "保存配置", exact: true }).click();
    await page.getByText("无未保存修改", { exact: true }).waitFor();
    assert.equal(configuration.capabilities.find(item => item.capability === "vision").model_name, "updated-model");
    assert.equal(configuration.capabilities.find(item => item.capability === "report").model_name, "synthetic-model");
    assert(await modelDialog.isVisible(), "保存当前用途后应保留配置页");
    await screenshot("models");
    await page.getByRole("button", { name: "返回工作区", exact: true }).click();
    if (role === "admin") {
      await page.getByTitle("打开管理控制台", { exact: true }).click();
      await page.getByRole("button", { name: "编辑 analyst_test", exact: true }).waitFor();
      await screenshot("users");
      await page.getByRole("button", { name: "编辑 analyst_test", exact: true }).click();
      assert(await page.getByRole("dialog", { name: "编辑用户", exact: true }).evaluate(node => node.contains(document.activeElement)));
      await screenshot("user-editor");
      await page.getByRole("button", { name: "取消", exact: true }).click();
      await page.getByRole("tab", { name: "空间管理", exact: true }).click();
      await page.getByText("测试档案", { exact: true }).first().waitFor();
      await page.getByLabel("每页条数", { exact: true }).selectOption("50");
      const spaceRegion = page.getByRole("region", { name: "空间列表", exact: true });
      await spaceRegion.focus();
      assert(await spaceRegion.evaluate(node => node === document.activeElement), "空间列表应支持键盘聚焦滚动");
      const scrollMetrics = await spaceRegion.evaluate(node => {
        node.scrollTop = node.scrollHeight;
        return { clientHeight: node.clientHeight, scrollHeight: node.scrollHeight, scrollTop: node.scrollTop };
      });
      assert(scrollMetrics.scrollHeight > scrollMetrics.clientHeight + 1, `${width}px空间列表未形成独立滚动区`);
      assert(scrollMetrics.scrollTop > 0, `${width}px空间列表不能独立滚动`);
      assert(await page.locator(".main-content, .account-admin-shell, .account-admin-surface").evaluateAll(nodes => nodes.every(node => (
        node.scrollTop === 0 && node.scrollHeight <= node.clientHeight + 1
      ))), `${width}px空间管理外层容器不应滚动`);
      assert(await page.evaluate(() => {
        const root = document.scrollingElement;
        return Boolean(root)
          && root.scrollHeight <= root.clientHeight + 1
          && document.body.scrollHeight <= innerHeight + 1;
      }), `${width}px空间管理不应让整个页面滚动`);
      await screenshot("spaces");
      await page.getByRole("button", { name: "打开档案列表", exact: true }).click();
      await page.getByRole("button", { name: "打开账号菜单", exact: true }).click();
      await page.getByRole("button", { name: "切换为深色模式", exact: true }).click();
      await page.keyboard.press("Escape");
      await page.keyboard.press("Escape");
      await page.getByRole("dialog", { name: "档案导航" }).waitFor({ state: "hidden" });
      assert(await page.locator(".account-admin-surface").evaluate(node => getComputedStyle(node).backgroundColor === "rgb(32, 34, 38)"), "深色管理员页面应使用工作台深色表面");
      await screenshot("spaces-dark");
      await page.getByTitle("模型配置调整", { exact: true }).click();
      await page.getByRole("button", { name: "返回管理中心", exact: true }).click();
      assert(await page.getByRole("tab", { name: "空间管理", exact: true }).isVisible());
      await page.getByRole("button", { name: "返回主界面", exact: true }).click();
      assert(await page.getByRole("navigation", { name: "事故处理阶段" }).isVisible());
    }
    assert.equal(traffic.some(item => /report-runs|report-evidence/.test(item)), false, "禁用harness不得发请求");
    assert.deepEqual(unexpected, []);
    assert.deepEqual(errors, []);
    result.checks.push(`${role}/${width}: 实际组件布局、覆盖导航、真实用户名、分类上传请求、事实保存与往返、处理记录、配置入口、禁用能力零请求`);
    await context.close();
  }
  result.passed = true;
} catch (error) { result.passed = false; result.error = error.stack; process.exitCode = 1; }
finally { await browser.close(); await fs.writeFile(path.join(output, "result.json"), JSON.stringify(result, null, 2)); }
console.log(JSON.stringify(result));
