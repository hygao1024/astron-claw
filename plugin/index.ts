import { PLUGIN_ID, PLUGIN_VERSION } from "./src/constants.js";
import { setRuntime, setLogger, logger } from "./src/runtime.js";
import { astronClawPlugin } from "./src/channel.js";
import { registerToolHooks } from "./src/hooks.js";

const plugin = {
  id: PLUGIN_ID,
  name: PLUGIN_ID,
  version: PLUGIN_VERSION,
  description: "AstronClaw channel plugin - connects chat clients via bridge server to OpenClaw.",

  register(api: any) {
    // Save runtime reference (like DingTalk's setDingTalkRuntime)
    setRuntime(api.runtime);
    setLogger(api.runtime?.logger ?? api.logger);

    // Register as a Channel (not a Service)
    api.registerChannel({ plugin: astronClawPlugin });

    // Register tool hooks for bridge integration
    registerToolHooks(api);

    logger.info(`AstronClaw v${PLUGIN_VERSION} registered as channel plugin`);
  },
};

export default plugin;
