// MEMORYMASTER_MANAGED_OPENCODE_V1
// https://opencode.ai/docs/plugins/ and packages/plugin/src/index.ts (Hooks).
import { bounded, createAdapter, latestUser, textParts } from "../memorymaster/runtime.mjs";

export const MemoryMaster = async ({ directory, client }) => {
  const adapter = createAdapter(directory);
  return {
    "chat.message": async (input, output) => {
      adapter.remember(input?.sessionID, textParts(output?.parts), output?.message?.id);
    },
    "experimental.chat.system.transform": async (input, output) => bounded(async (signal) => {
      const sessionID = input?.sessionID;
      if (!sessionID) return;
      if (!adapter.has(sessionID) && client?.session?.messages) {
        const response = await client.session.messages({ path: { id: sessionID } });
        const user = latestUser(response?.data);
        if (user) adapter.remember(sessionID, user.prompt, user.id);
      }
      if (!signal.aborted) await adapter.context(sessionID, output?.system, false, signal);
    }),
    event: async ({ event } = {}) => {
      if (event?.type === "session.idle") adapter.capture(event.properties?.sessionID);
      if (event?.type === "session.deleted") adapter.forget(event.properties?.info?.id);
    },
  };
};
