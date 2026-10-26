// MEMORYMASTER_MANAGED_OPENCODE_V1
// Transport only. All recall policy stays in memorymaster.recall.remote.
import { spawn } from "node:child_process";

const python = __MEMORYMASTER_PYTHON_JSON__;
const projectRoot = __MEMORYMASTER_ROOT_JSON__;
const prefix = "[MemoryMaster recall]\n";

function command(module, payload, extra = [], timeout = 2800) {
  return new Promise((resolve) => {
    let result = "";
    let settled = false;
    let child;
    const finish = (value = "") => {
      if (settled) return;
      settled = true;
      clearTimeout(timer);
      resolve(value);
    };
    const timer = setTimeout(() => { child?.kill(); finish(); }, timeout);
    try {
      child = spawn(python, ["-m", module, "--cli", "opencode", ...extra], {
        cwd: projectRoot, windowsHide: true, stdio: ["pipe", "pipe", "ignore"],
      });
      child.on("error", () => finish());
      child.stdin.on("error", () => {});
      child.stdout.setEncoding("utf8");
      child.stdout.on("data", (chunk) => {
        result += chunk;
        if (result.length > 65536) { child.kill(); finish(); }
      });
      child.on("close", (code) => finish(code === 0 ? result : ""));
      child.stdin.end(JSON.stringify(payload));
    } catch { finish(); }
  });
}

export function textParts(parts) {
  if (!Array.isArray(parts)) return "";
  return parts.filter((p) => p?.type === "text" && !p.synthetic && !p.ignored && typeof p.text === "string")
    .map((p) => p.text).join("\n");
}

export function latestUser(messages) {
  if (!Array.isArray(messages)) return undefined;
  for (let i = messages.length - 1; i >= 0; i--) {
    const message = messages[i];
    if ((message?.info?.role ?? message?.role ?? message?.type) !== "user") continue;
    const prompt = typeof message.text === "string" ? message.text : textParts(message.parts ?? message.content);
    if (prompt) return { prompt, id: message.info?.id ?? message.id ?? prompt };
  }
}

export function createAdapter(cwd) {
  const sessions = new Map();
  function remember(sessionID, prompt, id = prompt) {
    if (typeof sessionID !== "string" || !sessionID || typeof prompt !== "string" || !prompt.trim()) return;
    const previous = sessions.get(sessionID);
    if (previous?.id === id && previous?.prompt === prompt) return;
    if (sessions.size >= 1000) sessions.delete(sessions.keys().next().value);
    sessions.set(sessionID, { prompt: prompt.slice(0, 16000), id });
  }
  async function context(sessionID, system, structured = false, signal) {
    if (!cwd || !sessionID || !Array.isArray(system)) return;
    const state = sessions.get(sessionID);
    if (!state) return;
    // Reuse for tool continuations: system context is rebuilt for each model call.
    state.pending ??= command("memorymaster.surfaces.hook_recall", {
      prompt: state.prompt, cwd, session_id: sessionID,
    });
    const block = await state.pending;
    if (signal?.aborted || !block.startsWith(prefix)) return;
    for (let i = system.length - 1; i >= 0; i--) {
      const text = structured ? system[i]?.text : system[i];
      if (typeof text === "string" && text.startsWith(prefix)) system.splice(i, 1);
    }
    system.push(structured ? { type: "text", text: block } : block);
    // Receipt only after the actual prompt append; never include recalled content.
    void command("memorymaster.surfaces.hook_events", {}, ["--event", "injection", "--count", "1"]);
  }
  function capture(sessionID) {
    if (!cwd || !sessions.has(sessionID)) return;
    void command("memorymaster.surfaces.hook_capture", { session_id: sessionID, cwd }, [], 10000);
  }
  return { remember, context, capture, has: (id) => sessions.has(id), forget: (id) => sessions.delete(id) };
}

export async function bounded(operation, timeout = 3000) {
  let timer;
  const controller = new AbortController();
  try {
    return await Promise.race([operation(controller.signal), new Promise((resolve) => {
      timer = setTimeout(() => { controller.abort(); resolve(); }, timeout);
    })]);
  } catch { return undefined; }
  finally { clearTimeout(timer); }
}

// Shared native setup keeps the default and explicit-v2 installations equivalent.
export async function setupV2(ctx) {
  const cwd = ctx.location?.directory;
  if (!cwd || !ctx.session?.hook) return;
  const adapter = createAdapter(cwd);
  const registrations = [];
  const controller = new AbortController();
  const cleanup = async () => {
    controller.abort();
    await Promise.allSettled(registrations.map((r) => r.dispose()));
  };
  try {
    registrations.push(await ctx.session.hook("prompt", async (event) => {
      adapter.remember(event?.sessionID, event?.prompt?.text, event?.messageID);
    }));
    registrations.push(await ctx.session.hook("context", async (event) => bounded(async (signal) => {
      const user = latestUser(event?.messages);
      if (user) adapter.remember(event?.sessionID, user.prompt, user.id);
      await adapter.context(event?.sessionID, event?.system, true, signal);
    })));
  } catch { await cleanup(); return; }
  if (ctx.event?.subscribe) void (async () => {
    while (!controller.signal.aborted) {
      try {
        for await (const event of ctx.event.subscribe({ signal: controller.signal })) {
          if (controller.signal.aborted) break;
          const sessionID = event?.data?.sessionID;
          if (!adapter.has(sessionID)) continue;
          if (["session.execution.succeeded", "session.execution.failed", "session.execution.interrupted"].includes(event.type)) {
            adapter.capture(sessionID);
          }
          if (event.type === "session.deleted") adapter.forget(sessionID);
        }
      } catch { /* disconnected streams retry without breaking the host */ }
      if (!controller.signal.aborted) await new Promise((resolve) => {
        const done = () => {
          clearTimeout(timer);
          controller.signal.removeEventListener("abort", done);
          resolve();
        };
        const timer = setTimeout(done, 1000);
        timer.unref?.();
        controller.signal.addEventListener("abort", done, { once: true });
      });
    }
  })();
  return cleanup;
}
