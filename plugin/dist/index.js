import { randomUUID, generateKeyPairSync, createPublicKey, createHash, createPrivateKey, sign } from "node:crypto";
import { existsSync, readFileSync, writeFileSync, mkdirSync, chmodSync } from "node:fs";
import { join } from "node:path";
import { homedir } from "node:os";
import WebSocket from "ws";

// ---------------------------------------------------------------------------
// Constants
// ---------------------------------------------------------------------------
const PLUGIN_ID = "astron-claw";
const PLUGIN_VERSION = "1.0.0";

const DEFAULT_BRIDGE_URL = "ws://localhost:8765/bridge/bot";
const DEFAULT_GATEWAY_URL = "ws://127.0.0.1:18789";
const DEFAULT_GATEWAY_PROTOCOL = 3;
const DEFAULT_GATEWAY_CLIENT_ID = "gateway-client";
const DEFAULT_GATEWAY_CLIENT_MODE = "backend";
const DEFAULT_GATEWAY_AGENT_ID = "main";

const DEFAULT_RETRY_BASE_MS = 1000;
const DEFAULT_RETRY_MAX_MS = 60000;
const DEFAULT_RETRY_MAX_ATTEMPTS = 0; // 0 = unlimited

const LIVENESS_PING_INTERVAL_MS = 15000;
const LIVENESS_TIMEOUT_MS = 60000;

const MAIN_SESSION_KEY = "agent:main:main";

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
// Device Identity (Ed25519) - required by OpenClaw gateway handshake
// ---------------------------------------------------------------------------
const ED25519_SPKI_PREFIX = Buffer.from("302a300506032b6570032100", "hex");
const DEFAULT_IDENTITY_PATH = join(homedir(), ".openclaw", "plugins", "astron-claw", "device.json");

function base64UrlEncode(buf) {
  return buf.toString("base64").replaceAll("+", "-").replaceAll("/", "_").replace(/=+$/g, "");
}

function derivePublicKeyRaw(pem) {
  const der = createPublicKey(pem).export({ type: "spki", format: "der" });
  if (der.length === ED25519_SPKI_PREFIX.length + 32 &&
      der.subarray(0, ED25519_SPKI_PREFIX.length).equals(ED25519_SPKI_PREFIX)) {
    return der.subarray(ED25519_SPKI_PREFIX.length);
  }
  return der;
}

function fingerprintPublicKey(pem) {
  const raw = derivePublicKeyRaw(pem);
  return createHash("sha256").update(raw).digest("hex");
}

function loadOrCreateDeviceIdentity(identityPath = DEFAULT_IDENTITY_PATH) {
  try {
    if (existsSync(identityPath)) {
      const data = JSON.parse(readFileSync(identityPath, "utf8"));
      if (data?.version === 1 && data.publicKeyPem && data.privateKeyPem) {
        const deviceId = fingerprintPublicKey(data.publicKeyPem);
        return { deviceId, publicKeyPem: data.publicKeyPem, privateKeyPem: data.privateKeyPem };
      }
    }
  } catch {}

  const { publicKey, privateKey } = generateKeyPairSync("ed25519");
  const publicKeyPem = publicKey.export({ type: "spki", format: "pem" }).toString();
  const privateKeyPem = privateKey.export({ type: "pkcs8", format: "pem" }).toString();
  const deviceId = fingerprintPublicKey(publicKeyPem);

  mkdirSync(join(identityPath, ".."), { recursive: true });
  const identity = { version: 1, deviceId, publicKeyPem, privateKeyPem, createdAtMs: Date.now() };
  writeFileSync(identityPath, JSON.stringify(identity, null, 2) + "\n", { mode: 0o600 });
  try { chmodSync(identityPath, 0o600); } catch {}

  return { deviceId, publicKeyPem, privateKeyPem };
}

