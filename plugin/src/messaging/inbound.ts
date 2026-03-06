import { randomUUID } from "node:crypto";
import { writeFileSync, mkdirSync } from "node:fs";
import { join } from "node:path";
import { tmpdir } from "node:os";

import { PLUGIN_ID } from "../constants.js";
import { getRuntime, logger, activeSessionCtx, pendingToolCtx, recordChannelRuntimeState } from "../runtime.js";
import { downloadMediaFromBridge } from "../bridge/media.js";
import type { BridgeClient } from "../bridge/client.js";
import type { ResolvedAccount } from "../types.js";

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
    if (item.type === "text" && item.text) {
      textParts.push(item.text);
    } else if (item.type === "media" && item.media) {
      mediaItems.push(item);
    }
  }

  const messageText = textParts.join("\n");
  if (!messageText && mediaItems.length === 0) {
    logger.warn("Empty prompt received (no text or media), ignoring");
    return;
  }

  // Download media from bridge and save locally
  let mediaPath: string | null = null;
  let mediaType: string | null = null;
  let mediaUrl: string | null = null;
  if (mediaItems.length > 0) {
    const firstMedia = mediaItems[0];
    const mediaInfo = firstMedia.media;
    const mediaId = mediaInfo.mediaId;
    if (mediaId) {
      try {
        const { buffer, contentType: ct, fileName } = await downloadMediaFromBridge(account, mediaId);
        const dir = join(tmpdir(), "astron-claw-media");
        mkdirSync(dir, { recursive: true });
        const savedPath = join(dir, `${randomUUID()}_${fileName}`);
        writeFileSync(savedPath, buffer);
        mediaPath = savedPath;
        mediaType = ct;
        mediaUrl = savedPath;
        logger.info(`Downloaded media ${mediaId} -> ${savedPath} (${ct})`);
      } catch (err) {
        logger.error(`Failed to download media ${mediaId}: ${String(err)}`);
      }
    }
  }

  const senderId = sessionId;
  const senderName = "User";
  const fromAddress = `${PLUGIN_ID}:user:${senderId}`;
  const toAddress = `${PLUGIN_ID}:user:${senderId}`;
  const peerId = senderId;

  // For media-only messages, use a placeholder text so the message isn't dropped
  const effectiveText = messageText || (mediaPath ? "[Image]" : "");
  if (!effectiveText) {
    logger.warn("Empty prompt received (no text, no media), ignoring");
    return;
  }

  logger.info(`Inbound prompt from session ${sessionId}: ${effectiveText.slice(0, 100)}${mediaPath ? " [+media]" : ""}`);
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

  const sessionKey = route?.sessionKey ?? `${PLUGIN_ID}:${peerId}`;

  // Build envelope body (same as DingTalk's formatInboundEnvelope)
  let body = effectiveText;
  try {
    const cfg = rt.config?.loadConfig?.() ?? {};
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

  // Build MsgContext (same structure as DingTalk's buildInboundContext)
  const ctx: any = {
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
    // Media fields (same as Matrix/Zalo pattern)
    MediaPath: mediaPath ?? undefined,
    MediaType: mediaType ?? undefined,
    MediaUrl: mediaUrl ?? undefined,
  };

  // Token-level streaming state (following adp-openclaw pattern)
  let lastPartialText = "";
  let chunkCount = 0;
  let finalSent = false;

  // Helper: send a chunk to the bridge
  const sendChunk = (text: string): void => {
    if (!text) return;
    bridgeClient.send({
      jsonrpc: "2.0",
      method: "session/update",
      params: {
        sessionId,
        update: {
          sessionUpdate: "agent_message_chunk",
          content: { type: "text", text },
        },
      },
    });
    chunkCount++;
  };

  // Helper: send final completion to the bridge
  const sendFinal = (text: string): void => {
    if (finalSent) return;
    finalSent = true;
    if (text) {
      bridgeClient.send({
        jsonrpc: "2.0",
        method: "session/update",
        params: {
          sessionId,
          update: {
            sessionUpdate: "agent_message_final",
            content: { type: "text", text },
          },
        },
      });
    }
  };

  // Build dispatcher options (following adp-openclaw pattern):
  // - onPartialReply handles real-time token-level streaming
  // - deliver ignores "block" (already sent via onPartialReply) and only handles "final"
  const dispatcherOptions = {
    deliver: async (payload: any, info: any) => {
      const kind = info?.kind;
      const text = payload?.text ?? "";

      logger.info(`deliver called: kind=${kind}, info=${JSON.stringify(info)}, payload_keys=${Object.keys(payload || {})}, text_len=${text.length}, text_preview=${text.slice(0, 200)}`);

      try {
        if (kind === "block") {
          // Ignore — onPartialReply already sent deltas in real-time
          return;
        }
        if (kind === "tool") {
          // Ignore — after_tool_call hook already sent tool results
          return;
        }
        // "final" or undefined — send completion
        if (kind === "final" || kind === undefined) {
          sendFinal(text || lastPartialText);
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
  activeSessionCtx.set(sessionKey, { bridgeClient, sessionId });
  try {
    const cfg = rt.config?.loadConfig?.() ?? {};

    if (rt.channel?.reply?.dispatchReplyWithBufferedBlockDispatcher) {
      const { queuedFinal } = await rt.channel.reply.dispatchReplyWithBufferedBlockDispatcher({
        ctx,
        cfg,
        dispatcherOptions,
        replyOptions: {
          disableBlockStreaming: false,
          onPartialReply: async (payload: any) => {
            const fullText = payload?.text ?? "";
            if (!fullText) return;

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

      // Ensure final is sent even if SDK didn't call deliver with "final"
      if (!finalSent && chunkCount > 0) {
        sendFinal(lastPartialText);
      }

      if (queuedFinal) {
        bridgeClient.send({
          jsonrpc: "2.0",
          id: requestId,
          result: { stopReason: "end_turn" },
        });
      } else {
        logger.warn("No response generated for inbound message");
        bridgeClient.send({
          jsonrpc: "2.0",
          id: requestId,
          result: { stopReason: "no_reply" },
        });
      }
    } else {
      logger.warn("dispatchReplyWithBufferedBlockDispatcher not available on runtime");
      bridgeClient.send({
        jsonrpc: "2.0",
        id: requestId,
        error: { code: -32000, message: "Dispatch not available" },
      });
    }
  } catch (err) {
    logger.error(`Failed to dispatch inbound message: ${String(err)}`);
    bridgeClient.send({
      jsonrpc: "2.0",
      id: requestId,
      error: { code: -32000, message: String(err) },
    });
  } finally {
    activeSessionCtx.delete(sessionKey);
    // Sweep any leaked _pendingToolCtx entries for this session
    for (const [k, v] of pendingToolCtx) {
      if (v._sk === sessionKey) pendingToolCtx.delete(k);
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
