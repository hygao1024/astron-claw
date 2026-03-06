export const PLUGIN_ID = "astron-claw";
export const PLUGIN_VERSION = "2.0.0";
export const DEFAULT_ACCOUNT_ID = "default";

export const DEFAULT_BRIDGE_URL = "ws://localhost:8765/bridge/bot";

export const DEFAULT_RETRY_BASE_MS = 1000;
export const DEFAULT_RETRY_MAX_MS = 60000;
export const DEFAULT_RETRY_MAX_ATTEMPTS = 0; // 0 = unlimited

export const LIVENESS_PING_INTERVAL_MS = 15000;
export const LIVENESS_TIMEOUT_MS = 60000;

export const MEDIA_MAX_SIZE_DEFAULT = 50 * 1024 * 1024; // 50MB
export const MEDIA_ALLOWED_TYPES_DEFAULT = [
  "image/*", "audio/*", "video/*",
  "application/pdf", "application/zip",
  "text/plain", "application/octet-stream",
];
