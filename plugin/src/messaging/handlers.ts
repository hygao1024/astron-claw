import { randomUUID } from "node:crypto";
import { writeFile } from "node:fs/promises";
import { join } from "node:path";

import { extensionForMime, loadMedia } from "../sdk-compat.js";
import { logger } from "../runtime.js";
import type { MessageHandler, MediaItem } from "../types.js";
import { ensureInboundMediaDir, buildMediaFileName } from "./media-path.js";

// ---------------------------------------------------------------------------
// Common media download + save logic (deduplicates image/audio/video/file)
// Uses loadWebMedia (S3 public URL) + local writeFile to SDK convention path
// ---------------------------------------------------------------------------
async function downloadAndSaveMedia(
  downloadUrl: string,
  fileNameOverride?: string,
): Promise<{ savedPath: string; buffer: Buffer; contentType: string; fileName: string }> {
  // Guard: only accept HTTP URLs
  if (!downloadUrl.startsWith("http")) {
    throw new Error(`Invalid downloadUrl (expected HTTP URL): ${downloadUrl}`);
  }

  const loaded = await loadMedia(downloadUrl);
  const contentType = loaded.contentType ?? "application/octet-stream";
  const fileName = fileNameOverride ?? loaded.fileName ?? "file";

  const ext = extensionForMime(contentType) || ".bin";
  const mediaDir = await ensureInboundMediaDir();
  const uuid = randomUUID();
  const savedName = buildMediaFileName(fileName, uuid, ext);
  const savedPath = join(mediaDir, savedName);
  await writeFile(savedPath, loaded.buffer);

  return { savedPath, buffer: loaded.buffer, contentType, fileName };
}

// ---------------------------------------------------------------------------
// Helper: extract downloadUrl from data
// ---------------------------------------------------------------------------
function extractDownloadUrl(data: any): string | undefined {
  return data.content?.downloadUrl ?? data.media?.downloadUrl ?? data.downloadUrl;
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
    const url = extractDownloadUrl(data);
    if (!url) {
      return { valid: false, errorMessage: "No download URL for image" };
    }
    return { valid: true };
  },
  handle: async (data, _account) => {
    const url = extractDownloadUrl(data);
    if (!url) throw new Error("No download URL for image");
    const { savedPath, buffer, contentType, fileName } = await downloadAndSaveMedia(url);

    const mediaItem: MediaItem = {
      path: savedPath,
      contentType,
      fileName,
      size: buffer.length,
    };

    const text = data.text ?? data.content?.text ?? "";
    return {
      text: text || "<media:image>",
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
    const url = extractDownloadUrl(data);
    if (!url) {
      return { valid: false, errorMessage: "No download URL for audio" };
    }
    return { valid: true };
  },
  handle: async (data, _account) => {
    const url = extractDownloadUrl(data);
    if (!url) throw new Error("No download URL for audio");
    const { savedPath, buffer, contentType, fileName } = await downloadAndSaveMedia(url);

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
    if (!text) text = "<media:audio>";

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
    const url = extractDownloadUrl(data);
    if (!url) {
      return { valid: false, errorMessage: "No download URL for video" };
    }
    return { valid: true };
  },
  handle: async (data, _account) => {
    const url = extractDownloadUrl(data);
    if (!url) throw new Error("No download URL for video");
    const { savedPath, buffer, contentType, fileName } = await downloadAndSaveMedia(url);

    const duration = data.content?.duration ?? null;

    const mediaItem: MediaItem = {
      path: savedPath,
      contentType,
      fileName,
      size: buffer.length,
      duration,
    };

    const text = data.text ?? "<media:video>";
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
    const url = extractDownloadUrl(data);
    if (!url) {
      return { valid: false, errorMessage: "No download URL for file" };
    }
    return { valid: true };
  },
  handle: async (data, _account) => {
    const url = extractDownloadUrl(data);
    if (!url) throw new Error("No download URL for file");
    const realFileName = data.content?.fileName ?? data.content?.name ?? undefined;
    const { savedPath, buffer, contentType, fileName } = await downloadAndSaveMedia(url, realFileName);

    const fileSize = data.content?.fileSize ?? data.content?.size ?? buffer.length;

    const mediaItem: MediaItem = {
      path: savedPath,
      contentType,
      fileName,
      size: fileSize,
    };

    const text = data.text ?? `<media:file name="${fileName}">`;
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
