import { loadWebMedia } from "openclaw/plugin-sdk";

import { logger, recordChannelRuntimeState } from "../runtime.js";
import { uploadMediaToBridge, inferMediaType } from "../bridge/media.js";
import { normalizeTarget } from "./target.js";
import type { BridgeClient } from "../bridge/client.js";
import type { ResolvedAccount } from "../types.js";

// ---------------------------------------------------------------------------
// Outbound: sendText (from OpenClaw engine to chat client via bridge)
// ---------------------------------------------------------------------------
export async function sendTextMessage(
  to: string,
  text: string,
  { account, bridgeClient }: { account: ResolvedAccount; bridgeClient: BridgeClient },
): Promise<void> {
  if (!bridgeClient?.isReady()) {
    throw new Error("Bridge not connected");
  }

  const target = normalizeTarget(to);
  if (!target) throw new Error("Invalid target address");

  // Send as JSON-RPC notification with sessionId for routing
  bridgeClient.send({
    jsonrpc: "2.0",
    method: "session/update",
    params: {
      sessionId: target,
      update: {
        sessionUpdate: "agent_message_chunk",
        content: { type: "text", text },
      },
    },
  });

  recordChannelRuntimeState(account.accountId, { lastOutboundAt: Date.now() });
}

// ---------------------------------------------------------------------------
// Outbound: sendMedia (from OpenClaw engine to chat client via bridge)
// ---------------------------------------------------------------------------
export async function sendMediaMessage(
  to: string,
  mediaUrl: string,
  options: { text?: string; mimeType?: string; fileName?: string } | undefined,
  { account, bridgeClient }: { account: ResolvedAccount; bridgeClient: BridgeClient },
): Promise<void> {
  if (!bridgeClient?.isReady()) {
    throw new Error("Bridge not connected");
  }

  const target = normalizeTarget(to);
  if (!target) throw new Error("Invalid target address");

  // Load the media using OpenClaw SDK (supports local paths, URLs, file://, ~ paths)
  const loaded = await loadWebMedia(mediaUrl);
  const buffer = loaded.buffer;
  const contentType = loaded.contentType ?? options?.mimeType ?? "application/octet-stream";
  const fileName = loaded.fileName ?? options?.fileName ?? "file";

  const mediaType = inferMediaType(contentType);

  // Upload to bridge server
  let uploadResult: any;
  try {
    uploadResult = await uploadMediaToBridge(account, buffer, fileName, contentType);
  } catch (err) {
    // Fallback: send text with media URL
    logger.warn(`Media upload failed, sending as link: ${String(err)}`);
    const fallbackText = options?.text
      ? `${options.text}\n\n${mediaUrl}`
      : mediaUrl;
    await sendTextMessage(to, fallbackText, { account, bridgeClient });
    return;
  }

  // Send as JSON-RPC notification with media info
  bridgeClient.send({
    jsonrpc: "2.0",
    method: "session/update",
    params: {
      update: {
        sessionUpdate: "agent_media",
        content: {
          msgType: mediaType,
          text: options?.text ?? "",
          media: {
            mediaId: uploadResult.mediaId ?? uploadResult.media_id,
            fileName,
            mimeType: contentType,
            fileSize: buffer.length,
          },
        },
      },
    },
  });

  recordChannelRuntimeState(account.accountId, { lastOutboundAt: Date.now() });
}