function buildDeviceAuthField({ identity, clientId, clientMode, role, scopes, token, nonce }) {
  const signedAtMs = Date.now();
  const parts = [
    nonce ? "v2" : "v1",
    identity.deviceId,
    clientId,
    clientMode,
    role,
    scopes.join(","),
    String(signedAtMs),
    token ?? "",
  ];
  if (nonce) parts.push(nonce);
  const payload = parts.join("|");

  const privKey = createPrivateKey(identity.privateKeyPem);
  const signature = base64UrlEncode(sign(null, Buffer.from(payload, "utf8"), privKey));
  const publicKeyRaw = base64UrlEncode(derivePublicKeyRaw(identity.publicKeyPem));

  const result = { id: identity.deviceId, publicKey: publicKeyRaw, signature, signedAt: signedAtMs };
  if (nonce) result.nonce = nonce;
  return result;
}

// ---------------------------------------------------------------------------
// Resolve config from pluginConfig
// ---------------------------------------------------------------------------
function resolveConfig(pluginConfig) {
  const pc = pluginConfig ?? {};
  const b = pc.bridge ?? {};
  const g = pc.gateway ?? {};
  const r = pc.retry ?? {};
  return {
    bridge: {
      url: readStr(b.url) ?? DEFAULT_BRIDGE_URL,
      token: readStr(b.token) ?? "",
    },
    gateway: {
      url: readStr(g.url) ?? DEFAULT_GATEWAY_URL,
      protocol: readNum(g.protocol) ?? DEFAULT_GATEWAY_PROTOCOL,
      clientId: readStr(g.clientId) ?? DEFAULT_GATEWAY_CLIENT_ID,
      clientMode: readStr(g.clientMode) ?? DEFAULT_GATEWAY_CLIENT_MODE,
      agentId: readStr(g.agentId) ?? DEFAULT_GATEWAY_AGENT_ID,
    },
    retry: {
      baseMs: readNum(r.baseMs) ?? DEFAULT_RETRY_BASE_MS,
      maxMs: readNum(r.maxMs) ?? DEFAULT_RETRY_MAX_MS,
      maxAttempts: readNum(r.maxAttempts) ?? DEFAULT_RETRY_MAX_ATTEMPTS,
    },
  };
}

// ---------------------------------------------------------------------------
// Gateway WebSocket client (with handshake)
// ---------------------------------------------------------------------------
class GatewayClient {
  constructor({ url, cfg, logger, onFrame, onReady, onClose, retry }) {
    this.url = url;
    this.cfg = cfg;
    this.logger = logger;
    this.onFrame = onFrame;
    this.onReady = onReady;
    this.onClose = onClose;
    this.retry = retry;
    this.ws = null;
    this.ready = false;
    this.closing = false;
    this.backoffMs = retry.baseMs;
    this.attempts = 0;
    this.reconnectTimer = null;
    this.pingTimer = null;
    this.lastSeenAt = 0;
    this.connectNonce = null;
    this.connectSent = false;
    this.identity = loadOrCreateDeviceIdentity();
  }

