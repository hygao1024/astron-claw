import { PLUGIN_ID, DEFAULT_ACCOUNT_ID } from "./constants.js";
import { getRuntime, logger, recordChannelRuntimeState, activeBridgeClients } from "./runtime.js";
import { resolveAstronClawAccountFromCfg } from "./config.js";
import { astronClawOnboarding } from "./onboarding.js";
import { normalizeTarget } from "./messaging/target.js";
import { sendTextMessage, sendMediaMessage } from "./messaging/outbound.js";
import type { ResolvedAccount } from "./types.js";

// ---------------------------------------------------------------------------
// ChannelPlugin definition (following DingTalk pattern)
// ---------------------------------------------------------------------------
export const astronClawPlugin = {
  id: PLUGIN_ID,

  meta: {
    id: PLUGIN_ID,
    label: "AstronClaw",
    selectionLabel: "AstronClaw",
    blurb: "Bridge-based channel connecting web chat clients to OpenClaw via WebSocket.",
    systemImage: "message.fill",
  },

  // --- Capabilities ---
  capabilities: {
    chatTypes: ["direct"],
    media: true,
    blockStreaming: true,
    reactions: false,
    threads: false,
    nativeCommands: false,
  },

  // --- Onboarding ---
  onboarding: astronClawOnboarding,

  // --- Config (account discovery — required by framework) ---
  config: {
    listAccountIds: (cfg: any) => {
      const pluginCfg = cfg?.channels?.[PLUGIN_ID]
        ?? cfg?.plugins?.entries?.[PLUGIN_ID]?.config
        ?? {};
      // Return account if bridge URL is configured (token checked by isConfigured)
      if (pluginCfg.bridge?.url || pluginCfg.bridge?.token) {
        return [DEFAULT_ACCOUNT_ID];
      }
      return [];
    },

    resolveAccount: (cfg: any, _accountId: string) => {
      return resolveAstronClawAccountFromCfg(cfg);
    },

    defaultAccountId: (_cfg: any) => DEFAULT_ACCOUNT_ID,

    isConfigured: (account: ResolvedAccount) => {
      return !!(account?.bridge?.token && account?.bridge?.url);
    },

    describeAccount: (account: ResolvedAccount) => ({
      accountId: account?.accountId ?? DEFAULT_ACCOUNT_ID,
      name: account?.name ?? "AstronClaw",
      enabled: account?.enabled !== false,
      configured: !!(account?.bridge?.token && account?.bridge?.url),
      tokenSource: account?.bridge?.token ? "config" : "none",
    }),

    resolveAllowFrom: ({ cfg }: { cfg: any }) => {
      const account = resolveAstronClawAccountFromCfg(cfg);
      return account.allowFrom.map((entry: any) => String(entry));
    },

    formatAllowFrom: ({ allowFrom }: { allowFrom: string[] }) =>
      allowFrom
        .map((entry) => String(entry).trim())
        .filter(Boolean)
        .map((entry) => entry.replace(new RegExp(`^${PLUGIN_ID}:(?:user:)?`, "i"), "")),
  },

  // --- Outbound ---
  outbound: {
    deliveryMode: "direct",
    textChunkLimit: 4000,

    resolveTarget: ({ to, allowFrom, mode }: { to?: string; allowFrom?: string[]; mode?: string }) => {
      const trimmed = to?.trim() ?? "";
      const allowListRaw = (allowFrom ?? []).map((e) => String(e).trim()).filter(Boolean);
      const hasWildcard = allowListRaw.includes("*");
      const allowList = allowListRaw
        .filter((e) => e !== "*")
        .map((e) => normalizeTarget(e))
        .filter((e): e is string => !!e);

      if (trimmed) {
        const normalized = normalizeTarget(trimmed);
        if (!normalized) {
          if ((mode === "implicit" || mode === "heartbeat") && allowList.length > 0) {
            return { ok: true, to: allowList[0] };
          }
          return {
            ok: false,
            error: new Error(`Invalid target: ${trimmed}. Use <userId> or set allowFrom.`),
          };
        }

        if (mode === "explicit") {
          return { ok: true, to: normalized };
        }

        if (mode === "implicit" || mode === "heartbeat") {
          if (hasWildcard || allowList.length === 0) {
            return { ok: true, to: normalized };
          }
          if (allowList.includes(normalized)) {
            return { ok: true, to: normalized };
          }
          return { ok: true, to: allowList[0] };
        }

        return { ok: true, to: normalized };
      }

      // No target specified
      if (allowList.length > 0) {
        return { ok: true, to: allowList[0] };
      }

      return {
        ok: false,
        error: new Error(`No target specified. Set allowFrom or provide a target.`),
      };
    },

    sendText: async ({ to, text, cfg }: { to: string; text: string; cfg: any }) => {
      const account = resolveAstronClawAccountFromCfg(cfg);
      const bridgeClient = activeBridgeClients.get(account.accountId ?? DEFAULT_ACCOUNT_ID);
      if (!bridgeClient) throw new Error("No active bridge connection");
      await sendTextMessage(to, text, { account, bridgeClient });
      return { channel: PLUGIN_ID, messageId: "", chatId: to };
    },

    sendMedia: async ({ to, text, mediaUrl, cfg }: { to: string; text?: string; mediaUrl?: string; cfg: any }) => {
      const account = resolveAstronClawAccountFromCfg(cfg);
      const bridgeClient = activeBridgeClients.get(account.accountId ?? DEFAULT_ACCOUNT_ID);
      if (!bridgeClient) throw new Error("No active bridge connection");
      if (mediaUrl) {
        await sendMediaMessage(to, mediaUrl, { text }, { account, bridgeClient });
      } else if (text) {
        await sendTextMessage(to, text, { account, bridgeClient });
      }
      return { channel: PLUGIN_ID, messageId: "", chatId: to };
    },
  },

  // --- Messaging (target resolution for message tool) ---
  messaging: {
    normalizeTarget: (target: string) => {
      const trimmed = target?.trim();
      if (!trimmed) return undefined;
      return normalizeTarget(trimmed);
    },
    targetResolver: {
      looksLikeId: (id: string) => {
        const trimmed = id?.trim();
        if (!trimmed) return false;
        // Accept: astron-claw:user:xxx, user:xxx, raw UUID, chat:xxx
        const prefixPattern = new RegExp(`^${PLUGIN_ID}:`, "i");
        return prefixPattern.test(trimmed)
          || trimmed.startsWith("user:")
          || trimmed.startsWith("chat:")
          || /^[a-zA-Z0-9_-]+$/.test(trimmed);
      },
      hint: `<userId> or ${PLUGIN_ID}:user:<userId>`,
    },
  },

  // --- Security ---
  security: {
    resolveDmPolicy: ({ cfg }: { cfg: any }) => {
      const account = resolveAstronClawAccountFromCfg(cfg);
      return {
        policy: "allowlist",
        allowFrom: account.allowFrom ?? ["*"],
        policyPath: `channels.${PLUGIN_ID}.allowFrom`,
        normalizeEntry: (raw: any) => {
          if (typeof raw !== "string") return String(raw);
          return raw.replace(`${PLUGIN_ID}:user:`, "").replace(`${PLUGIN_ID}:`, "");
        },
      };
    },
  },

  // --- Gateway ---
  gateway: {
    startAccount: async (ctx: any) => {
      const { account, abortSignal } = ctx;
      ctx.log?.info?.(`[${account.accountId}] starting AstronClaw bridge connection`);

      const { probeBridgeServer } = await import("./bridge/monitor.js");
      const probe = await probeBridgeServer(account);
      if (probe.ok) {
        ctx.log?.info?.(`[${account.accountId}] bridge probe OK: ${probe.name}`);
      } else {
        ctx.log?.warn?.(`[${account.accountId}] bridge probe failed: ${probe.error} (will try connecting anyway)`);
      }

      const { monitorBridgeProvider } = await import("./bridge/monitor.js");
      return monitorBridgeProvider(account, abortSignal);
    },

    logoutAccount: async ({ account, cfg }: { account: ResolvedAccount; cfg: any }) => {
      logger.info(`Logging out account: ${account.accountId}`);
      const client = activeBridgeClients.get(account.accountId);
      if (client) {
        client.stop();
        activeBridgeClients.delete(account.accountId);
      }
      recordChannelRuntimeState(account.accountId, {
        running: false,
        lastStopAt: Date.now(),
      });

      // Clear credentials from config (write to channels.*, not plugins.entries.*)
      const rt = getRuntime();
      if (rt?.config?.writeConfigFile) {
        const nextCfg = { ...cfg };
        const channelCfg = cfg?.channels?.[PLUGIN_ID] ?? {};
        const nextChannelCfg = { ...channelCfg };
        delete nextChannelCfg.bridge;

        if (Object.keys(nextChannelCfg).length > 0) {
          nextCfg.channels = { ...nextCfg.channels, [PLUGIN_ID]: nextChannelCfg };
        } else {
          const nextChannels = { ...nextCfg.channels };
          delete nextChannels[PLUGIN_ID];
          if (Object.keys(nextChannels).length > 0) {
            nextCfg.channels = nextChannels;
          } else {
            delete nextCfg.channels;
          }
        }
        await rt.config.writeConfigFile(nextCfg);
      }

      return { cleared: true, loggedOut: true };
    },
  },

  // --- Status ---
  status: {
    defaultRuntime: {
      accountId: DEFAULT_ACCOUNT_ID,
      running: false,
      lastStartAt: null,
      lastStopAt: null,
      lastError: null,
    },

    probeAccount: async ({ account, timeoutMs }: { account: ResolvedAccount; timeoutMs?: number }) => {
      const { probeBridgeServer } = await import("./bridge/monitor.js");
      return probeBridgeServer(account);
    },

    buildAccountSnapshot: ({ account, runtime, probe }: { account: ResolvedAccount; runtime?: any; probe?: any }) => {
      const configured = !!(account?.bridge?.token && account?.bridge?.url);
      return {
        accountId: account.accountId,
        name: account.name ?? "AstronClaw",
        enabled: account.enabled !== false,
        configured,
        tokenSource: account.bridge?.token ? "config" : "none",
        running: runtime?.running ?? false,
        lastStartAt: runtime?.lastStartAt ?? null,
        lastStopAt: runtime?.lastStopAt ?? null,
        lastError: runtime?.lastError ?? null,
        mode: "bridge",
        probe,
      };
    },

    collectStatusIssues: (accounts: any[]) => {
      const issues: any[] = [];
      for (const account of accounts) {
        const accountId = account.accountId ?? DEFAULT_ACCOUNT_ID;
        if (!account.configured) {
          issues.push({
            channel: PLUGIN_ID,
            accountId,
            kind: "config",
            message: "Bridge credentials (url/token) not configured",
          });
        }
      }
      return issues;
    },
  },
};
