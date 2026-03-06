import { logger, getRawLogger, recordChannelRuntimeState, activeBridgeClients } from "../runtime.js";
import { BridgeClient } from "./client.js";
import { getBridgeHttpBaseUrl } from "./media.js";
import { handleInboundMessage } from "../messaging/inbound.js";
import type { ResolvedAccount, ProbeResult } from "../types.js";

// ---------------------------------------------------------------------------
// Bridge connection monitor (like DingTalk's monitorDingTalkProvider)
// ---------------------------------------------------------------------------
export function monitorBridgeProvider(account: ResolvedAccount, abortSignal?: AbortSignal): Promise<void> {
  return new Promise((resolve) => {
    const bridgeClient = new BridgeClient({
      url: account.bridge.url,
      token: account.bridge.token,
      logger: getRawLogger(),
      retry: account.retry,
      onMessage: (msg: any) => {
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

// ---------------------------------------------------------------------------
// Probe bridge server connectivity
// ---------------------------------------------------------------------------
export async function probeBridgeServer(account: ResolvedAccount): Promise<ProbeResult> {
  const baseUrl = getBridgeHttpBaseUrl(account.bridge.url);
  try {
    const headers: Record<string, string> = {};
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
