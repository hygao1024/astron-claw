import { randomUUID } from "node:crypto";
import { writeFileSync, mkdirSync } from "node:fs";
import { join } from "node:path";
import { tmpdir } from "node:os";

import { downloadMediaFromBridge } from "../bridge/media.js";
import { getRuntime } from "../runtime.js";
import type { ResolvedAccount, MessageHandler, HandleResult, MediaItem } from "../types.js";

// ---------------------------------------------------------------------------
// Common media download + save logic (deduplicates image/audio/video/file)
// ---------------------------------------------------------------------------
async function downloadAndSaveMedia(
  account: ResolvedAccount,
  mediaId: string,
  fileNameOverride?: string,
): Promise<{ savedPath: string; buffer: Buffer; contentType: string; fileName: string }> {
  const { buffer, contentType, fileName: rawFileName } = await downloadMediaFromBridge(account, mediaId);
  const fileName = fileNameOverride ?? rawFileName;

  const rt = getRuntime();
  let savedPath: string;
  if (rt?.media?.saveMediaLocally) {
    savedPath = await rt.media.saveMediaLocally(buffer, { contentType, fileName });
  } else {
    // Fallback: save to temp directory
    const dir = join(tmpdir(), "astron-claw-media");
    mkdirSync(dir, { recursive: true });
    savedPath = join(dir, `${randomUUID()}_${fileName}`);
    writeFileSync(savedPath, buffer);
  }

  return { savedPath, buffer, contentType, fileName };
}

// ---------------------------------------------------------------------------
// Helper: extract mediaId from data
// ---------------------------------------------------------------------------
function extractMediaId(data: any): string | undefined {
  return data.content?.mediaId ?? data.content?.downloadCode ?? data.mediaId;
}

// ---------------------------------------------------------------------------
// Message Handlers (Strategy Pattern)
// ---------------------------------------------------------------------------

export const textMessageHandler: MessageHandler = {
  canHandle: (data) => data.msgType === "text",
  getPreview: (data) => {
    const text = data.text ?? data.content?.text ?? "";
    return text.length > 50 ? text.slice(0, 50) + "..." : text;
  },
  validate: (data) => {
    const text = data.text ?? data.content?.text;
    if (!text || typeof text !== "string" || !text.trim()) {
      return { valid: false, errorMessage: "Empty text message" };
    }
    return { valid: true };
  },
  handle: async (data, _account) => {
    const text = data.text ?? data.content?.text ?? "";
    return { text: text.trim() };
  },
};

export const imageMessageHandler: MessageHandler = {
  canHandle: (data) => data.msgType === "image" || data.msgType === "picture",
  getPreview: (_data) => "[Image]",
  validate: (data) => {
    const mediaId = extractMediaId(data);
    if (!mediaId) {
      return { valid: false, errorMessage: "No media ID for image" };
    }
    return { valid: true };
  },
  handle: async (data, account) => {
    const mediaId = extractMediaId(data)!;
    const { savedPath, buffer, contentType, fileName } = await downloadAndSaveMedia(account, mediaId);

    const mediaItem: MediaItem = {
      path: savedPath,
      contentType,
      fileName,
      size: buffer.length,
    };

    const text = data.text ?? data.content?.text ?? "";
    return {
      text: text || "[Image]",
      media: { items: [mediaItem], primary: mediaItem },
    };
  },
};

export const audioMessageHandler: MessageHandler = {
  canHandle: (data) => data.msgType === "audio" || data.msgType === "voice",
  getPreview: (data) => {
    const duration = data.content?.duration;
    return duration ? `[Audio ${duration}s]` : "[Audio]";
  },
  validate: (data) => {
    const mediaId = extractMediaId(data);
    if (!mediaId) {
      return { valid: false, errorMessage: "No media ID for audio" };
    }
    return { valid: true };
  },
  handle: async (data, account) => {
    const mediaId = extractMediaId(data)!;
    const { savedPath, buffer, contentType, fileName } = await downloadAndSaveMedia(account, mediaId);

    const duration = data.content?.duration ?? null;
    const transcript = data.content?.recognition ?? data.content?.transcript ?? null;

    const mediaItem: MediaItem = {
      path: savedPath,
      contentType,
      fileName,
      size: buffer.length,
      duration,
    };

    let text = data.text ?? "";
    if (transcript) text = transcript;
    if (!text) text = "[Audio]";

    return {
      text,
      media: { items: [mediaItem], primary: mediaItem },
      extra: { duration, transcript },
    };
  },
};

export const videoMessageHandler: MessageHandler = {
  canHandle: (data) => data.msgType === "video",
  getPreview: (data) => {
    const duration = data.content?.duration;
    return duration ? `[Video ${duration}s]` : "[Video]";
  },
  validate: (data) => {
    const mediaId = extractMediaId(data);
    if (!mediaId) {
      return { valid: false, errorMessage: "No media ID for video" };
    }
    return { valid: true };
  },
  handle: async (data, account) => {
    const mediaId = extractMediaId(data)!;
    const { savedPath, buffer, contentType, fileName } = await downloadAndSaveMedia(account, mediaId);

    const duration = data.content?.duration ?? null;

    const mediaItem: MediaItem = {
      path: savedPath,
      contentType,
      fileName,
      size: buffer.length,
      duration,
    };

    const text = data.text ?? "[Video]";
    return {
      text,
      media: { items: [mediaItem], primary: mediaItem },
      extra: { duration },
    };
  },
};

export const fileMessageHandler: MessageHandler = {
  canHandle: (data) => data.msgType === "file",
  getPreview: (data) => {
    const name = data.content?.fileName ?? data.content?.name ?? "file";
    return `[File: ${name}]`;
  },
  validate: (data) => {
    const mediaId = extractMediaId(data);
    if (!mediaId) {
      return { valid: false, errorMessage: "No media ID for file" };
    }
    return { valid: true };
  },
  handle: async (data, account) => {
    const mediaId = extractMediaId(data)!;
    const realFileName = data.content?.fileName ?? data.content?.name ?? undefined;
    const { savedPath, buffer, contentType, fileName } = await downloadAndSaveMedia(account, mediaId, realFileName);

    const fileSize = data.content?.fileSize ?? data.content?.size ?? buffer.length;

    const mediaItem: MediaItem = {
      path: savedPath,
      contentType,
      fileName,
      size: fileSize,
    };

    const text = data.text ?? `[File: ${fileName}]`;
    return {
      text,
      media: { items: [mediaItem], primary: mediaItem },
      extra: { fileName, fileSize },
    };
  },
};

export const unsupportedMessageHandler: MessageHandler = {
  canHandle: () => true, // catch-all
  getPreview: (data) => `[Unsupported: ${data.msgType ?? "unknown"}]`,
  validate: () => ({ valid: true }),
  handle: async (data) => {
    return { text: `[Unsupported message type: ${data.msgType ?? "unknown"}]` };
  },
};

const messageHandlers: MessageHandler[] = [
  textMessageHandler,
  imageMessageHandler,
  audioMessageHandler,
  videoMessageHandler,
  fileMessageHandler,
  unsupportedMessageHandler,
];

export function findHandler(data: any): MessageHandler {
  return messageHandlers.find((h) => h.canHandle(data)) ?? unsupportedMessageHandler;
}
