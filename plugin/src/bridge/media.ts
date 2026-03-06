import { randomUUID } from "node:crypto";
import type { ResolvedAccount } from "../types.js";

// ---------------------------------------------------------------------------
// Bridge REST API client (for media upload/download)
// ---------------------------------------------------------------------------

export function getBridgeHttpBaseUrl(wsUrl: string): string {
  // Convert ws(s)://host:port/path to http(s)://host:port
  try {
    const url = new URL(wsUrl);
    const protocol = url.protocol === "wss:" ? "https:" : "http:";
    return `${protocol}//${url.host}`;
  } catch {
    return "http://localhost:8765";
  }
}

export async function downloadMediaFromBridge(
  account: ResolvedAccount,
  mediaId: string,
): Promise<{ buffer: Buffer; contentType: string; fileName: string }> {
  const baseUrl = getBridgeHttpBaseUrl(account.bridge.url);
  const url = `${baseUrl}/api/media/download/${encodeURIComponent(mediaId)}`;

  const headers: Record<string, string> = {};
  if (account.bridge.token) {
    headers["Authorization"] = `Bearer ${account.bridge.token}`;
  }

  const res = await fetch(url, { headers });
  if (!res.ok) {
    throw new Error(`Media download failed: ${res.status} ${res.statusText}`);
  }

  const contentType = res.headers.get("content-type") ?? "application/octet-stream";
  const disposition = res.headers.get("content-disposition") ?? "";
  let fileName = `media_${mediaId}`;
  const match = disposition.match(/filename[*]?=(?:UTF-8''|"?)([^";]+)/i);
  if (match) fileName = decodeURIComponent(match[1]);

  const buffer = Buffer.from(await res.arrayBuffer());
  return { buffer, contentType, fileName };
}

export async function uploadMediaToBridge(
  account: ResolvedAccount,
  buffer: Buffer,
  fileName: string,
  contentType: string,
): Promise<any> {
  const baseUrl = getBridgeHttpBaseUrl(account.bridge.url);
  const url = `${baseUrl}/api/media/upload`;

  const boundary = `----AstronClawBoundary${randomUUID().replace(/-/g, "")}`;
  const CRLF = "\r\n";

  // Build multipart body manually to avoid external dependency
  const parts: string[] = [];
  parts.push(`--${boundary}${CRLF}`);
  parts.push(`Content-Disposition: form-data; name="file"; filename="${fileName}"${CRLF}`);
  parts.push(`Content-Type: ${contentType}${CRLF}`);
  parts.push(CRLF);

  const header = Buffer.from(parts.join(""), "utf8");
  const footer = Buffer.from(`${CRLF}--${boundary}--${CRLF}`, "utf8");
  const body = Buffer.concat([header, buffer, footer]);

  const headers: Record<string, string> = {
    "Content-Type": `multipart/form-data; boundary=${boundary}`,
  };
  if (account.bridge.token) {
    headers["Authorization"] = `Bearer ${account.bridge.token}`;
  }

  const res = await fetch(url, { method: "POST", headers, body });
  if (!res.ok) {
    throw new Error(`Media upload failed: ${res.status} ${res.statusText}`);
  }

  const result = await res.json();
  return result; // { mediaId, url, type }
}

// ---------------------------------------------------------------------------
// Infer media type from MIME
// ---------------------------------------------------------------------------
export function inferMediaType(mimeType: string): "image" | "audio" | "video" | "file" {
  if (!mimeType) return "file";
  if (mimeType.startsWith("image/")) return "image";
  if (mimeType.startsWith("audio/")) return "audio";
  if (mimeType.startsWith("video/")) return "video";
  return "file";
}
