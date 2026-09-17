export function trackChild(child) {
  let settled = child.exitCode !== null || child.signalCode !== null;
  const completion = new Promise((resolve) => {
    if (settled) return resolve();
    const finish = () => {
      settled = true;
      resolve();
    };
    child.once("exit", finish);
    child.once("error", finish);
  });
  return {
    child,
    async wait(timeoutMs) {
      if (settled || child.exitCode !== null || child.signalCode !== null) return true;
      let timer;
      try {
        return await Promise.race([
          completion.then(() => true),
          new Promise((resolve) => { timer = setTimeout(() => resolve(false), timeoutMs); }),
        ]);
      } finally {
        clearTimeout(timer);
      }
    },
  };
}

export async function stopChild(tracked, graceMs = 5000, killMs = 5000) {
  const forced = !await tracked.wait(graceMs);
  if (forced) {
    tracked.child.kill();
    if (!await tracked.wait(killMs)) throw new Error("子进程强制终止超时");
  }
  return { forced, exitCode: tracked.child.exitCode, signalCode: tracked.child.signalCode };
}

export function cleanupSucceeded(status, allowForced = false) {
  return (allowForced && status.forced)
    || (!status.forced && status.exitCode === 0 && status.signalCode === null);
}
