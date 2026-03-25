import { readFile } from "node:fs/promises";
import { basename, resolve } from "node:path";
import { fileURLToPath } from "node:url";

import { getRuntime } from "./runtime.js";

export const SILENT_REPLY_TOKEN = "NO_REPLY";
export const HEARTBEAT_TOKEN = "HEARTBEAT_OK";

type LoadedMedia = {
  buffer: Buffer;
  contentType?: string;
  fileName?: string;
};

function escapeRegExp(text: string): string {
  return text.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
}

export function isSilentReplyText(text: string, token = SILENT_REPLY_TOKEN): boolean {
  return new RegExp(`^\\s*${escapeRegExp(token)}\\s*$`).test(text);
}

function resolveRuntimeMediaLoader():
  | ((source: string) => Promise<{ buffer: Uint8Array | ArrayBuffer | Buffer; contentType?: string; fileName?: string }>)
  | null {
  const runtime = getRuntime();
  return typeof runtime?.media?.loadWebMedia === "function" ? runtime.media.loadWebMedia : null;
}

function inferFileNameFromSource(source: string): string {
  if (/^https?:\/\//i.test(source)) {
    try {
      return decodeURIComponent(new URL(source).pathname.split("/").pop() || "file");
    } catch {
      return source.split("/").pop() || "file";
    }
  }

  if (source.startsWith("file://")) {
    return basename(fileURLToPath(source));
  }

  return basename(source);
}

async function loadMediaFallback(source: string): Promise<LoadedMedia> {
  if (/^https?:\/\//i.test(source)) {
    const response = await fetch(source);
    if (!response.ok) {
      throw new Error(`HTTP ${response.status} ${response.statusText}`);
    }

    return {
      buffer: Buffer.from(await response.arrayBuffer()),
      contentType: response.headers.get("content-type") ?? undefined,
      fileName: inferFileNameFromSource(source),
    };
  }

  const filePath = source.startsWith("file://") ? fileURLToPath(source) : resolve(source);
  return {
    buffer: await readFile(filePath),
    fileName: basename(filePath),
  };
}

export async function loadMedia(source: string): Promise<LoadedMedia> {
  const runtimeLoader = resolveRuntimeMediaLoader();
  if (runtimeLoader) {
    const loaded = await runtimeLoader(source);
    return {
      buffer: Buffer.from(loaded.buffer),
      contentType: loaded.contentType,
      fileName: loaded.fileName,
    };
  }

  return await loadMediaFallback(source);
}

const MIME_EXTENSION_MAP: Record<string, string> = {
  "application/gzip": ".gz",
  "application/json": ".json",
  "application/octet-stream": ".bin",
  "application/pdf": ".pdf",
  "application/zip": ".zip",
  "audio/mpeg": ".mp3",
  "audio/mp4": ".m4a",
  "audio/ogg": ".ogg",
  "audio/wav": ".wav",
  "image/jpeg": ".jpg",
  "image/png": ".png",
  "image/svg+xml": ".svg",
  "image/webp": ".webp",
  "text/csv": ".csv",
  "text/html": ".html",
  "text/markdown": ".md",
  "text/plain": ".txt",
  "video/mp4": ".mp4",
  "video/quicktime": ".mov",
  "video/webm": ".webm",
};

export function extensionForMime(mimeType?: string | null): string | null {
  if (!mimeType) {
    return null;
  }

  const normalized = mimeType.split(";")[0]?.trim().toLowerCase();
  if (!normalized) {
    return null;
  }

  const mapped = MIME_EXTENSION_MAP[normalized];
  if (mapped) {
    return mapped;
  }

  const [type, rawSubtype] = normalized.split("/");
  if (!type || !rawSubtype) {
    return null;
  }

  const subtype = rawSubtype.split("+")[0];
  if (!subtype || /[^a-z0-9.-]/.test(subtype)) {
    return null;
  }

  return `.${subtype}`;
}
