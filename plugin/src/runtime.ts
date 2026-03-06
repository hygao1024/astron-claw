import { PLUGIN_ID } from "./constants.js";
import type { BridgeClient } from "./bridge/client.js";
import type { ChannelRuntimeState, SessionContext } from "./types.js";

// ---------------------------------------------------------------------------
// Logger
// ---------------------------------------------------------------------------
let _logger: any = console;

export function setLogger(l: any): void {
  _logger = l ?? console;
}

export function getRawLogger(): any {
  return _logger;
}

export const logger = {
  info: (...args: any[]) => _logger.info?.("[AstronClaw]", ...args),
  warn: (...args: any[]) => _logger.warn?.("[AstronClaw]", ...args),
  error: (...args: any[]) => _logger.error?.("[AstronClaw]", ...args),
  debug: (...args: any[]) => _logger.debug?.("[AstronClaw]", ...args),
};

// ---------------------------------------------------------------------------
// Runtime singleton (holds PluginRuntime reference)
// ---------------------------------------------------------------------------
let _runtime: any = null;

export function setRuntime(rt: any): void {
  _runtime = rt;
}

export function getRuntime(): any {
  return _runtime;
}

// ---------------------------------------------------------------------------
// Session context (per-sessionKey)
// ---------------------------------------------------------------------------
export const activeSessionCtx = new Map<string, SessionContext>();

// ---------------------------------------------------------------------------
// Pending tool context (SDK workaround)
// Map<toolCtxKey, SessionContext & { _sk: string }> — per-invocation mapping
// for after_tool_call (SDK bug: ctx.sessionKey is undefined there).
// Key = "toolName\0paramsJSON", set in before_tool_call, consumed in after_tool_call.
// ---------------------------------------------------------------------------
export const pendingToolCtx = new Map<string, SessionContext & { _sk: string }>();

export function toolCtxKey(toolName: string, params: unknown): string {
  return `${toolName}\0${JSON.stringify(params ?? "")}`;
}

// ---------------------------------------------------------------------------
// Channel runtime state (per-accountId)
// ---------------------------------------------------------------------------
const runtimeState = new Map<string, ChannelRuntimeState>();

function defaultRuntimeState(): ChannelRuntimeState {
  return {
    running: false,
    lastStartAt: null,
    lastStopAt: null,
    lastError: null,
    lastInboundAt: null,
    lastOutboundAt: null,
  };
}

export function recordChannelRuntimeState(accountId: string, updates: Partial<ChannelRuntimeState>): void {
  const key = `${PLUGIN_ID}:${accountId}`;
  const current = runtimeState.get(key) ?? defaultRuntimeState();
  Object.assign(current, updates);
  runtimeState.set(key, current);
}

export function getChannelRuntimeState(accountId: string): ChannelRuntimeState {
  return runtimeState.get(`${PLUGIN_ID}:${accountId}`) ?? defaultRuntimeState();
}

// ---------------------------------------------------------------------------
// Active bridge clients (per-accountId)
// ---------------------------------------------------------------------------
export const activeBridgeClients = new Map<string, BridgeClient>();
