import { loadMedia } from "../sdk-compat.js";
import { logger, recordChannelRuntimeState } from "../runtime.js";
import { uploadMediaToBridge, inferMediaType, type UploadResult } from "../bridge/media.js";
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
  const ok = bridgeClient.send({
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
  if (!ok) {
    throw new Error(`Bridge send failed for session ${target}`);
  }

  recordChannelRuntimeState(account.accountId, { lastOutboundAt: Date.now() });
}

// ---------------------------------------------------------------------------
// Outbound: sendMedia (from OpenClaw engine to chat client via bridge)
// ---------------------------------------------------------------------------
export async function sendMediaMessage(
  to: string,
  mediaUrl: string,
  options: { text?: string; mimeType?: string; fileName?: string; sessionId?: string } | undefined,
  { account, bridgeClient }: { account: ResolvedAccount; bridgeClient: BridgeClient },
): Promise<void> {
  if (!bridgeClient?.isReady()) {
    throw new Error("Bridge not connected");
  }

  const target = normalizeTarget(to);
  if (!target) throw new Error("Invalid target address");

  // Load media via host runtime, with local fallback if the runtime surface is absent.
  const loaded = await loadMedia(mediaUrl);
  const buffer = loaded.buffer;
  const contentType = loaded.contentType ?? options?.mimeType ?? "application/octet-stream";
  const fileName = loaded.fileName ?? options?.fileName ?? "file";

  const mediaType = inferMediaType(contentType);

  // sessionId: prefer explicit option, fall back to target (which is the session)
  const sessionId = options?.sessionId ?? target;

  // Upload to bridge server
  let uploadResult: UploadResult;
  try {
    uploadResult = await uploadMediaToBridge(account, buffer, fileName, contentType, sessionId);
  } catch (err) {
    // Fallback: send text with media URL
    logger.warn(`Media upload failed, sending as link: ${String(err)}`);
    const fallbackText = options?.text
      ? `${options.text}\n\n${mediaUrl}`
      : mediaUrl;
    await sendTextMessage(to, fallbackText, { account, bridgeClient });
    return;
  }

  // Send as JSON-RPC notification with media info.
  // sessionId for routing = target (normalizeTarget extracts sessionId from address).
  // This also matches the S3 storage path prefix — both are the session UUID.
  const ok = bridgeClient.send({
    jsonrpc: "2.0",
    method: "session/update",
    params: {
      sessionId: target,
      update: {
        sessionUpdate: "agent_media",
        content: {
          msgType: mediaType,
          text: options?.text ?? "",
          media: {
            downloadUrl: uploadResult.downloadUrl,
            fileName,
            mimeType: contentType,
            fileSize: buffer.length,
          },
        },
      },
    },
  });
  if (!ok) {
    throw new Error(`Bridge media send failed for session ${target}`);
  }

  recordChannelRuntimeState(account.accountId, { lastOutboundAt: Date.now() });
}
