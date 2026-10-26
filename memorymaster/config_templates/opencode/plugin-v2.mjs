// MEMORYMASTER_MANAGED_OPENCODE_V1
// https://opencode.ai/v2/docs/build/plugins and migrate-v1/.
// Plugin.define returns this plain {id, setup} definition; no package import required.
import { bounded, createAdapter, latestUser } from "../memorymaster/runtime.mjs";

export default {
  id: "memorymaster",
  async setup(ctx) {
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
  },
};
