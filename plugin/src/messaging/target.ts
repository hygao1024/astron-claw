import { PLUGIN_ID } from "../constants.js";

// ---------------------------------------------------------------------------
// Normalize target address (like DingTalk's normalizeDingTalkTarget)
// ---------------------------------------------------------------------------
export function normalizeTarget(raw: string | null): string | null {
  if (!raw || typeof raw !== "string") return null;
  const s = raw.trim();

  // Strip plugin prefix
  const prefix = `${PLUGIN_ID}:`;
  let target = s.startsWith(prefix) ? s.slice(prefix.length) : s;

  // Handle user: prefix
  if (target.startsWith("user:")) {
    return target.slice("user:".length);
  }

  // Handle chat: prefix - keep it
  if (target.startsWith("chat:")) {
    return target;
  }

  // Plain ID
  return target;
}

export function isGroupTarget(target: string | null): boolean {
  return target?.startsWith("chat:") ?? false;
}
