import assert from "node:assert/strict";
import { spawn } from "node:child_process";
import { test } from "node:test";
import { cleanupSucceeded, stopChild, trackChild } from "./process-cleanup.mjs";

test("信号终止已经发生时不等待第二次退出事件", async () => {
  const child = spawn(process.execPath, ["-e", "setInterval(() => {}, 1000)"], {
    windowsHide: true, stdio: "ignore",
  });
  const tracked = trackChild(child);
  child.kill();
  assert.equal(await tracked.wait(5000), true);
  const result = await stopChild(tracked, 10, 10);
  assert.equal(result.forced, false);
  assert(child.exitCode !== null || child.signalCode !== null);
  assert.equal(cleanupSucceeded(result, true), false);
});

test("仍在运行的进程有界终止且记录强制清理", async () => {
  const child = spawn(process.execPath, ["-e", "setInterval(() => {}, 1000)"], {
    windowsHide: true, stdio: "ignore",
  });
  const result = await stopChild(trackChild(child), 5, 5000);
  assert.equal(result.forced, true);
  assert.equal(cleanupSucceeded(result, true), true);
  assert.equal(cleanupSucceeded(result), false);
});

test("无法结束的进程明确失败而不是无限等待", async () => {
  const tracked = { child: { kill() {} }, async wait() { return false; } };
  await assert.rejects(stopChild(tracked, 1, 1), /终止超时/);
});

test("服务提前非零退出不能当作主动清理通过", async () => {
  const child = spawn(process.execPath, ["-e", "process.exit(7)"], {
    windowsHide: true, stdio: "ignore",
  });
  const tracked = trackChild(child);
  assert.equal(await tracked.wait(5000), true);
  assert.equal(cleanupSucceeded(await stopChild(tracked), true), false);
});

test("服务正常退出通过且不需要强杀", async () => {
  const child = spawn(process.execPath, ["-e", "process.exit(0)"], {
    windowsHide: true, stdio: "ignore",
  });
  const tracked = trackChild(child);
  assert.equal(await tracked.wait(5000), true);
  assert.equal(cleanupSucceeded(await stopChild(tracked)), true);
});
