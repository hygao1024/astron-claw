import { randomUUID } from "node:crypto";
import { writeFileSync, mkdirSync } from "node:fs";
import { join } from "node:path";
import { tmpdir } from "node:os";
import WebSocket from "ws";
import { loadWebMedia } from "openclaw/plugin-sdk";

// ---------------------------------------------------------------------------
// Constants
// ---------------------------------------------------------------------------
const PLUGIN_ID = "astron-claw";
const PLUGIN_VERSION = "2.0.0";
const DEFAULT_ACCOUNT_ID = "default";

const DEFAULT_BRIDGE_URL = "ws://localhost:8765/bridge/bot";

const DEFAULT_RETRY_BASE_MS = 1000;
const DEFAULT_RETRY_MAX_MS = 60000;
const DEFAULT_RETRY_MAX_ATTEMPTS = 0; // 0 = unlimited

const LIVENESS_PING_INTERVAL_MS = 15000;
const LIVENESS_TIMEOUT_MS = 60000;

const MEDIA_MAX_SIZE_DEFAULT = 50 * 1024 * 1024; // 50MB
const MEDIA_ALLOWED_TYPES_DEFAULT = [
  "image/*", "audio/*", "video/*",
  "application/pdf", "application/zip",
  "text/plain", "application/octet-stream",
];

// ---------------------------------------------------------------------------
// Logger
// ---------------------------------------------------------------------------
let _logger = console;

function setLogger(l) {
  _logger = l ?? console;
}

const logger = {
  info: (...args) => _logger.info?.("[AstronClaw]", ...args),
  warn: (...args) => _logger.warn?.("[AstronClaw]", ...args),
  error: (...args) => _logger.error?.("[AstronClaw]", ...args),
  debug: (...args) => _logger.debug?.("[AstronClaw]", ...args),
};

// ---------------------------------------------------------------------------
// Runtime singleton (holds PluginRuntime reference)
// ---------------------------------------------------------------------------
let _runtime = null;

function setRuntime(rt) {
  _runtime = rt;
}

function getRuntime() {
  return _runtime;
}

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------
function readStr(v) {
  return typeof v === "string" && v.trim() ? v.trim() : undefined;
}

function readNum(v) {
  return typeof v === "number" && Number.isFinite(v) ? v : undefined;
}

// ---------------------------------------------------------------------------
// Account resolution
// ---------------------------------------------------------------------------
function resolveAstronClawAccountFromCfg(cfg) {
  const pluginCfg = cfg?.channels?.[PLUGIN_ID]
    ?? cfg?.plugins?.entries?.[PLUGIN_ID]?.config
    ?? {};

  const bridge = pluginCfg.bridge ?? {};
  const retry = pluginCfg.retry ?? {};
  const media = pluginCfg.media ?? {};

  return {
    accountId: DEFAULT_ACCOUNT_ID,
    enabled: pluginCfg.enabled !== false,
    name: readStr(pluginCfg.name) ?? "AstronClaw",
    bridge: {
      url: readStr(bridge.url) ?? DEFAULT_BRIDGE_URL,
      token: readStr(bridge.token) ?? "",
    },
    retry: {
      baseMs: readNum(retry.baseMs) ?? DEFAULT_RETRY_BASE_MS,
      maxMs: readNum(retry.maxMs) ?? DEFAULT_RETRY_MAX_MS,
      maxAttempts: readNum(retry.maxAttempts) ?? DEFAULT_RETRY_MAX_ATTEMPTS,
    },
    allowFrom: Array.isArray(pluginCfg.allowFrom) ? pluginCfg.allowFrom : ["*"],
    media: {
      maxSize: readNum(media.maxSize) ?? MEDIA_MAX_SIZE_DEFAULT,
      allowedTypes: Array.isArray(media.allowedTypes) ? media.allowedTypes : MEDIA_ALLOWED_TYPES_DEFAULT,
    },
    tokenSource: bridge.token ? "config" : "none",
  };
}

function resolveAstronClawAccount() {
  const rt = getRuntime();
  if (!rt) return null;

  let cfg;
  try {
    cfg = rt.config?.loadConfig?.() ?? {};
  } catch {
    cfg = {};
  }
  return resolveAstronClawAccountFromCfg(cfg);
}

// ---------------------------------------------------------------------------
// Runtime state tracking
// ---------------------------------------------------------------------------
const runtimeState = new Map();

function recordChannelRuntimeState(accountId, updates) {
  const key = `${PLUGIN_ID}:${accountId}`;
  const current = runtimeState.get(key) ?? {
    running: false,
    lastStartAt: null,
    lastStopAt: null,
    lastError: null,
    lastInboundAt: null,
    lastOutboundAt: null,
  };
  Object.assign(current, updates);
  runtimeState.set(key, current);
}

