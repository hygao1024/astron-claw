import { randomUUID } from "node:crypto";
import { writeFile } from "node:fs/promises";
import { join } from "node:path";

import {
  BOT_RPC_ERRORS,
  PLUGIN_ID,
} from "../constants.js";
import {
  HEARTBEAT_TOKEN,
  SILENT_REPLY_TOKEN,
  extensionForMime,
  isSilentReplyText,
  loadMedia,
} from "../sdk-compat.js";
import { getRuntime, logger, activeSessionCtx, activeDispatches, pendingToolCtx, recordChannelRuntimeState } from "../runtime.js";
import { inferMediaType } from "../bridge/media.js";
import { ensureInboundMediaDir, buildMediaFileName } from "./media-path.js";
import type { BridgeClient } from "../bridge/client.js";
import type { ResolvedAccount } from "../types.js";

// ---------------------------------------------------------------------------
// Silent reply token filtering (mirrors SDK internal isSilentReplyPrefixText
// with relaxed guard — drops the includes("_") check so that the first
// streaming delta "NO" is also recognised as a prefix of "NO_REPLY").
// ---------------------------------------------------------------------------
const SILENT_TOKENS = [SILENT_REPLY_TOKEN, HEARTBEAT_TOKEN];

function isSilentTokenPrefix(text: string): boolean {
  const normalized = text.trim().toUpperCase();
  if (!normalized) return false;
  if (/[^A-Z_]/.test(normalized)) return false;
  return SILENT_TOKENS.some(token => token.startsWith(normalized));
}

// ---------------------------------------------------------------------------
// Inbound message processing
// ---------------------------------------------------------------------------

export async function handleInboundMessage(msg: any, account: ResolvedAccount, bridgeClient: BridgeClient): Promise<void> {
  const rt = getRuntime();
  if (!rt) {
    logger.error("No runtime available, dropping inbound message");
    return;
  }

  // Bridge server sends JSON-RPC requests (session/prompt) from chat clients
  if (msg.jsonrpc === "2.0" && msg.method === "session/prompt") {
    await handleJsonRpcPrompt(msg, account, bridgeClient);
    return;
  }

  // Handle session cancel (user clicked Stop)
  if (msg.jsonrpc === "2.0" && msg.method === "session/cancel") {
    const cancelSessionId = msg.params?.sessionId;
    if (cancelSessionId) {
      const ac = activeDispatches.get(cancelSessionId);
      if (ac) {
        logger.info(`Cancelling dispatch for session ${cancelSessionId}`);
        ac.abort();
      } else {
        logger.debug(`No active dispatch for session ${cancelSessionId}, cancel is no-op`);
      }
    }
    return;
  }

  // Also handle direct message format (for future extensibility)
  if (msg && msg.type === "message") {
    await handleDirectMessage(msg, account, bridgeClient);
    return;
  }

  // Unknown message format
  logger.warn(`Unknown message format: ${JSON.stringify(msg).slice(0, 200)}`);
}

