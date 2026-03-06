import {
  PLUGIN_ID,
  DEFAULT_ACCOUNT_ID,
  DEFAULT_BRIDGE_URL,
  DEFAULT_RETRY_BASE_MS,
  DEFAULT_RETRY_MAX_MS,
  DEFAULT_RETRY_MAX_ATTEMPTS,
  MEDIA_MAX_SIZE_DEFAULT,
  MEDIA_ALLOWED_TYPES_DEFAULT,
} from "./constants.js";
import { getRuntime } from "./runtime.js";
import type { ResolvedAccount } from "./types.js";

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------
function readStr(v: unknown): string | undefined {
  return typeof v === "string" && v.trim() ? v.trim() : undefined;
}

function readNum(v: unknown): number | undefined {
  return typeof v === "number" && Number.isFinite(v) ? v : undefined;
}

// ---------------------------------------------------------------------------
// Account resolution
// ---------------------------------------------------------------------------
export function resolveAstronClawAccountFromCfg(cfg: any): ResolvedAccount {
  const pluginCfg = cfg?.channels?.[PLUGIN_ID]
    ?? cfg?.plugins?.entries?.[PLUGIN_ID]?.config
    ?? {};

  const bridge = pluginCfg.bridge ?? {};
  const retry = pluginCfg.retry ?? {};
  const media = pluginCfg.media ?? {};

  return {
    accountId: DEFAULT_ACCOUNT_ID,
    enabled: pluginCfg.enabled !== false,
    name: readStr(pluginCfg.name) ?? "AstronClaw",
    bridge: {
      url: readStr(bridge.url) ?? DEFAULT_BRIDGE_URL,
      token: readStr(bridge.token) ?? "",
    },
    retry: {
      baseMs: readNum(retry.baseMs) ?? DEFAULT_RETRY_BASE_MS,
      maxMs: readNum(retry.maxMs) ?? DEFAULT_RETRY_MAX_MS,
      maxAttempts: readNum(retry.maxAttempts) ?? DEFAULT_RETRY_MAX_ATTEMPTS,
    },
    allowFrom: Array.isArray(pluginCfg.allowFrom) ? pluginCfg.allowFrom : ["*"],
    media: {
      maxSize: readNum(media.maxSize) ?? MEDIA_MAX_SIZE_DEFAULT,
      allowedTypes: Array.isArray(media.allowedTypes) ? media.allowedTypes : MEDIA_ALLOWED_TYPES_DEFAULT,
    },
    tokenSource: bridge.token ? "config" : "none",
  };
}

export function resolveAstronClawAccount(): ResolvedAccount | null {
  const rt = getRuntime();
  if (!rt) return null;

  let cfg: any;
  try {
    cfg = rt.config?.loadConfig?.() ?? {};
  } catch {
    cfg = {};
  }
  return resolveAstronClawAccountFromCfg(cfg);
}
