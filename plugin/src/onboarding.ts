import { DEFAULT_ACCOUNT_ID, DEFAULT_BRIDGE_URL } from "./constants.js";
import { resolveAstronClawAccount } from "./config.js";
import { probeBridgeServer } from "./bridge/monitor.js";

// ---------------------------------------------------------------------------
// Onboarding (interactive configuration)
// ---------------------------------------------------------------------------
export const astronClawOnboarding = {
  getStatus: async () => {
    const account = resolveAstronClawAccount();
    if (!account) {
      return {
        configured: false,
        message: "AstronClaw is not configured. Bridge URL and token are required.",
        quickStartScore: 0,
      };
    }

    if (!account.bridge.token) {
      return {
        configured: false,
        message: "AstronClaw bridge token is not configured.",
        quickStartScore: 30,
      };
    }

    // Try probe
    const probe = await probeBridgeServer(account);
    if (probe.ok) {
      return {
        configured: true,
        message: `Connected to bridge: ${probe.name}`,
        quickStartScore: 100,
      };
    }

    return {
      configured: true,
      message: `Bridge configured but unreachable: ${probe.error}`,
      quickStartScore: 60,
    };
  },

  configure: async (interaction: any) => {
    const account = resolveAstronClawAccount();
    const hasCreds = account?.bridge?.token;

    if (hasCreds && interaction?.confirm) {
      const keep = await interaction.confirm("Bridge credentials already configured. Keep them?");
      if (keep) {
        return { cfg: { enabled: true }, accountId: DEFAULT_ACCOUNT_ID };
      }
    }

    let bridgeUrl = DEFAULT_BRIDGE_URL;
    let bridgeToken = "";

    if (interaction?.prompt) {
      // Show help text
      if (interaction.display) {
        interaction.display(
          "## AstronClaw Configuration\n\n" +
          "AstronClaw connects to a bridge server that relays messages from chat clients.\n\n" +
          "You need:\n" +
          "1. **Bridge URL** - WebSocket URL of the bridge server\n" +
          "2. **Bridge Token** - Authentication token for the bridge server\n"
        );
      }

      const urlInput = await interaction.prompt("Bridge WebSocket URL", { default: DEFAULT_BRIDGE_URL });
      if (urlInput) bridgeUrl = urlInput;

      const tokenInput = await interaction.prompt("Bridge authentication token");
      if (tokenInput) bridgeToken = tokenInput;
    }

    const cfg = {
      enabled: true,
      name: "AstronClaw",
      bridge: {
        url: bridgeUrl,
        token: bridgeToken,
      },
      allowFrom: ["*"],
    };

    return { cfg, accountId: DEFAULT_ACCOUNT_ID };
  },

  disable: async () => {
    return { cfg: { enabled: false } };
  },
};