// ---------------------------------------------------------------------------
// JSON-RPC session/prompt handling
// ---------------------------------------------------------------------------
async function handleJsonRpcPrompt(rpcMsg: any, account: ResolvedAccount, bridgeClient: BridgeClient): Promise<void> {
  const rt = getRuntime();
  if (!rt) return;

  const requestId = rpcMsg.id;
  const params = rpcMsg.params ?? {};
  const sessionId = params.sessionId ?? "default";
  const prompt = params.prompt ?? {};
  const contentItems = prompt.content ?? [];

  // Extract text and media from content items
  const textParts: string[] = [];
  const mediaItems: any[] = [];
  for (const item of contentItems) {
    if (item.type === "text" && item.content) {
      textParts.push(item.content);
    } else if (item.type === "url" && item.content) {
      mediaItems.push(item);
    }
  }

  const messageText = textParts.join("\n");
  if (!messageText && mediaItems.length === 0) {
    logger.warn("Empty prompt received (no text or media), ignoring");
    return;
  }

  // Download media from S3 (public URL) and save locally via SDK
  const mediaPaths: string[] = [];
  const mediaTypes: string[] = [];
  const placeholders: string[] = [];
  const mediaDir = mediaItems.length > 0 ? await ensureInboundMediaDir() : "";
  for (const item of mediaItems) {
    const downloadUrl = item.content;
    if (!downloadUrl) continue;

    // Guard: only accept HTTP URLs (loadWebMedia has localRoots whitelist for local paths)
    if (!downloadUrl.startsWith("http")) {
      logger.error(`Invalid downloadUrl (expected HTTP URL): ${downloadUrl}`);
      continue;
    }

    try {
      let buffer: Buffer;
      let contentType: string;
      let fileName: string;

      // Extract file name from URL path (e.g. /sid/photo.jpg → "photo")
      let rawName: string;
      try {
        rawName = decodeURIComponent(
          new URL(downloadUrl).pathname.split("/").pop() ?? "file"
        );
      } catch {
        rawName = new URL(downloadUrl).pathname.split("/").pop() ?? "file";
      }
      const urlBaseName = rawName.replace(/\.[^.]+$/, "") || "file";

      try {
        // Primary: use host runtime media loader for cross-version compatibility.
        const loaded = await loadMedia(downloadUrl);
        buffer = loaded.buffer;
        contentType = loaded.contentType ?? "application/octet-stream";
        fileName = loaded.fileName ?? urlBaseName;
      } catch (sdkErr) {
        // Fallback: native fetch (bypasses SDK SSRF for trusted bridge URLs)
        logger.warn(`loadWebMedia blocked, falling back to native fetch: ${String(sdkErr)}`);
        const resp = await fetch(downloadUrl);
        if (!resp.ok) throw new Error(`HTTP ${resp.status} ${resp.statusText}`);
        buffer = Buffer.from(await resp.arrayBuffer());
        contentType = resp.headers.get("content-type") ?? "application/octet-stream";
        fileName = urlBaseName;
      }

      // Save buffer to SDK convention path: ~/.openclaw/media/inbound/{name}---{uuid}{ext}
      // Prefer extension from URL path (ignoring query params) over mime-based detection
      const urlExt = new URL(downloadUrl).pathname.match(/\.[a-zA-Z0-9]+$/)?.[0];
      const ext = urlExt || extensionForMime(contentType) || ".bin";
      const uuid = randomUUID();
      const savedName = buildMediaFileName(fileName, uuid, ext);
      const savedPath = join(mediaDir, savedName);
      await writeFile(savedPath, buffer);

      mediaPaths.push(savedPath);
      mediaTypes.push(contentType);

      // Semantic placeholder based on media type
      const type = inferMediaType(contentType);
      if (type === "image") placeholders.push("<media:image>");
      else if (type === "audio") placeholders.push("<media:audio>");
      else if (type === "video") placeholders.push("<media:video>");
      else placeholders.push(`<media:file name="${fileName}">`);

      logger.info(`Downloaded media ${downloadUrl} -> ${savedPath} (${contentType})`);
    } catch (err) {
      logger.error(`Failed to download media ${downloadUrl}: ${String(err)}`);
    }
  }

  const senderId = sessionId;
  const senderName = "User";
  const fromAddress = `${PLUGIN_ID}:user:${senderId}`;
  const toAddress = `${PLUGIN_ID}:user:${senderId}`;
  const peerId = senderId;

  // For media-only messages, use semantic placeholder so the message isn't dropped
  const mediaPlaceholder = placeholders.length > 0 ? placeholders.join(" ") : "";
  const effectiveText = messageText || mediaPlaceholder;
  if (!effectiveText) {
    logger.warn("Empty prompt received (no text, no media), ignoring");
    return;
  }

  logger.info(`Inbound prompt from session ${sessionId}: ${effectiveText.slice(0, 100)}${mediaPaths.length > 0 ? " [+media]" : ""}`);
  recordChannelRuntimeState(account.accountId, { lastInboundAt: Date.now() });

  // Resolve route via runtime SDK (same as DingTalk)
  let route: any;
  try {
    route = rt.channel?.routing?.resolveAgentRoute?.({
      cfg: rt.config?.loadConfig?.() ?? {},
      channel: PLUGIN_ID,
      accountId: account.accountId,
      peer: { kind: "dm", id: peerId },
    });
  } catch {
    route = { sessionKey: `${PLUGIN_ID}:${peerId}` };
  }

  // Always suffix with peerId (= sessionId) to guarantee session isolation.
  // SDK routing may return a shared sessionKey (e.g. based on accountId only),
  // which causes concurrent dispatches for different sessions to collide.
  const routeBase = route?.sessionKey ?? PLUGIN_ID;
  const sessionKey = routeBase.endsWith(peerId) ? routeBase : `${routeBase}:${peerId}`;

  // Load config once for all subsequent SDK calls
  const cfg = rt.config?.loadConfig?.() ?? {};

  // Build envelope body (same as DingTalk/adp-openclaw)
  let body = effectiveText;
  try {
    const envelopeOpts = rt.channel?.reply?.resolveEnvelopeFormatOptions?.(cfg);
    const formatted = rt.channel?.reply?.formatInboundEnvelope?.({
      channel: "AstronClaw",
      from: senderName,
      timestamp: Date.now(),
      body: effectiveText,
      chatType: "direct",
      sender: { id: senderId, name: senderName },
      envelope: envelopeOpts,
    });
    if (formatted) body = formatted;
  } catch {
    // Use raw text as fallback
  }

  // Resolve messages config for responsePrefix (following adp-openclaw pattern)
  let messagesConfig: any = {};
  try {
    messagesConfig = rt.channel?.reply?.resolveEffectiveMessagesConfig?.(cfg, route?.agentId) ?? {};
  } catch {
    // Fallback: no responsePrefix
  }

  // Build MsgContext and finalize through SDK (critical for BodyForAgent etc.)
  const rawCtx: any = {
    Body: body,
    RawBody: effectiveText,
    CommandBody: effectiveText,
    From: fromAddress,
    To: toAddress,
    SessionKey: sessionKey,
    AccountId: account.accountId,
    ChatType: "direct",
    ConversationLabel: senderName,
    SenderId: senderId,
    SenderName: senderName,
    Provider: PLUGIN_ID,
    Surface: PLUGIN_ID,
    MessageSid: requestId ?? randomUUID(),
    Timestamp: Date.now(),
    WasMentioned: true, // In DM, always treat as mentioned
    OriginatingChannel: PLUGIN_ID,
    OriginatingTo: toAddress,
    CommandAuthorized: true,
    // Media fields (supports multi-media via MediaPaths array)
    MediaPath: mediaPaths[0] ?? undefined,
    MediaPaths: mediaPaths.length > 0 ? mediaPaths : undefined,
    MediaType: mediaTypes[0] ?? undefined,
    MediaUrl: mediaPaths[0] ?? undefined,
    MediaUrls: mediaPaths.length > 0 ? mediaPaths : undefined,
  };

  // FIX: finalize through SDK — sets BodyForAgent, BodyForCommands, sanitizes
  // system tags, normalizes media types. Without this, SDK dispatch may behave
  // incorrectly (every other built-in channel calls this).
  const ctx = rt.channel?.reply?.finalizeInboundContext
    ? rt.channel.reply.finalizeInboundContext(rawCtx)
    : rawCtx;

  // Token-level streaming state (following adp-openclaw pattern)
  let lastPartialText = "";
  let allTurnsText = "";   // accumulated text across all turns for done event
  let chunkCount = 0;
  let finalSent = false;
  let turnIndex = 0;       // track agent turns for logging
  let turnTextLength = 0;  // cumulative text length within current turn

  const sendBridge = (payload: any, description: string): boolean => {
    const ok = bridgeClient.send(payload);
    if (!ok) {
      logger.warn(`[bridge] send dropped (${description}) session=${sessionId} request=${String(requestId ?? "n/a")}`);
    }
    return ok;
  };

  // Helper: send a chunk to the bridge
  const sendChunk = (text: string): void => {
    if (!text) return;
    const ok = sendBridge({
      jsonrpc: "2.0",
      method: "session/update",
      params: {
        sessionId,
        update: {
          sessionUpdate: "agent_message_chunk",
          content: { type: "text", text },
        },
      },
    }, "agent_message_chunk");
    if (ok) chunkCount++;
  };

  // Helper: send final completion to the bridge
  // Always send agent_message_final even when text is empty (e.g. tool-only
  // turns like file sends) so that the SSE stream receives a "done" frame.
  const sendFinal = (text: string): void => {
    if (finalSent) return;
    const ok = sendBridge({
      jsonrpc: "2.0",
      method: "session/update",
      params: {
        sessionId,
        update: {
          sessionUpdate: "agent_message_final",
          content: { type: "text", text: text || "" },
        },
      },
    }, "agent_message_final");
    if (ok) finalSent = true;
  };

  // Build dispatcher options (following adp-openclaw pattern):
  // - onPartialReply handles real-time token-level streaming
  // - deliver ignores "block" (already sent via onPartialReply) and only handles "final"
  const dispatcherOptions = {
    // FIX: pass responsePrefix from messagesConfig (same as adp-openclaw)
    responsePrefix: messagesConfig.responsePrefix,
    deliver: async (payload: any, info: any) => {
      const kind = info?.kind;
      const text = payload?.text ?? "";

      // === DEBUG LOG: full deliver payload (no truncation) ===
      logger.info(`[DIAG] deliver | turn=${turnIndex} kind=${kind} finalSent=${finalSent} chunkCount=${chunkCount} payload=${JSON.stringify(payload)} info=${JSON.stringify(info)}`);

      try {
        if (kind === "block") {
          // Ignore — onPartialReply already sent deltas in real-time
          return;
        }
        if (kind === "tool") {
          // Ignore — after_tool_call hook already sent tool results
          return;
        }
        // "final" or undefined — do NOT send done here.
        // SDK calls deliver(final) after each turn, not just at the end of
        // the entire dispatch.  Sending done prematurely closes the SSE
        // stream and causes all subsequent tool_call / tool_result events
        // from later turns to be silently dropped.
        // Instead, record the latest text and let the post-dispatch safety
        // net (after dispatchReplyWithBufferedBlockDispatcher resolves)
        // send the single done event.
        if (kind === "final" || kind === undefined) {
          if (text) {
            lastPartialText = text;
          }
        }
      } catch (sendErr) {
        logger.error(`deliver send error: ${String(sendErr)}`);
      }

      recordChannelRuntimeState(account.accountId, { lastOutboundAt: Date.now() });
    },
    onError: (err: any, info: any) => {
      logger.error(`Reply delivery error (${info?.kind}): ${String(err)}`);
    },
  };

  // Dispatch through the OpenClaw SDK using onPartialReply for token-level streaming.
  // onPartialReply receives cumulative text on each token; we compute the delta
  // and send only the new portion as a chunk (same approach as adp-openclaw).
  // Use per-request context key to prevent concurrent dispatches from overwriting
  // each other's session context. The requestId suffix ensures uniqueness.
  const ctxKey = `${sessionKey}:${requestId ?? randomUUID()}`;
  activeSessionCtx.set(ctxKey, { bridgeClient, sessionId, requestId });
  const abortController = new AbortController();
  activeDispatches.set(sessionId, abortController);
  try {
    if (rt.channel?.reply?.dispatchReplyWithBufferedBlockDispatcher) {
      await rt.channel.reply.dispatchReplyWithBufferedBlockDispatcher({
        ctx,
        cfg,
        dispatcherOptions,
        replyOptions: {
          disableBlockStreaming: false,
          abortSignal: abortController.signal,
          onPartialReply: async (payload: any) => {
            if (abortController.signal.aborted) return;
            const fullText = payload?.text ?? "";
            if (!fullText) return;

            // === DEBUG LOG: full onPartialReply payload (no truncation) ===
            if (fullText.length !== turnTextLength) {
              logger.info(`[DIAG] onPartialReply | turn=${turnIndex} chunkCount=${chunkCount} fullText=${fullText}`);
              turnTextLength = fullText.length;
            }

            // Detect turn reset (text got shorter = new agent turn)
            if (fullText.length < lastPartialText.length) {
              allTurnsText += lastPartialText;
              turnIndex++;
              turnTextLength = fullText.length;
              logger.info(`[DIAG] === NEW TURN ${turnIndex} === prevText_len=${lastPartialText.length} newText_len=${fullText.length}`);
              lastPartialText = "";
            }

            // Filter silent reply tokens (same pattern as SDK built-in channels)
            if (isSilentReplyText(fullText, SILENT_REPLY_TOKEN)) return;
            if (isSilentTokenPrefix(fullText)) return;

            // Calculate delta (new text since last send)
            let delta = fullText;
            if (fullText.startsWith(lastPartialText)) {
              delta = fullText.slice(lastPartialText.length);
            }

            if (!delta) return;
            lastPartialText = fullText;

            sendChunk(delta);
          },
        },
      });

      // FIX: After dispatch completes, ensure final is sent even if SDK
      // didn't call deliver with "final" (follows adp-openclaw pattern).
      logger.info(`[DIAG] dispatch COMPLETED | turns=${turnIndex + 1} totalChunks=${chunkCount} lastPartialText_len=${lastPartialText.length} allTurnsText_len=${allTurnsText.length} finalSent=${finalSent}`);
      if (!finalSent && (chunkCount > 0 || lastPartialText || allTurnsText)) {
        const finalText = allTurnsText + lastPartialText;
        logger.info(`dispatch completed without final deliver, sending final (chunks=${chunkCount})`);
        if (!isSilentReplyText(finalText, SILENT_REPLY_TOKEN)) {
          sendFinal(finalText);
        }
      }

      // FIX: Use finalSent/chunkCount as response signal instead of queuedFinal.
      // In ACP dispatch flow, queuedFinal is architecturally false because agent
      // text is delivered via block replies, not final replies. adp-openclaw also
      // does not rely on queuedFinal.
      const hasResponse = finalSent || chunkCount > 0;
      sendBridge({
        jsonrpc: "2.0",
        id: requestId,
        sessionId,
        result: { stopReason: hasResponse ? "end_turn" : "no_reply" },
      }, "session_result");

      if (!hasResponse) {
        logger.warn("No response generated for inbound message");
      }
    } else {
      logger.warn("dispatchReplyWithBufferedBlockDispatcher not available on runtime");
      sendBridge({
        jsonrpc: "2.0",
        id: requestId,
        sessionId,
        error: BOT_RPC_ERRORS.DISPATCH_FAILED,
      }, "dispatch_not_available");
    }
  } catch (err) {
    if (abortController.signal.aborted) {
      logger.info(`Dispatch aborted for session ${sessionId}`);
      if (!finalSent) {
        const partialText = allTurnsText + lastPartialText;
        sendFinal(partialText);
      }
      sendBridge({
        jsonrpc: "2.0",
        id: requestId,
        sessionId,
        result: { stopReason: "end_turn" },
      }, "dispatch_abort_result");
    } else {
      logger.error(`Failed to dispatch inbound message: ${String(err)}`);
      sendBridge({
        jsonrpc: "2.0",
        id: requestId,
        sessionId,
        error: { ...BOT_RPC_ERRORS.DISPATCH_FAILED, message: String(err) },
      }, "dispatch_error");
    }
  } finally {
    activeDispatches.delete(sessionId);
    activeSessionCtx.delete(ctxKey);
    // Sweep any leaked _pendingToolCtx entries for this request
    for (const [k, v] of pendingToolCtx) {
      if (v._sk === ctxKey) pendingToolCtx.delete(k);
    }
  }
}

