// MEMORYMASTER_MANAGED_OPENCODE_V1
// OpenCode 2.1.1 PluginModule.load reads default.id and default.setup (not server).
// https://opencode.ai/v2/docs/build/plugins
import { setupV2 } from "../memorymaster/runtime.mjs";

export default { id: "memorymaster", setup: setupV2 };
