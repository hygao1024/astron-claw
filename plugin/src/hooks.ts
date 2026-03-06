import { activeSessionCtx, pendingToolCtx, toolCtxKey } from "./runtime.js";

// ---------------------------------------------------------------------------
// SDK event hooks (before_tool_call / after_tool_call)
// ---------------------------------------------------------------------------
export function registerToolHooks(api: any): void {
  // Hook: before_tool_call – send tool input to bridge
  api.on("before_tool_call", (event: any, ctx: any) => {
    const sessionCtx = activeSessionCtx.get(ctx.sessionKey);
    if (!sessionCtx) return;
    // Stash for after_tool_call which lacks ctx.sessionKey (SDK bug)
    pendingToolCtx.set(toolCtxKey(event.toolName, event.params), { ...sessionCtx, _sk: ctx.sessionKey });
    const { bridgeClient, sessionId } = sessionCtx;
    const inputText = typeof event.params === "object"
      ? JSON.stringify(event.params) : String(event.params ?? "");
    bridgeClient.send({
      jsonrpc: "2.0",
      method: "session/update",
      params: {
        sessionId,
        update: {
          sessionUpdate: "tool_call",
          title: event.toolName || "tool",
          status: "running",
          content: inputText,
        },
      },
    });
  });

  // Hook: after_tool_call – send tool result to bridge
  // NOTE: SDK bug – ctx.sessionKey is undefined in after_tool_call,
  // so we look up via _pendingToolCtx keyed on toolName+params.
  api.on("after_tool_call", (event: any, ctx: any) => {
    // SDK fires after_tool_call twice; only handle the complete one (has durationMs)
    if (event.durationMs === undefined) return;
    const key = toolCtxKey(event.toolName, event.params);
    const sessionCtx = activeSessionCtx.get(ctx.sessionKey) || pendingToolCtx.get(key);
    pendingToolCtx.delete(key); // cleanup
    if (!sessionCtx) return;
    const { bridgeClient, sessionId } = sessionCtx;
    const resultText = event.error
      ? `Error: ${event.error}`
      : (typeof event.result === "string" ? event.result : JSON.stringify(event.result ?? ""));
    bridgeClient.send({
      jsonrpc: "2.0",
      method: "session/update",
      params: {
        sessionId,
        update: {
          sessionUpdate: "tool_result",
          title: event.toolName || "tool",
          status: event.error ? "error" : "completed",
          content: resultText,
        },
      },
    });
  });
}
