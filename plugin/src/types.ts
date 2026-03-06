import type { BridgeClient } from "./bridge/client.js";

export type RetryConfig = {
  baseMs: number;
  maxMs: number;
  maxAttempts: number;
};

export type MediaConfig = {
  maxSize: number;
  allowedTypes: string[];
};

export type BridgeConfig = {
  url: string;
  token: string;
};

export type ResolvedAccount = {
  accountId: string;
  enabled: boolean;
  name: string;
  bridge: BridgeConfig;
  retry: RetryConfig;
  allowFrom: string[];
  media: MediaConfig;
  tokenSource: "config" | "none";
};

export type ChannelRuntimeState = {
  running: boolean;
  lastStartAt: number | null;
  lastStopAt: number | null;
  lastError: string | null;
  lastInboundAt: number | null;
  lastOutboundAt: number | null;
};

export type SessionContext = {
  bridgeClient: BridgeClient;
  sessionId: string;
};

export type ValidationResult = {
  valid: boolean;
  errorMessage?: string;
};

export type HandleResult = {
  text: string;
  media?: {
    items: MediaItem[];
    primary?: MediaItem;
  };
  extra?: Record<string, unknown>;
};

export type MediaItem = {
  path: string;
  contentType: string;
  fileName: string;
  size: number;
  duration?: number | null;
};

export type MessageHandler = {
  canHandle: (data: any) => boolean;
  getPreview: (data: any) => string;
  validate: (data: any) => ValidationResult;
  handle: (data: any, account: ResolvedAccount) => Promise<HandleResult>;
};

export type ProbeResult = {
  ok: boolean;
  name?: string;
  data?: any;
  error?: string;
};