  start() {
    this.closing = false;
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

  send(frame) {
    if (!this.ws || this.ws.readyState !== WebSocket.OPEN || !this.ready)
      return false;
    try {
      this.ws.send(JSON.stringify(frame));
      return true;
    } catch (e) {
      this.logger.warn?.(`[gateway] send failed: ${String(e)}`);
      return false;
    }
  }

  _connect() {
    if (this.closing) return;

    this.logger.info?.(`[gateway] connecting to ${this.url}`);
    this.ws = new WebSocket(this.url, { handshakeTimeout: LIVENESS_TIMEOUT_MS });

    this.ws.on("open", () => {
      this._markSeen();
      this._startPing();
      this.connectSent = false;
      this.connectNonce = null;
      // Queue connect with a short delay to allow challenge to arrive first
      setTimeout(() => {
        if (!this.connectSent && this.ws?.readyState === WebSocket.OPEN) {
          this._sendConnect();
        }
      }, 750);
    });

    this.ws.on("message", (data) => {
      this._markSeen();
      const raw = data.toString();
      if (raw.trim().toLowerCase() === "ping") {
        this._sendRaw("pong");
        return;
      }
      if (raw.trim().toLowerCase() === "pong") return;

      let frame;
      try {
        frame = JSON.parse(raw);
      } catch {
        this.logger.warn?.("[gateway] invalid json payload");
        return;
      }

      // Handle connect challenge - gateway sends nonce before we send connect
      if (frame.type === "event" && frame.event === "connect.challenge") {
        const nonce = typeof frame.payload?.nonce === "string" ? frame.payload.nonce : undefined;
        if (nonce) this.connectNonce = nonce;
        this._sendConnect();
        return;
      }

      // Handle handshake response
      if (frame.type === "res" && frame.id === "connect") {
        if (frame.ok === false) {
          const msg = frame.error?.message ?? "handshake failed";
          this.logger.error?.(`[gateway] handshake rejected: ${msg}`);
          this.ws?.close(1008, "handshake rejected");
          return;
        }
        this.ready = true;
        this.backoffMs = this.retry.baseMs;
        this.attempts = 0;
        this.logger.info?.("[gateway] handshake complete");
        this.onReady?.();
        return;
      }

      // Forward all other frames
      if (this.ready) {
        this.onFrame?.(frame);
      }
    });

    this.ws.on("close", (code, reason) => {
      this.ready = false;
      this._stopPing();
      this.logger.warn?.(`[gateway] closed code=${code} reason=${reason.toString()}`);
      this.onClose?.();
      this._scheduleReconnect();
    });

    this.ws.on("error", (err) => {
      this.logger.warn?.(`[gateway] error: ${String(err)}`);
    });
  }

  _sendConnect() {
    if (this.connectSent || !this.ws || this.ws.readyState !== WebSocket.OPEN) return;
    this.connectSent = true;

    const role = "operator";
    const scopes = ["operator.admin"];
    const connectFrame = {
      type: "req",
      id: "connect",
      method: "connect",
      params: {
        minProtocol: this.cfg.gateway.protocol,
        maxProtocol: this.cfg.gateway.protocol,
        client: {
          id: this.cfg.gateway.clientId,
          version: PLUGIN_VERSION,
          platform: process.platform,
          mode: this.cfg.gateway.clientMode,
          displayName: "astron-bridge-connector",
        },
        role,
        scopes,
        caps: ["tool-events"],
        ...(this.cfg.gateway.token ? { auth: { token: this.cfg.gateway.token } } : {}),
        device: buildDeviceAuthField({
          identity: this.identity,
          clientId: this.cfg.gateway.clientId,
          clientMode: this.cfg.gateway.clientMode,
          role,
          scopes,
          token: this.cfg.gateway.token ?? undefined,
          nonce: this.connectNonce ?? undefined,
        }),
      },
    };
    this._sendRaw(JSON.stringify(connectFrame));
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
        try { this.ws.ping(); } catch {}
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

  _scheduleReconnect() {
    if (this.closing) return;
    if (this.retry.maxAttempts > 0 && this.attempts >= this.retry.maxAttempts) {
      this.logger.error?.("[gateway] retry limit reached, giving up");
      return;
    }
    const delay = Math.min(this.backoffMs, this.retry.maxMs);
    this.attempts += 1;
    this.backoffMs = Math.min(2 * this.backoffMs, this.retry.maxMs);
    this.logger.info?.(`[gateway] reconnecting in ${delay}ms (attempt ${this.attempts})`);
    this.reconnectTimer = setTimeout(() => this._connect(), delay);
    this.reconnectTimer.unref?.();
  }
}

// ---------------------------------------------------------------------------
// Bridge WebSocket client (connection to Astron server)
// ---------------------------------------------------------------------------
class BridgeClient {
  constructor({ url, token, logger, onMessage, onReady, onClose, retry }) {
    this.url = url;
    this.token = token;
    this.logger = logger;
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
      this.logger.warn?.(`[bridge] send failed: ${String(e)}`);
      return false;
    }
  }

  _connect() {
    if (this.closing) return;

    const headers = {};
    if (this.token) {
      headers["X-Astron-Bot-Token"] = this.token;
    }

    this.logger.info?.(`[bridge] connecting to ${this.url}`);
    this.ws = new WebSocket(this.url, {
      headers,
      handshakeTimeout: LIVENESS_TIMEOUT_MS,
    });

    this.ws.on("open", () => {
      this.ready = true;
      this._markSeen();
      this._startPing();
      this.backoffMs = this.retry.baseMs;
      this.attempts = 0;
      this.logger.info?.("[bridge] connected");
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
        this.logger.warn?.("[bridge] invalid json payload");
        return;
      }
      this.onMessage?.(msg);
    });

    this.ws.on("close", (code, reason) => {
      this.ready = false;
      this._stopPing();
      this.logger.warn?.(`[bridge] closed code=${code} reason=${reason.toString()}`);
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
      this.logger.warn?.(`[bridge] unexpected http response status=${status}`);
      this._scheduleReconnect();
    });

    this.ws.on("error", (err) => {
      this.logger.warn?.(`[bridge] error: ${String(err)}`);
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
    this.logger.error?.(`[bridge] auth failed (${reason}), will not retry`);
  }

  _scheduleReconnect() {
    if (this.closing || this.authFailed) return;
    if (this.retry.maxAttempts > 0 && this.attempts >= this.retry.maxAttempts) {
      this.logger.error?.("[bridge] retry limit reached, giving up");
      return;
    }
    const delay = Math.min(this.backoffMs, this.retry.maxMs);
    this.attempts += 1;
    this.backoffMs = Math.min(2 * this.backoffMs, this.retry.maxMs);
    this.logger.info?.(`[bridge] reconnecting in ${delay}ms (attempt ${this.attempts})`);
    this.reconnectTimer = setTimeout(() => this._connect(), delay);
    this.reconnectTimer.unref?.();
  }
}

// ---------------------------------------------------------------------------
// ACP Bridge Core - handles JSON-RPC methods from the Astron server
// ---------------------------------------------------------------------------
class AcpBridgeCore {
  constructor({ logger, cfg, isGatewayReady, sendGatewayFrame, sendBridgeMessage }) {
    this.logger = logger;
    this.cfg = cfg;
    this.isGatewayReady = isGatewayReady;
    this.sendGatewayFrame = sendGatewayFrame;
    this.sendBridgeMessage = sendBridgeMessage;
    // Maps gateway request IDs to { rpcId, sessionId, done }
    this.inFlightPrompts = new Map();
  }

  // Called when a JSON-RPC message arrives from the bridge
  handleBridgeMessage(msg) {
    if (!msg || typeof msg !== "object") return;
    const method = typeof msg.method === "string" ? msg.method.trim() : null;
    if (!method) return;

    const id = msg.id;

    switch (method) {
      case "initialize":
        if (id !== undefined) this._handleInitialize(id, msg.params);
        break;
      case "session/new":
        if (id !== undefined) this._handleSessionNew(id, msg.params);
        break;
      case "session/prompt":
        if (id !== undefined) this._handleSessionPrompt(id, msg.params);
        break;
      case "session/cancel":
        this._handleSessionCancel(id, msg.params);
        break;
      default:
        if (id !== undefined) {
          this._sendError(id, -32601, `method not found: ${method}`);
        }
    }
  }

  // Called when a frame arrives from the local gateway
  handleGatewayFrame(frame) {
    if (!frame || typeof frame !== "object") return;

    // Gateway response to our request
    if (frame.type === "res") {
      const prompt = this.inFlightPrompts.get(frame.id);
      if (prompt && !prompt.done) {
        if (frame.ok === false) {
          const errMsg = frame.error?.message ?? "gateway returned error";
          this._failPrompt(prompt, -32020, errMsg, frame.error);
        }
      }
      return;
    }

    // Gateway events
    if (frame.type === "event") {
      this._handleGatewayEvent(frame);
    }
  }

  // Called when the gateway disconnects
  handleGatewayDisconnected() {
    for (const [, prompt] of this.inFlightPrompts) {
      if (!prompt.done) {
        this._failPrompt(prompt, -32001, "gateway disconnected");
      }
    }
  }

  // ---- JSON-RPC method handlers ----

  _handleInitialize(id, params) {
    this._sendResult(id, {
      protocolVersion: 1,
      agentCapabilities: {
        loadSession: false,
        promptCapabilities: {
          embeddedContext: true,
          image: false,
          audio: false,
        },
      },
      agentInfo: {
        name: PLUGIN_ID,
        version: PLUGIN_VERSION,
      },
    });
  }

  _handleSessionNew(id, params) {
    const sessionId = (params && params.sessionId) || MAIN_SESSION_KEY;
    this._sendResult(id, {
      sessionId,
      modes: {
        availableModes: [{ id: "default", name: "Default", description: "Default agent mode" }],
        currentModeId: "default",
      },
    }, sessionId);
  }

  _handleSessionPrompt(id, params) {
    if (!params || typeof params !== "object") {
      this._sendError(id, -32602, "invalid params");
      return;
    }

    const sessionId = (params && params.sessionId) || MAIN_SESSION_KEY;

    if (!this.isGatewayReady()) {
      this._sendError(id, -32001, "gateway unavailable");
      return;
    }

    // Build the prompt text from params
    const promptText = this._extractPromptText(params);
    if (!promptText) {
      this._sendError(id, -32602, "prompt text is required");
      return;
    }

    const gatewayRequestId = `req_${randomUUID().replace(/-/g, "")}`;
    const idempotencyKey = `astron_${sessionId}_${Date.now()}`;

    const gatewayFrame = {
      type: "req",
      id: gatewayRequestId,
      method: "agent",
      params: {
        agentId: this.cfg.gateway.agentId,
        sessionKey: sessionId,
        message: `User Message From Astron:\n${promptText}`,
        deliver: false,
        idempotencyKey,
      },
    };

    if (!this.sendGatewayFrame(gatewayFrame)) {
      this._sendError(id, -32001, "failed to send prompt to gateway");
      return;
    }

    // Send initial empty chunk so the bridge knows streaming started
    this._sendSessionUpdate(sessionId, {
      sessionUpdate: "agent_message_chunk",
      content: { type: "text", text: "" },
    }, id);

    // Track the in-flight prompt
    const prompt = {
      rpcId: id,
      sessionId,
      gatewayRequestId,
      done: false,
    };
    this.inFlightPrompts.set(gatewayRequestId, prompt);
  }

  _handleSessionCancel(id, params) {
    if (!params || typeof params !== "object") {
      if (id !== undefined) this._sendError(id, -32602, "invalid params");
      return;
    }

    const targetSessionId = (params && params.sessionId) || MAIN_SESSION_KEY;

    // Find the active prompt and cancel it
    for (const [, prompt] of this.inFlightPrompts) {
      if (!prompt.done && prompt.sessionId === targetSessionId) {
        // Send cancel to gateway
        const cancelFrame = {
          type: "req",
          id: `cancel_${prompt.gatewayRequestId}`,
          method: "agent.cancel",
          params: {
            sessionKey: prompt.sessionId,
            requestId: prompt.gatewayRequestId,
          },
        };
        this.sendGatewayFrame(cancelFrame);
        this._completePrompt(prompt, "cancelled");
        break;
      }
    }

    if (id !== undefined) {
      this._sendResult(id, {}, targetSessionId);
    }
  }

  // ---- Gateway event handling ----

  _handleGatewayEvent(frame) {
    const event = typeof frame.event === "string" ? frame.event : null;
    if (!event) return;

    if (event === "agent" || event === "event.agent") {
      this._handleAgentEvent(frame.payload ?? frame.data ?? frame);
    }
  }

  _handleAgentEvent(payload) {
    if (!payload || typeof payload !== "object") return;

    const stream = typeof payload.stream === "string" ? payload.stream : null;
    const data = payload.data && typeof payload.data === "object" ? payload.data : {};
    const runId = typeof payload.runId === "string" ? payload.runId : undefined;
    const requestId = typeof payload.requestId === "string" ? payload.requestId : undefined;

    // Resolve the prompt this event belongs to
    const prompt = this._resolvePrompt(payload);
    if (!prompt || prompt.done) return;

    if (stream === "assistant") {
      const text =
        this._asTextChunk(data.delta) ?? this._asTextChunk(data.text);
      if (text) {
        this._sendSessionUpdate(prompt.sessionId, {
          sessionUpdate: "agent_message_chunk",
          content: { type: "text", text },
        }, prompt.rpcId);
      }

      // Handle content array blocks
      const blocks = Array.isArray(data.content) ? data.content
        : (data.content && typeof data.content === "object") ? [data.content]
        : (typeof data.type === "string") ? [data]
        : [];

      for (const block of blocks) {
        if (!block || typeof block !== "object") continue;
        const type = typeof block.type === "string" ? block.type : null;

        if (type === "thinking") {
          const thinkText = this._normalizeText(block.thinking) ?? this._normalizeText(block.text);
          if (thinkText) {
            this._sendSessionUpdate(prompt.sessionId, {
              sessionUpdate: "agent_thought_chunk",
              content: { type: "text", text: thinkText },
            }, prompt.rpcId);
          }
        } else if (type === "toolCall") {
          const toolCallId = this._readToolCallId(block) ?? `tc_${prompt.gatewayRequestId}_${Date.now()}`;
          const name = typeof block.name === "string" ? block.name : "tool";
          const args = block.arguments ?? block.args ?? {};
          this._sendSessionUpdate(prompt.sessionId, {
            sessionUpdate: "tool_call",
            toolCallId,
            title: name,
            status: "in_progress",
            content: [{ type: "content", content: { type: "text", text: JSON.stringify(args, null, 2) } }],
          }, prompt.rpcId);
        } else if (type === "toolResult") {
          const toolCallId = this._readToolCallId(block) ?? `tc_${prompt.gatewayRequestId}_result_${Date.now()}`;
          const name = typeof block.name === "string" ? block.name : "tool";
          const resultText = this._normalizeText(block.text)
            ?? this._normalizeText(block.result)
            ?? (typeof block.result === "object" ? JSON.stringify(block.result, null, 2) : undefined);
          if (resultText) {
            this._sendSessionUpdate(prompt.sessionId, {
              sessionUpdate: "tool_call_update",
              toolCallId,
              title: name,
              status: "completed",
              content: [{ type: "content", content: { type: "text", text: resultText } }],
            }, prompt.rpcId);
          }
        }
      }
      return;
    }

    if (stream === "thinking") {
      const text =
        this._asTextChunk(data.delta) ?? this._asTextChunk(data.text);
      if (text) {
        this._sendSessionUpdate(prompt.sessionId, {
          sessionUpdate: "agent_thought_chunk",
          content: { type: "text", text },
        }, prompt.rpcId);
      }
      return;
    }

    if (stream === "tool") {
      const phase = typeof data.phase === "string" ? data.phase : null;
      const toolCallId = this._readToolCallId(data) ?? `tc_${prompt.gatewayRequestId}_${Date.now()}`;
      const toolName = typeof data.name === "string" ? data.name : "tool";

      if (phase === "start") {
        const args = data.arguments ?? data.args ?? {};
        this._sendSessionUpdate(prompt.sessionId, {
          sessionUpdate: "tool_call",
          toolCallId,
          title: toolName,
          status: "in_progress",
          content: [{ type: "content", content: { type: "text", text: JSON.stringify(args, null, 2) } }],
        }, prompt.rpcId);
      } else if (phase === "result") {
        const resultText = this._normalizeText(data.text)
          ?? this._normalizeText(data.result)
          ?? (typeof data.result === "object" ? JSON.stringify(data.result, null, 2) : undefined)
          ?? "";
        this._sendSessionUpdate(prompt.sessionId, {
          sessionUpdate: "tool_call_update",
          toolCallId,
          title: toolName,
          status: "completed",
          content: [{ type: "content", content: { type: "text", text: resultText } }],
        }, prompt.rpcId);
      }
      return;
    }

    if (stream === "lifecycle") {
      const phase = typeof data.phase === "string" ? data.phase : null;
      if (phase === "end") {
        this._completePrompt(prompt, "end_turn");
      } else if (phase === "cancelled" || phase === "cancel") {
        this._completePrompt(prompt, "cancelled");
      } else if (phase === "error") {
        const errMsg = typeof data.message === "string" ? data.message
          : (data.error?.message ?? "gateway lifecycle error");
        this._failPrompt(prompt, -32021, errMsg, data);
      }
    }
  }

  _resolvePrompt(payload) {
    // Try matching by requestId
    const reqId = typeof payload.requestId === "string" ? payload.requestId : undefined;
    if (reqId && this.inFlightPrompts.has(reqId)) {
      return this.inFlightPrompts.get(reqId);
    }
    // Try matching by runId or nested data.requestId
    const data = payload.data ?? {};
    const nestedReqId = typeof data.requestId === "string" ? data.requestId : undefined;
    if (nestedReqId && this.inFlightPrompts.has(nestedReqId)) {
      return this.inFlightPrompts.get(nestedReqId);
    }
    // Fallback: return any in-flight prompt
    for (const [, p] of this.inFlightPrompts) {
      if (!p.done) return p;
    }
    return null;
  }

  // ---- Transport helpers ----

  _sendSessionUpdate(sessionId, update, requestId) {
    const notification = {
      jsonrpc: "2.0",
      method: "session/update",
      params: {
        sessionId,
        update,
        _meta: {
          ...(requestId !== undefined ? { requestId } : {}),
          messageType: "normal",
        },
      },
    };
    this.sendBridgeMessage(notification);
  }

  _sendResult(id, result, sessionId) {
    const msg = {
      jsonrpc: "2.0",
      id,
      result: {
        ...result,
        _meta: {
          requestId: id,
          ...(sessionId ? { sessionId } : {}),
        },
      },
    };
    this.sendBridgeMessage(msg);
  }

  _sendError(id, code, message, data) {
    const msg = {
      jsonrpc: "2.0",
      id,
      error: {
        code,
        message,
        ...(data !== undefined ? { data } : {}),
      },
    };
    this.sendBridgeMessage(msg);
    this.logger.warn?.(`[acp] request failed code=${code} message=${message}`);
  }

  _completePrompt(prompt, stopReason) {
    if (prompt.done) return;
    prompt.done = true;
    this._sendResult(prompt.rpcId, { stopReason }, prompt.sessionId);
    this.inFlightPrompts.delete(prompt.gatewayRequestId);
  }

  _failPrompt(prompt, code, message, data) {
    if (prompt.done) return;
    prompt.done = true;
    this._sendError(prompt.rpcId, code, message, data);
    this.inFlightPrompts.delete(prompt.gatewayRequestId);
  }

  _extractPromptText(params) {
    // params.prompt can be a string or object with text/blocks
    const prompt = params.prompt;
    if (typeof prompt === "string") return prompt.trim() || null;
    if (prompt && typeof prompt === "object") {
      // Check for .text
      if (typeof prompt.text === "string") return prompt.text.trim() || null;
      // Check for .blocks array
      if (Array.isArray(prompt.blocks)) {
        const texts = [];
        for (const block of prompt.blocks) {
          if (typeof block === "string") texts.push(block);
          else if (block && typeof block.text === "string") texts.push(block.text);
        }
        return texts.join("\n").trim() || null;
      }
      // Check for content array
      if (Array.isArray(prompt.content)) {
        const texts = [];
        for (const c of prompt.content) {
          if (typeof c === "string") texts.push(c);
          else if (c && typeof c.text === "string") texts.push(c.text);
        }
        return texts.join("\n").trim() || null;
      }
    }
    // Fallback: params.message
    if (typeof params.message === "string") return params.message.trim() || null;
    return null;
  }

  _asTextChunk(v) {
    if (typeof v === "string" && v.length > 0) return v;
    return null;
  }

  _normalizeText(v) {
    if (typeof v === "string") {
      const t = v.trim();
      return t || null;
    }
    return null;
  }

  _readToolCallId(obj) {
    return typeof obj.toolCallId === "string" ? obj.toolCallId
      : typeof obj.tool_call_id === "string" ? obj.tool_call_id
      : typeof obj.callId === "string" ? obj.callId
      : typeof obj.id === "string" ? obj.id
      : null;
  }
}

// ---------------------------------------------------------------------------
// Service factory
// ---------------------------------------------------------------------------
function createAstronService({ logger, pluginConfig, runtime }) {
  let bridgeClient = null;
  let gatewayClient = null;
  let bridgeCore = null;
  let stopped = true;

  function stop() {
    stopped = true;
    bridgeClient?.stop();
    gatewayClient?.stop();
    bridgeClient = null;
    gatewayClient = null;
    bridgeCore = null;
  }

  function start(serviceCtx) {
    stop();
    stopped = false;

    const cfg = resolveConfig(pluginConfig);

    // Read gateway auth token from OpenClaw config
    let gatewayToken = undefined;
    try {
      const openclawCfg = serviceCtx?.config ?? {};
      gatewayToken = openclawCfg?.gateway?.auth?.token ?? openclawCfg?.gateway?.token;
      if (!gatewayToken && runtime?.config?.loadConfig) {
        const loaded = runtime.config.loadConfig();
        gatewayToken = loaded?.gateway?.auth?.token ?? loaded?.gateway?.token;
      }
      if (!gatewayToken) {
        // Read directly from file as fallback
        const cfgPath = join(homedir(), ".openclaw", "openclaw.json");
        if (existsSync(cfgPath)) {
          const raw = JSON.parse(readFileSync(cfgPath, "utf8"));
          gatewayToken = raw?.gateway?.auth?.token ?? raw?.gateway?.token;
        }
      }
    } catch (e) {
      logger.warn?.(`[astron-claw] failed to read gateway token: ${String(e)}`);
    }
    cfg.gateway.token = gatewayToken;

    logger.info?.(`[astron-claw] starting service bridge=${cfg.bridge.url} gateway=${cfg.gateway.url}`);

    // Create the ACP bridge core
    bridgeCore = new AcpBridgeCore({
      logger,
      cfg,
      isGatewayReady: () => gatewayClient?.isReady() ?? false,
      sendGatewayFrame: (frame) => gatewayClient?.send(frame) ?? false,
      sendBridgeMessage: (msg) => {
        if (!bridgeClient?.send(msg)) {
          logger.warn?.("[astron-claw] bridge not ready; dropping message");
        }
      },
    });

    // Create bridge client (connects outbound to Astron server)
    bridgeClient = new BridgeClient({
      url: cfg.bridge.url,
      token: cfg.bridge.token,
      logger,
      retry: cfg.retry,
      onMessage: (msg) => bridgeCore?.handleBridgeMessage(msg),
      onReady: () => logger.info?.(`[astron-claw] bridge connected url=${cfg.bridge.url}`),
      onClose: () => logger.warn?.("[astron-claw] bridge disconnected"),
    });

    // Create gateway client (connects to local OpenClaw gateway)
    gatewayClient = new GatewayClient({
      url: cfg.gateway.url,
      cfg,
      logger,
      retry: cfg.retry,
      onFrame: (frame) => bridgeCore?.handleGatewayFrame(frame),
      onReady: () => logger.info?.(`[astron-claw] gateway connected url=${cfg.gateway.url}`),
      onClose: () => {
        logger.warn?.("[astron-claw] gateway disconnected");
        if (!stopped) {
          bridgeCore?.handleGatewayDisconnected();
        }
      },
    });

    // Start both connections
    bridgeClient.start();
    gatewayClient.start();
  }

  return { start, stop };
}

// ---------------------------------------------------------------------------
// Plugin export
// ---------------------------------------------------------------------------
const plugin = {
  id: PLUGIN_ID,
  name: PLUGIN_ID,
  description:
    "Connector plugin that bridges the Astron server with the local OpenClaw Gateway.",
  register(ctx) {
    const service = createAstronService({
      logger: ctx.logger,
      pluginConfig: ctx.pluginConfig ?? {},
      runtime: ctx.runtime,
    });

    ctx.registerService({
      id: PLUGIN_ID,
      start: (serviceCtx) => service.start(serviceCtx),
      stop: () => service.stop(),
    });
  },
};

export default plugin;