function getChannelRuntimeState(accountId) {
  return runtimeState.get(`${PLUGIN_ID}:${accountId}`) ?? {
    running: false,
    lastStartAt: null,
    lastStopAt: null,
    lastError: null,
    lastInboundAt: null,
    lastOutboundAt: null,
  };
}

// ---------------------------------------------------------------------------
// Bridge REST API client (for media upload/download)
// ---------------------------------------------------------------------------

function getBridgeHttpBaseUrl(wsUrl) {
  // Convert ws(s)://host:port/path to http(s)://host:port
  try {
    const url = new URL(wsUrl);
    const protocol = url.protocol === "wss:" ? "https:" : "http:";
    return `${protocol}//${url.host}`;
  } catch {
    return "http://localhost:8765";
  }
}

async function downloadMediaFromBridge(account, mediaId) {
  const baseUrl = getBridgeHttpBaseUrl(account.bridge.url);
  const url = `${baseUrl}/api/media/download/${encodeURIComponent(mediaId)}`;

  const headers = {};
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

async function uploadMediaToBridge(account, buffer, fileName, contentType) {
  const baseUrl = getBridgeHttpBaseUrl(account.bridge.url);
  const url = `${baseUrl}/api/media/upload`;

  const boundary = `----AstronClawBoundary${randomUUID().replace(/-/g, "")}`;
  const CRLF = "\r\n";

  // Build multipart body manually to avoid external dependency
  const parts = [];
  parts.push(`--${boundary}${CRLF}`);
  parts.push(`Content-Disposition: form-data; name="file"; filename="${fileName}"${CRLF}`);
  parts.push(`Content-Type: ${contentType}${CRLF}`);
  parts.push(CRLF);

  const header = Buffer.from(parts.join(""), "utf8");
  const footer = Buffer.from(`${CRLF}--${boundary}--${CRLF}`, "utf8");
  const body = Buffer.concat([header, buffer, footer]);

  const headers = {
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
function inferMediaType(mimeType) {
  if (!mimeType) return "file";
  if (mimeType.startsWith("image/")) return "image";
  if (mimeType.startsWith("audio/")) return "audio";
  if (mimeType.startsWith("video/")) return "video";
  return "file";
}

// ---------------------------------------------------------------------------
// Message Handlers (Strategy Pattern)
// ---------------------------------------------------------------------------

// Each handler: { canHandle, getPreview, validate, handle }
// handle returns: { text, media? }
// media: { items: MediaItem[], primary?: MediaItem }

const textMessageHandler = {
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

const imageMessageHandler = {
  canHandle: (data) => data.msgType === "image" || data.msgType === "picture",
  getPreview: (data) => "[Image]",
  validate: (data) => {
    const mediaId = data.content?.mediaId ?? data.content?.downloadCode ?? data.mediaId;
    if (!mediaId) {
      return { valid: false, errorMessage: "No media ID for image" };
    }
    return { valid: true };
  },
  handle: async (data, account) => {
    const mediaId = data.content?.mediaId ?? data.content?.downloadCode ?? data.mediaId;
    const { buffer, contentType, fileName } = await downloadMediaFromBridge(account, mediaId);

    const rt = getRuntime();
    let savedPath = null;
    if (rt?.media?.saveMediaLocally) {
      savedPath = await rt.media.saveMediaLocally(buffer, { contentType, fileName });
    } else {
      // Fallback: save to temp directory
      const dir = join(tmpdir(), "astron-claw-media");
      mkdirSync(dir, { recursive: true });
      savedPath = join(dir, `${randomUUID()}_${fileName}`);
      writeFileSync(savedPath, buffer);
    }

    const mediaItem = {
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

const audioMessageHandler = {
  canHandle: (data) => data.msgType === "audio" || data.msgType === "voice",
  getPreview: (data) => {
    const duration = data.content?.duration;
    return duration ? `[Audio ${duration}s]` : "[Audio]";
  },
  validate: (data) => {
    const mediaId = data.content?.mediaId ?? data.content?.downloadCode ?? data.mediaId;
    if (!mediaId) {
      return { valid: false, errorMessage: "No media ID for audio" };
    }
    return { valid: true };
  },
  handle: async (data, account) => {
    const mediaId = data.content?.mediaId ?? data.content?.downloadCode ?? data.mediaId;
    const { buffer, contentType, fileName } = await downloadMediaFromBridge(account, mediaId);

    const rt = getRuntime();
    let savedPath = null;
    if (rt?.media?.saveMediaLocally) {
      savedPath = await rt.media.saveMediaLocally(buffer, { contentType, fileName });
    } else {
      const dir = join(tmpdir(), "astron-claw-media");
      mkdirSync(dir, { recursive: true });
      savedPath = join(dir, `${randomUUID()}_${fileName}`);
      writeFileSync(savedPath, buffer);
    }

    const duration = data.content?.duration ?? null;
    const transcript = data.content?.recognition ?? data.content?.transcript ?? null;

    const mediaItem = {
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

const videoMessageHandler = {
  canHandle: (data) => data.msgType === "video",
  getPreview: (data) => {
    const duration = data.content?.duration;
    return duration ? `[Video ${duration}s]` : "[Video]";
  },
  validate: (data) => {
    const mediaId = data.content?.mediaId ?? data.content?.downloadCode ?? data.mediaId;
    if (!mediaId) {
      return { valid: false, errorMessage: "No media ID for video" };
    }
    return { valid: true };
  },
  handle: async (data, account) => {
    const mediaId = data.content?.mediaId ?? data.content?.downloadCode ?? data.mediaId;
    const { buffer, contentType, fileName } = await downloadMediaFromBridge(account, mediaId);

    const rt = getRuntime();
    let savedPath = null;
    if (rt?.media?.saveMediaLocally) {
      savedPath = await rt.media.saveMediaLocally(buffer, { contentType, fileName });
    } else {
      const dir = join(tmpdir(), "astron-claw-media");
      mkdirSync(dir, { recursive: true });
      savedPath = join(dir, `${randomUUID()}_${fileName}`);
      writeFileSync(savedPath, buffer);
    }

    const duration = data.content?.duration ?? null;

    const mediaItem = {
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

const fileMessageHandler = {
  canHandle: (data) => data.msgType === "file",
  getPreview: (data) => {
    const name = data.content?.fileName ?? data.content?.name ?? "file";
    return `[File: ${name}]`;
  },
  validate: (data) => {
    const mediaId = data.content?.mediaId ?? data.content?.downloadCode ?? data.mediaId;
    if (!mediaId) {
      return { valid: false, errorMessage: "No media ID for file" };
    }
    return { valid: true };
  },
  handle: async (data, account) => {
    const mediaId = data.content?.mediaId ?? data.content?.downloadCode ?? data.mediaId;
    const { buffer, contentType, fileName } = await downloadMediaFromBridge(account, mediaId);

    const rt = getRuntime();
    const realFileName = data.content?.fileName ?? data.content?.name ?? fileName;
    let savedPath = null;
    if (rt?.media?.saveMediaLocally) {
      savedPath = await rt.media.saveMediaLocally(buffer, { contentType, fileName: realFileName });
    } else {
      const dir = join(tmpdir(), "astron-claw-media");
      mkdirSync(dir, { recursive: true });
      savedPath = join(dir, `${randomUUID()}_${realFileName}`);
      writeFileSync(savedPath, buffer);
    }

    const fileSize = data.content?.fileSize ?? data.content?.size ?? buffer.length;

    const mediaItem = {
      path: savedPath,
      contentType,
      fileName: realFileName,
      size: fileSize,
    };

    const text = data.text ?? `[File: ${realFileName}]`;
    return {
      text,
      media: { items: [mediaItem], primary: mediaItem },
      extra: { fileName: realFileName, fileSize },
    };
  },
};

const unsupportedMessageHandler = {
  canHandle: () => true, // catch-all
  getPreview: (data) => `[Unsupported: ${data.msgType ?? "unknown"}]`,
  validate: () => ({ valid: true }),
  handle: async (data) => {
    return { text: `[Unsupported message type: ${data.msgType ?? "unknown"}]` };
  },
};

const messageHandlers = [
  textMessageHandler,
  imageMessageHandler,
  audioMessageHandler,
  videoMessageHandler,
  fileMessageHandler,
  unsupportedMessageHandler,
];

function findHandler(data) {
  return messageHandlers.find((h) => h.canHandle(data)) ?? unsupportedMessageHandler;
}

// ---------------------------------------------------------------------------
// Bridge WebSocket client (transport layer - like DingTalk's Stream WebSocket)
// ---------------------------------------------------------------------------
class BridgeClient {
  constructor({ url, token, logger: log, onMessage, onReady, onClose, retry }) {
    this.url = url;
    this.token = token;
    this.log = log;
    this.onMessage = onMessage;
    this.onReady = onReady;
    this.onClose = onClose;
    this.retry = retry;
    this.ws = null;
    this.ready = false;
    this.closing = false;
    this.authFailed = false;
    this.backoffMs = retry.baseMs;
    this.attempts = 0;
    this.reconnectTimer = null;
    this.pingTimer = null;
    this.lastSeenAt = 0;
  }

  start() {
    this.closing = false;
    this.authFailed = false;
    this._connect();
  }

  stop() {
    this.closing = true;
    this.ready = false;
    this._stopPing();
    if (this.reconnectTimer) {
      clearTimeout(this.reconnectTimer);
      this.reconnectTimer = null;
    }
    if (this.ws) {
      try { this.ws.close(); } catch {}
      this.ws = null;
    }
  }

  isReady() {
    return this.ready;
  }

  send(msg) {
    if (!this.ws || this.ws.readyState !== WebSocket.OPEN || !this.ready)
      return false;
    try {
      this.ws.send(JSON.stringify(msg));
      return true;
    } catch (e) {
      this.log.warn?.(`[bridge] send failed: ${String(e)}`);
      return false;
    }
  }

  _connect() {
    if (this.closing) return;

    const headers = {};
    if (this.token) {
      headers["X-Astron-Bot-Token"] = this.token;
    }

    this.log.info?.(`[bridge] connecting to ${this.url}`);
    this.ws = new WebSocket(this.url, { headers, handshakeTimeout: LIVENESS_TIMEOUT_MS });

    this.ws.on("open", () => {
      this.ready = true;
      this._markSeen();
      this._startPing();
      this.backoffMs = this.retry.baseMs;
      this.attempts = 0;
      this.log.info?.("[bridge] connected");
      this.onReady?.();
    });

    this.ws.on("message", (data) => {
      this._markSeen();
      const raw = data.toString();
      if (raw.trim().toLowerCase() === "ping") {
        this._sendRaw("pong");
        return;
      }
      if (raw.trim().toLowerCase() === "pong") return;

      let msg;
      try {
        msg = JSON.parse(raw);
      } catch {
        this.log.warn?.("[bridge] invalid json payload");
        return;
      }
      this.onMessage?.(msg);
    });

    this.ws.on("close", (code, reason) => {
      this.ready = false;
      this._stopPing();
      this.log.warn?.(`[bridge] closed code=${code} reason=${reason.toString()}`);
      this.onClose?.();
      if (code === 4001) {
        this._markAuthFailed("4001");
      } else {
        this._scheduleReconnect();
      }
    });

    this.ws.on("unexpected-response", (_req, res) => {
      const status = res.statusCode;
      if (status === 401) {
        this._markAuthFailed("http 401");
        return;
      }
      this.log.warn?.(`[bridge] unexpected http response status=${status}`);
      this._scheduleReconnect();
    });

    this.ws.on("error", (err) => {
      this.log.warn?.(`[bridge] error: ${String(err)}`);
      const msg = err instanceof Error ? err.message : String(err);
      if (msg.includes("401")) {
        this._markAuthFailed("http 401");
      }
    });
  }

  _sendRaw(data) {
    if (!this.ws || this.ws.readyState !== WebSocket.OPEN) return false;
    try {
      this.ws.send(data);
      return true;
    } catch {
      return false;
    }
  }

  _markSeen() {
    this.lastSeenAt = Date.now();
  }

  _startPing() {
    this._stopPing();
    this.pingTimer = setInterval(() => {
      if (this.ws && this.ws.readyState === WebSocket.OPEN) {
        try {
          this.ws.send(JSON.stringify({ type: "ping" }));
        } catch {}
      }
    }, LIVENESS_PING_INTERVAL_MS);
    this.pingTimer.unref?.();
  }

  _stopPing() {
    if (this.pingTimer) {
      clearInterval(this.pingTimer);
      this.pingTimer = null;
    }
  }

  _markAuthFailed(reason) {
    if (this.authFailed) return;
    this.authFailed = true;
    if (this.reconnectTimer) {
      clearTimeout(this.reconnectTimer);
      this.reconnectTimer = null;
    }
    this.log.error?.(`[bridge] auth failed (${reason}), will not retry`);
  }

  _scheduleReconnect() {
    if (this.closing || this.authFailed) return;
    if (this.retry.maxAttempts > 0 && this.attempts >= this.retry.maxAttempts) {
      this.log.error?.("[bridge] retry limit reached, giving up");
      return;
    }
    const delay = Math.min(this.backoffMs, this.retry.maxMs);
    this.attempts += 1;
    this.backoffMs = Math.min(2 * this.backoffMs, this.retry.maxMs);
    this.log.info?.(`[bridge] reconnecting in ${delay}ms (attempt ${this.attempts})`);
    this.reconnectTimer = setTimeout(() => this._connect(), delay);
    this.reconnectTimer.unref?.();
  }
}

// ---------------------------------------------------------------------------
// Bridge Monitor - inbound message processing (like DingTalk's monitor.ts)
// ---------------------------------------------------------------------------

async function handleInboundMessage(msg, account, bridgeClient) {
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

async function handleJsonRpcPrompt(rpcMsg, account, bridgeClient) {
  const rt = getRuntime();
  if (!rt) return;

  const requestId = rpcMsg.id;
  const params = rpcMsg.params ?? {};
  const sessionId = params.sessionId ?? "default";
  const prompt = params.prompt ?? {};
  const contentItems = prompt.content ?? [];

  // Extract text and media from content items
  let textParts = [];
  let mediaItems = [];
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
  let mediaPath = null;
  let mediaType = null;
  let mediaUrl = null;
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
  let route;
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
  const ctx = {
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
  const sendChunk = (text) => {
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
  const sendFinal = (text) => {
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
    deliver: async (payload, info) => {
      const kind = info?.kind;
      const text = payload?.text ?? "";

      logger.info(`deliver called: kind=${kind}, info=${JSON.stringify(info)}, payload_keys=${Object.keys(payload || {})}, text_len=${text.length}, text_preview=${text.slice(0, 200)}`);

      try {
        if (kind === "block") {
          // Ignore — onPartialReply already sent deltas in real-time
          return;
        }
        if (kind === "tool") {
          // Tool result
          bridgeClient.send({
            jsonrpc: "2.0",
            method: "session/update",
            params: {
              sessionId,
              update: {
                sessionUpdate: "tool_result",
                content: { type: "text", text },
              },
            },
          });
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
    onError: (err, info) => {
      logger.error(`Reply delivery error (${info?.kind}): ${String(err)}`);
    },
  };

  // Dispatch through the OpenClaw SDK using onPartialReply for token-level streaming.
  // onPartialReply receives cumulative text on each token; we compute the delta
  // and send only the new portion as a chunk (same approach as adp-openclaw).
  try {
    const cfg = rt.config?.loadConfig?.() ?? {};

    if (rt.channel?.reply?.dispatchReplyWithBufferedBlockDispatcher) {
      const { queuedFinal } = await rt.channel.reply.dispatchReplyWithBufferedBlockDispatcher({
        ctx,
        cfg,
        dispatcherOptions,
        replyOptions: {
          disableBlockStreaming: false,
          onToolStart: async ({ name, phase }) => {
            if (phase !== "start") return;
            bridgeClient.send({
              jsonrpc: "2.0",
              method: "session/update",
              params: {
                sessionId,
                update: {
                  sessionUpdate: "tool_call",
                  title: name || "tool",
                  status: "running",
                  content: [],
                },
              },
            });
          },
          onPartialReply: async (payload) => {
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
  }
}

async function handleDirectMessage(msg, account, bridgeClient) {
  // Handle direct { type: "message" } format (for future extensibility)
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

  let route;
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
function createReplyDispatcher(data, account, bridgeClient) {
  return {
    deliver: async (payload) => {
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
    onError: (err, info) => {
      logger.error(`Reply delivery error: ${String(err)}`, info);
    },
  };
}

// ---------------------------------------------------------------------------
// Normalize target address (like DingTalk's normalizeDingTalkTarget)
// ---------------------------------------------------------------------------
function normalizeTarget(raw) {
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

function isGroupTarget(target) {
  return target?.startsWith("chat:");
}

// ---------------------------------------------------------------------------
// Outbound: sendText (from OpenClaw engine to chat client via bridge)
// ---------------------------------------------------------------------------
async function sendTextMessage(to, text, { account, bridgeClient }) {
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
async function sendMediaMessage(to, mediaUrl, options, { account, bridgeClient }) {
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
  let uploadResult;
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

// ---------------------------------------------------------------------------
// Bridge connection monitor (like DingTalk's monitorDingTalkProvider)
// ---------------------------------------------------------------------------
function monitorBridgeProvider(account, abortSignal) {
  return new Promise((resolve) => {
    const bridgeClient = new BridgeClient({
      url: account.bridge.url,
      token: account.bridge.token,
      logger: _logger,
      retry: account.retry,
      onMessage: (msg) => {
        handleInboundMessage(msg, account, bridgeClient).catch((err) => {
          logger.error(`Inbound message processing error: ${String(err)}`);
        });
      },
      onReady: () => {
        logger.info(`Bridge connected: ${account.bridge.url}`);
        recordChannelRuntimeState(account.accountId, {
          running: true,
          lastStartAt: Date.now(),
          lastError: null,
        });
      },
      onClose: () => {
        logger.warn("Bridge disconnected");
        recordChannelRuntimeState(account.accountId, { running: false });
      },
    });

    // Store reference for outbound use
    activeBridgeClients.set(account.accountId, bridgeClient);

    // Wrap connect to prevent unhandled rejection
    try {
      bridgeClient.start();
    } catch (err) {
      logger.error(`Bridge start failed: ${String(err)}`);
      recordChannelRuntimeState(account.accountId, {
        running: false,
        lastError: String(err),
      });
    }

    // Return a pending Promise; resolve on abort (same pattern as DingTalk)
    if (abortSignal) {
      const onAbort = () => {
        bridgeClient.stop();
        activeBridgeClients.delete(account.accountId);
        recordChannelRuntimeState(account.accountId, {
          running: false,
          lastStopAt: Date.now(),
        });
        resolve();
      };
      if (abortSignal.aborted) {
        onAbort();
      } else {
        abortSignal.addEventListener("abort", onAbort, { once: true });
      }
    }
  });
}

// Active bridge client references for outbound messaging
const activeBridgeClients = new Map();

// ---------------------------------------------------------------------------
// Probe bridge server connectivity
// ---------------------------------------------------------------------------
async function probeBridgeServer(account) {
  const baseUrl = getBridgeHttpBaseUrl(account.bridge.url);
  try {
    const headers = {};
    if (account.bridge.token) {
      headers["X-Astron-Bot-Token"] = account.bridge.token;
    }
    const res = await fetch(`${baseUrl}/api/health`, { headers, signal: AbortSignal.timeout(5000) });
    if (res.ok) {
      const data = await res.json().catch(() => ({}));
      return { ok: true, name: data.name ?? "AstronClaw Bridge", data };
    }
    return { ok: false, error: `HTTP ${res.status}` };
  } catch (err) {
    return { ok: false, error: String(err) };
  }
}

// ---------------------------------------------------------------------------
// Onboarding (interactive configuration)
// ---------------------------------------------------------------------------
const astronClawOnboarding = {
  getStatus: async () => {
    const account = resolveAstronClawAccount();
    if (!account) {
      return {
        configured: false,
        message: "AstronClaw is not configured. Bridge URL and token are required.",
        quickStartScore: 0,
      };
    }

    if (!account.bridge.token) {
      return {
        configured: false,
        message: "AstronClaw bridge token is not configured.",
        quickStartScore: 30,
      };
    }

    // Try probe
    const probe = await probeBridgeServer(account);
    if (probe.ok) {
      return {
        configured: true,
        message: `Connected to bridge: ${probe.name}`,
        quickStartScore: 100,
      };
    }

    return {
      configured: true,
      message: `Bridge configured but unreachable: ${probe.error}`,
      quickStartScore: 60,
    };
  },

  configure: async (interaction) => {
    const account = resolveAstronClawAccount();
    const hasCreds = account?.bridge?.token;

    if (hasCreds && interaction?.confirm) {
      const keep = await interaction.confirm("Bridge credentials already configured. Keep them?");
      if (keep) {
        return { cfg: { enabled: true }, accountId: DEFAULT_ACCOUNT_ID };
      }
    }

    let bridgeUrl = DEFAULT_BRIDGE_URL;
    let bridgeToken = "";

    if (interaction?.prompt) {
      // Show help text
      if (interaction.display) {
        interaction.display(
          "## AstronClaw Configuration\n\n" +
          "AstronClaw connects to a bridge server that relays messages from chat clients.\n\n" +
          "You need:\n" +
          "1. **Bridge URL** - WebSocket URL of the bridge server\n" +
          "2. **Bridge Token** - Authentication token for the bridge server\n"
        );
      }

      const urlInput = await interaction.prompt("Bridge WebSocket URL", { default: DEFAULT_BRIDGE_URL });
      if (urlInput) bridgeUrl = urlInput;

      const tokenInput = await interaction.prompt("Bridge authentication token");
      if (tokenInput) bridgeToken = tokenInput;
    }

    const cfg = {
      enabled: true,
      name: "AstronClaw",
      bridge: {
        url: bridgeUrl,
        token: bridgeToken,
      },
      allowFrom: ["*"],
    };

    return { cfg, accountId: DEFAULT_ACCOUNT_ID };
  },

  disable: async () => {
    return { cfg: { enabled: false } };
  },
};

// ---------------------------------------------------------------------------
// ChannelPlugin definition (following DingTalk pattern)
// ---------------------------------------------------------------------------
const astronClawPlugin = {
  id: PLUGIN_ID,

  meta: {
    id: PLUGIN_ID,
    label: "AstronClaw",
    selectionLabel: "AstronClaw",
    blurb: "Bridge-based channel connecting web chat clients to OpenClaw via WebSocket.",
    systemImage: "message.fill",
  },

  // --- Capabilities ---
  capabilities: {
    chatTypes: ["direct"],
    media: true,
    blockStreaming: true,
    reactions: false,
    threads: false,
    nativeCommands: false,
  },

  // --- Onboarding ---
  onboarding: astronClawOnboarding,

  // --- Config (account discovery — required by framework) ---
  config: {
    listAccountIds: (cfg) => {
      const pluginCfg = cfg?.channels?.[PLUGIN_ID]
        ?? cfg?.plugins?.entries?.[PLUGIN_ID]?.config
        ?? {};
      // Return account if bridge URL is configured (token checked by isConfigured)
      if (pluginCfg.bridge?.url || pluginCfg.bridge?.token) {
        return [DEFAULT_ACCOUNT_ID];
      }
      return [];
    },

    resolveAccount: (cfg, _accountId) => {
      return resolveAstronClawAccountFromCfg(cfg);
    },

    defaultAccountId: (_cfg) => DEFAULT_ACCOUNT_ID,

    isConfigured: (account) => {
      return !!(account?.bridge?.token && account?.bridge?.url);
    },

    describeAccount: (account) => ({
      accountId: account?.accountId ?? DEFAULT_ACCOUNT_ID,
      name: account?.name ?? "AstronClaw",
      enabled: account?.enabled !== false,
      configured: !!(account?.bridge?.token && account?.bridge?.url),
      tokenSource: account?.bridge?.token ? "config" : "none",
    }),

    resolveAllowFrom: ({ cfg }) => {
      const account = resolveAstronClawAccountFromCfg(cfg);
      return account.allowFrom.map((entry) => String(entry));
    },

    formatAllowFrom: ({ allowFrom }) =>
      allowFrom
        .map((entry) => String(entry).trim())
        .filter(Boolean)
        .map((entry) => entry.replace(new RegExp(`^${PLUGIN_ID}:(?:user:)?`, "i"), "")),
  },

  // --- Outbound ---
  outbound: {
    deliveryMode: "direct",
    textChunkLimit: 4000,

    resolveTarget: ({ to, allowFrom, mode }) => {
      const trimmed = to?.trim() ?? "";
      const allowListRaw = (allowFrom ?? []).map((e) => String(e).trim()).filter(Boolean);
      const hasWildcard = allowListRaw.includes("*");
      const allowList = allowListRaw
        .filter((e) => e !== "*")
        .map((e) => normalizeTarget(e))
        .filter((e) => !!e);

      if (trimmed) {
        const normalized = normalizeTarget(trimmed);
        if (!normalized) {
          if ((mode === "implicit" || mode === "heartbeat") && allowList.length > 0) {
            return { ok: true, to: allowList[0] };
          }
          return {
            ok: false,
            error: new Error(`Invalid target: ${trimmed}. Use <userId> or set allowFrom.`),
          };
        }

        if (mode === "explicit") {
          return { ok: true, to: normalized };
        }

        if (mode === "implicit" || mode === "heartbeat") {
          if (hasWildcard || allowList.length === 0) {
            return { ok: true, to: normalized };
          }
          if (allowList.includes(normalized)) {
            return { ok: true, to: normalized };
          }
          return { ok: true, to: allowList[0] };
        }

        return { ok: true, to: normalized };
      }

      // No target specified
      if (allowList.length > 0) {
        return { ok: true, to: allowList[0] };
      }

      return {
        ok: false,
        error: new Error(`No target specified. Set allowFrom or provide a target.`),
      };
    },

    sendText: async ({ to, text, cfg }) => {
      const account = resolveAstronClawAccountFromCfg(cfg);
      const bridgeClient = activeBridgeClients.get(account.accountId ?? DEFAULT_ACCOUNT_ID);
      if (!bridgeClient) throw new Error("No active bridge connection");
      await sendTextMessage(to, text, { account, bridgeClient });
      return { channel: PLUGIN_ID, messageId: "", chatId: to };
    },

    sendMedia: async ({ to, text, mediaUrl, cfg }) => {
      const account = resolveAstronClawAccountFromCfg(cfg);
      const bridgeClient = activeBridgeClients.get(account.accountId ?? DEFAULT_ACCOUNT_ID);
      if (!bridgeClient) throw new Error("No active bridge connection");
      if (mediaUrl) {
        await sendMediaMessage(to, mediaUrl, { text }, { account, bridgeClient });
      } else if (text) {
        await sendTextMessage(to, text, { account, bridgeClient });
      }
      return { channel: PLUGIN_ID, messageId: "", chatId: to };
    },
  },

  // --- Messaging (target resolution for message tool) ---
  messaging: {
    normalizeTarget: (target) => {
      const trimmed = target?.trim();
      if (!trimmed) return undefined;
      return normalizeTarget(trimmed);
    },
    targetResolver: {
      looksLikeId: (id) => {
        const trimmed = id?.trim();
        if (!trimmed) return false;
        // Accept: astron-claw:user:xxx, user:xxx, raw UUID, chat:xxx
        const prefixPattern = new RegExp(`^${PLUGIN_ID}:`, "i");
        return prefixPattern.test(trimmed)
          || trimmed.startsWith("user:")
          || trimmed.startsWith("chat:")
          || /^[a-zA-Z0-9_-]+$/.test(trimmed);
      },
      hint: `<userId> or ${PLUGIN_ID}:user:<userId>`,
    },
  },

  // --- Security ---
  security: {
    resolveDmPolicy: ({ cfg }) => {
      const account = resolveAstronClawAccountFromCfg(cfg);
      return {
        policy: "allowlist",
        allowFrom: account.allowFrom ?? ["*"],
        policyPath: `channels.${PLUGIN_ID}.allowFrom`,
        normalizeEntry: (raw) => {
          if (typeof raw !== "string") return String(raw);
          return raw.replace(`${PLUGIN_ID}:user:`, "").replace(`${PLUGIN_ID}:`, "");
        },
      };
    },
  },

  // --- Gateway ---
  gateway: {
    startAccount: async (ctx) => {
      const { account, abortSignal } = ctx;
      ctx.log?.info?.(`[${account.accountId}] starting AstronClaw bridge connection`);

      const probe = await probeBridgeServer(account);
      if (probe.ok) {
        ctx.log?.info?.(`[${account.accountId}] bridge probe OK: ${probe.name}`);
      } else {
        ctx.log?.warn?.(`[${account.accountId}] bridge probe failed: ${probe.error} (will try connecting anyway)`);
      }

      return monitorBridgeProvider(account, abortSignal);
    },

    logoutAccount: async ({ account, cfg }) => {
      logger.info(`Logging out account: ${account.accountId}`);
      const client = activeBridgeClients.get(account.accountId);
      if (client) {
        client.stop();
        activeBridgeClients.delete(account.accountId);
      }
      recordChannelRuntimeState(account.accountId, {
        running: false,
        lastStopAt: Date.now(),
      });

      // Clear credentials from config (write to channels.*, not plugins.entries.*)
      const rt = getRuntime();
      if (rt?.config?.writeConfigFile) {
        const nextCfg = { ...cfg };
        const channelCfg = cfg?.channels?.[PLUGIN_ID] ?? {};
        const nextChannelCfg = { ...channelCfg };
        delete nextChannelCfg.bridge;

        if (Object.keys(nextChannelCfg).length > 0) {
          nextCfg.channels = { ...nextCfg.channels, [PLUGIN_ID]: nextChannelCfg };
        } else {
          const nextChannels = { ...nextCfg.channels };
          delete nextChannels[PLUGIN_ID];
          if (Object.keys(nextChannels).length > 0) {
            nextCfg.channels = nextChannels;
          } else {
            delete nextCfg.channels;
          }
        }
        await rt.config.writeConfigFile(nextCfg);
      }

      return { cleared: true, loggedOut: true };
    },
  },

  // --- Status ---
  status: {
    defaultRuntime: {
      accountId: DEFAULT_ACCOUNT_ID,
      running: false,
      lastStartAt: null,
      lastStopAt: null,
      lastError: null,
    },

    probeAccount: async ({ account, timeoutMs }) => {
      return probeBridgeServer(account);
    },

    buildAccountSnapshot: ({ account, runtime, probe }) => {
      const configured = !!(account?.bridge?.token && account?.bridge?.url);
      return {
        accountId: account.accountId,
        name: account.name ?? "AstronClaw",
        enabled: account.enabled !== false,
        configured,
        tokenSource: account.bridge?.token ? "config" : "none",
        running: runtime?.running ?? false,
        lastStartAt: runtime?.lastStartAt ?? null,
        lastStopAt: runtime?.lastStopAt ?? null,
        lastError: runtime?.lastError ?? null,
        mode: "bridge",
        probe,
      };
    },

    collectStatusIssues: (accounts) => {
      const issues = [];
      for (const account of accounts) {
        const accountId = account.accountId ?? DEFAULT_ACCOUNT_ID;
        if (!account.configured) {
          issues.push({
            channel: PLUGIN_ID,
            accountId,
            kind: "config",
            message: "Bridge credentials (url/token) not configured",
          });
        }
      }
      return issues;
    },
  },
};

// ---------------------------------------------------------------------------
// Plugin entry point
// ---------------------------------------------------------------------------
const plugin = {
  id: PLUGIN_ID,
  name: PLUGIN_ID,
  version: PLUGIN_VERSION,
  description: "AstronClaw channel plugin - connects chat clients via bridge server to OpenClaw.",

  register(api) {
    // Save runtime reference (like DingTalk's setDingTalkRuntime)
    setRuntime(api.runtime);
    setLogger(api.runtime?.logger ?? api.logger);

    // Register as a Channel (not a Service)
    api.registerChannel({ plugin: astronClawPlugin });

    logger.info(`AstronClaw v${PLUGIN_VERSION} registered as channel plugin`);
  },
};

export default plugin;