// ---------------------------------------------------------------------------
// Direct message handling (for future extensibility)
// ---------------------------------------------------------------------------
async function handleDirectMessage(msg: any, account: ResolvedAccount, bridgeClient: BridgeClient): Promise<void> {
  const rt = getRuntime();
  if (!rt) return;

  const senderId = msg.from?.id ?? msg.senderId ?? "unknown";
  const senderName = msg.from?.name ?? msg.senderName ?? senderId;
  const messageText = msg.text ?? msg.content?.text ?? "";

  if (!messageText) return;

  logger.info(`Inbound direct message from ${senderName}(${senderId}): ${messageText.slice(0, 100)}`);
  recordChannelRuntimeState(account.accountId, { lastInboundAt: Date.now() });

  const fromAddress = `${PLUGIN_ID}:user:${senderId}`;
  const toAddress = `${PLUGIN_ID}:user:${senderId}`;

  let route: any;
  try {
    route = rt.routing?.resolveAgentRoute?.({
      peer: { kind: "dm", id: senderId },
    });
  } catch {
    route = { agentId: "main", sessionKey: `${PLUGIN_ID}:${senderId}` };
  }

  const envelope = {
    channelId: PLUGIN_ID,
    accountId: account.accountId,
    from: fromAddress,
    to: toAddress,
    senderDisplayName: senderName,
    messageId: msg.id ?? randomUUID(),
    timestamp: msg.timestamp ?? Date.now(),
  };

  const inboundCtx = {
    envelope,
    route: route ?? { agentId: "main", sessionKey: `${PLUGIN_ID}:${senderId}` },
    message: messageText,
  };

  const replyDispatcher = createReplyDispatcher({ senderId, chatType: "direct" }, account, bridgeClient);

  if (rt.channels?.dispatchInbound) {
    await rt.channels.dispatchInbound(inboundCtx, replyDispatcher);
  } else if (rt.dispatchInbound) {
    await rt.dispatchInbound(inboundCtx, replyDispatcher);
  } else {
    logger.warn("No dispatch method found on runtime, message dropped");
  }
}

// ---------------------------------------------------------------------------
// Reply Dispatcher (outbound delivery from OpenClaw engine back to user)
// ---------------------------------------------------------------------------
function createReplyDispatcher(
  data: { senderId: string; chatType: string; groupId?: string; raw?: any },
  account: ResolvedAccount,
  bridgeClient: BridgeClient,
) {
  return {
    deliver: async (payload: any) => {
      const to = data.chatType === "group" ? data.groupId : data.senderId;
      const text = typeof payload === "string"
        ? payload
        : (payload?.text ?? payload?.content?.text ?? "");

      if (!text && !payload?.media) return;

      bridgeClient.send({
        type: "reply",
        to,
        chatType: data.chatType,
        msgType: "text",
        content: { text },
        replyTo: data.raw?.id,
      });

      recordChannelRuntimeState(account.accountId, { lastOutboundAt: Date.now() });
    },
    onError: (err: any, info: any) => {
      logger.error(`Reply delivery error: ${String(err)}`, info);
    },
  };
}
