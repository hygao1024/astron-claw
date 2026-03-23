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

/**
 * Resolve a specific account by accountId from the plugin config.
 * Supports multi-account format: channels.astron-claw.accounts.<accountId>
 * Falls back to single-account (top-level bridge) when no accounts block exists.
 */
export function resolveAstronClawAccountFromCfg(cfg: any, accountId?: string): ResolvedAccount {
  const pluginCfg = cfg?.channels?.[PLUGIN_ID]
    ?? cfg?.plugins?.entries?.[PLUGIN_ID]?.config
    ?? {};

  // Multi-account: merge per-account config over top-level defaults
  const accounts = pluginCfg.accounts;
  const effectiveId = accountId ?? DEFAULT_ACCOUNT_ID;
  const acctOverride = accounts?.[effectiveId] ?? {};

  const bridge = acctOverride.bridge ?? pluginCfg.bridge ?? {};
  const retry = acctOverride.retry ?? pluginCfg.retry ?? {};
  const media = acctOverride.media ?? pluginCfg.media ?? {};
  const allowFrom = acctOverride.allowFrom ?? pluginCfg.allowFrom;

  return {
    accountId: effectiveId,
    enabled: (acctOverride.enabled ?? pluginCfg.enabled) !== false,
    name: readStr(acctOverride.name) ?? readStr(pluginCfg.name) ?? effectiveId,
    bridge: {
      url: readStr(bridge.url) ?? DEFAULT_BRIDGE_URL,
      token: readStr(bridge.token) ?? "",
    },
    retry: {
      baseMs: readNum(retry.baseMs) ?? DEFAULT_RETRY_BASE_MS,
      maxMs: readNum(retry.maxMs) ?? DEFAULT_RETRY_MAX_MS,
      maxAttempts: readNum(retry.maxAttempts) ?? DEFAULT_RETRY_MAX_ATTEMPTS,
    },
    allowFrom: Array.isArray(allowFrom) ? allowFrom : ["*"],
    media: {
      maxSize: readNum(media.maxSize) ?? MEDIA_MAX_SIZE_DEFAULT,
      allowedTypes: Array.isArray(media.allowedTypes) ? media.allowedTypes : MEDIA_ALLOWED_TYPES_DEFAULT,
    },
    tokenSource: bridge.token ? "config" : "none",
  };
}

/**
 * List all account IDs from the plugin config.
 * Returns keys from accounts block if present, otherwise falls back based on
 * whether a single-account bridge config exists.
 */
export function listAstronClawAccountIds(cfg: any): string[] {
  const pluginCfg = cfg?.channels?.[PLUGIN_ID]
    ?? cfg?.plugins?.entries?.[PLUGIN_ID]?.config
    ?? {};

  const accounts = pluginCfg.accounts;
  if (accounts && typeof accounts === "object") {
    const keys = Object.keys(accounts);
    if (keys.length > 0) return keys;
  }

  // Fallback: single account if bridge config exists
  if (pluginCfg.bridge?.url || pluginCfg.bridge?.token) {
    return [DEFAULT_ACCOUNT_ID];
  }
  return [];
}

export function resolveAstronClawAccount(accountId?: string): ResolvedAccount | null {
  const rt = getRuntime();
  if (!rt) return null;

  let cfg: any;
  try {
    cfg = rt.config?.loadConfig?.() ?? {};
  } catch {
    cfg = {};
  }
  return resolveAstronClawAccountFromCfg(cfg, accountId);
}
