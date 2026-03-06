import WebSocket from "ws";
import { LIVENESS_PING_INTERVAL_MS, LIVENESS_TIMEOUT_MS } from "../constants.js";
import type { RetryConfig } from "../types.js";

// ---------------------------------------------------------------------------
// Bridge WebSocket client (transport layer - like DingTalk's Stream WebSocket)
// ---------------------------------------------------------------------------
export class BridgeClient {
  url: string;
  token: string;
  log: any;
  onMessage: ((msg: any) => void) | undefined;
  onReady: (() => void) | undefined;
  onClose: (() => void) | undefined;
  retry: RetryConfig;
  ws: WebSocket | null;
  ready: boolean;
  closing: boolean;
  authFailed: boolean;
  backoffMs: number;
  attempts: number;
  reconnectTimer: ReturnType<typeof setTimeout> | null;
  pingTimer: ReturnType<typeof setInterval> | null;
  lastSeenAt: number;

  constructor({ url, token, logger: log, onMessage, onReady, onClose, retry }: {
    url: string;
    token: string;
    logger: any;
    onMessage?: (msg: any) => void;
    onReady?: () => void;
    onClose?: () => void;
    retry: RetryConfig;
  }) {
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

  start(): void {
    this.closing = false;
    this.authFailed = false;
    this._connect();
  }

  stop(): void {
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

  isReady(): boolean {
    return this.ready;
  }

  send(msg: any): boolean {
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

  _connect(): void {
    if (this.closing) return;

    const headers: Record<string, string> = {};
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

  _sendRaw(data: string): boolean {
    if (!this.ws || this.ws.readyState !== WebSocket.OPEN) return false;
    try {
      this.ws.send(data);
      return true;
    } catch {
      return false;
    }
  }

  _markSeen(): void {
    this.lastSeenAt = Date.now();
  }

  _startPing(): void {
    this._stopPing();
    this.pingTimer = setInterval(() => {
      if (this.ws && this.ws.readyState === WebSocket.OPEN) {
        try {
          this.ws.send(JSON.stringify({ type: "ping" }));
        } catch {}
      }
    }, LIVENESS_PING_INTERVAL_MS);
    (this.pingTimer as any).unref?.();
  }

  _stopPing(): void {
    if (this.pingTimer) {
      clearInterval(this.pingTimer);
      this.pingTimer = null;
    }
  }

  _markAuthFailed(reason: string): void {
    if (this.authFailed) return;
    this.authFailed = true;
    if (this.reconnectTimer) {
      clearTimeout(this.reconnectTimer);
      this.reconnectTimer = null;
    }
    this.log.error?.(`[bridge] auth failed (${reason}), will not retry`);
  }

  _scheduleReconnect(): void {
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
    (this.reconnectTimer as any).unref?.();
  }
}
