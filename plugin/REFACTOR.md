# AstronClaw Plugin 模块化拆分设计文档

## 1. 现状分析

### 1.1 当前结构

```
plugin/
├── package.json
├── package-lock.json
├── openclaw.plugin.json
└── dist/
    └── index.js          ← 1706 行，~54KB，所有逻辑集中在单文件
```

### 1.2 问题

| 问题 | 影响 |
|------|------|
| **单文件 1700+ 行** | 难以定位代码、认知负担重 |
| **职责混杂** | WebSocket 传输层、消息协议、媒体处理、配置解析、UI 引导全部糅合 |
| **无类型系统** | 纯 JS 无 TypeScript，接口契约靠注释维护，重构易引入 Bug |
| **直接放 `dist/`** | 无源码/产物分离，无法增量构建或引入静态检查 |
| **媒体 Handler 大量重复** | image/audio/video/file 四个 handler 的 `handle()` 方法 80% 逻辑相同（下载→保存→构建 mediaItem） |
| **全局可变状态分散** | `_runtime`、`_logger`、`_activeSessionCtx`、`_pendingToolCtx`、`runtimeState`、`activeBridgeClients` 六个全局 Map/变量散落各处 |

### 1.3 现有代码逻辑分区（按行号）

| 行号范围 | 职责 | 预估行数 |
|----------|------|----------|
| 1-30 | Constants | 30 |
| 31-81 | Logger + Runtime singleton + Helpers | 50 |
| 83-158 | Account resolution + Runtime state tracking | 75 |
| 160-231 | Bridge REST API client (media upload/download) | 70 |
| 233-477 | Message Handlers (Strategy Pattern) | 245 |
| 479-665 | BridgeClient class (WebSocket transport) | 187 |
| 667-971 | Inbound message processing (JSON-RPC dispatch) | 305 |
| 973-1054 | Reply dispatcher (direct format) | 80 |
| 1056-1083 | Target normalization | 28 |
| 1085-1167 | Outbound messaging (sendText/sendMedia) | 83 |
| 1169-1233 | Bridge connection monitor | 65 |
| 1235-1254 | Bridge health probe | 20 |
| 1256-1344 | Onboarding wizard | 89 |
| 1346-1630 | ChannelPlugin definition | 285 |
| 1632-1706 | Plugin entry point + register + hooks | 75 |

---

## 2. 目标架构

参考 ADP-OpenClaw 的模块化模式：**入口文件在包根目录，实现细节在 `src/`，每个文件一个职责**。

### 2.1 目录结构

```
plugin/
├── index.ts                  ← 包入口（根目录），register() 注册 channel + 挂载 hooks
├── package.json              ← 更新 extensions 指向 ./index.ts
├── package-lock.json
├── openclaw.plugin.json      ← 不变
├── tsconfig.json             ← 新增：TypeScript 配置
└── src/                      ← 内部实现模块
    ├── constants.ts          ← 插件级常量
    ├── runtime.ts            ← Runtime singleton + Logger + 全局状态
    ├── types.ts              ← TypeScript 类型定义
    ├── config.ts             ← Account 解析、配置读写
    ├── channel.ts            ← ChannelPlugin 定义（对接 SDK 接口）
    ├── onboarding.ts         ← Onboarding 交互流程
    ├── hooks.ts              ← before_tool_call / after_tool_call 钩子
    ├── bridge/
    │   ├── client.ts         ← BridgeClient class（WebSocket 传输层）
    │   ├── media.ts          ← Bridge REST API（媒体上传/下载）
    │   └── monitor.ts        ← Bridge 连接生命周期管理
    └── messaging/
        ├── handlers.ts       ← 消息 Handler 策略集（text/image/audio/video/file）
        ├── inbound.ts        ← 入站消息处理（JSON-RPC dispatch + streaming）
        ├── outbound.ts       ← 出站消息发送（sendText/sendMedia）
        └── target.ts         ← Target 地址归一化
```

> **为什么 `index.ts` 在根目录？**
> 遵循 ADP-OpenClaw 的包约定——入口文件是包的**公共接口**，`package.json` 的 `openclaw.extensions` 直接指向它；`src/` 目录是**内部实现**，外部不应直接引用。入口文件仅做接线（~20 行），不含业务逻辑。

### 2.2 模块依赖关系

```
index.ts (包入口，根目录)
├── src/runtime.ts          (setRuntime, setLogger)
├── src/channel.ts          (astronClawPlugin)
│   ├── src/config.ts       (resolveAccount, listAccountIds, ...)
│   ├── src/onboarding.ts   (astronClawOnboarding)
│   ├── src/messaging/target.ts     (normalizeTarget, isGroupTarget)
│   ├── src/messaging/outbound.ts   (sendTextMessage, sendMediaMessage)
│   │   ├── src/bridge/media.ts     (uploadMediaToBridge)
│   │   └── src/runtime.ts          (activeBridgeClients)
│   └── src/bridge/monitor.ts       (monitorBridgeProvider) ← 动态 import
│       ├── src/bridge/client.ts    (BridgeClient)
│       ├── src/messaging/inbound.ts(handleInboundMessage)
│       │   ├── src/messaging/handlers.ts (findHandler)
│       │   ├── src/bridge/media.ts      (downloadMediaFromBridge)
│       │   └── src/runtime.ts           (getRuntime, activeSessionCtx)
│       └── src/runtime.ts          (recordState, activeBridgeClients)
└── src/hooks.ts            (registerToolHooks)
    └── src/runtime.ts      (activeSessionCtx, pendingToolCtx)
```

---

## 3. 各模块详细设计

### 3.1 `src/constants.ts`

提取所有硬编码常量。

```ts
export const PLUGIN_ID = "astron-claw";
export const PLUGIN_VERSION = "2.0.0";
export const DEFAULT_ACCOUNT_ID = "default";
export const DEFAULT_BRIDGE_URL = "ws://localhost:8765/bridge/bot";

// Retry
export const DEFAULT_RETRY_BASE_MS = 1000;
export const DEFAULT_RETRY_MAX_MS = 60000;
export const DEFAULT_RETRY_MAX_ATTEMPTS = 0;

// Liveness
export const LIVENESS_PING_INTERVAL_MS = 15000;
export const LIVENESS_TIMEOUT_MS = 60000;

// Media
export const MEDIA_MAX_SIZE_DEFAULT = 50 * 1024 * 1024;
export const MEDIA_ALLOWED_TYPES_DEFAULT = [
  "image/*", "audio/*", "video/*",
  "application/pdf", "application/zip",
  "text/plain", "application/octet-stream",
];
```

### 3.2 `src/types.ts`

定义共享类型，为所有模块提供类型契约。

```ts
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
```

### 3.3 `src/runtime.ts`

集中管理所有全局可变状态，对外暴露 getter/setter 函数。

```ts
// Runtime singleton
let _runtime: PluginRuntime | null = null;
export function setRuntime(rt: PluginRuntime) { ... }
export function getRuntime(): PluginRuntime | null { ... }

// Logger
let _logger: Logger = console;
export function setLogger(l: Logger) { ... }
export const logger = { info, warn, error, debug };

// Session context（per-sessionKey）
export const activeSessionCtx = new Map<string, SessionContext>();

// Pending tool context（SDK workaround）
export const pendingToolCtx = new Map<string, SessionContext & { _sk: string }>();
export function toolCtxKey(toolName: string, params: unknown): string { ... }

// Channel runtime state（per-accountId）
const runtimeState = new Map<string, ChannelRuntimeState>();
export function recordChannelRuntimeState(accountId: string, updates: Partial<ChannelRuntimeState>) { ... }
export function getChannelRuntimeState(accountId: string): ChannelRuntimeState { ... }

// Active bridge clients（per-accountId）
export const activeBridgeClients = new Map<string, BridgeClient>();
```

### 3.4 `src/config.ts`

账号解析与配置读写，对应原文件 83-128 行。

```ts
export function resolveAccountFromCfg(cfg: ClawdbotConfig): ResolvedAccount { ... }
export function resolveAccount(): ResolvedAccount | null { ... }

// 辅助
function readStr(v: unknown): string | undefined { ... }
function readNum(v: unknown): number | undefined { ... }
```

### 3.5 `src/bridge/client.ts`

纯传输层，对应原文件 479-665 行 `BridgeClient` class。

- 职责：WebSocket 连接管理、心跳、重连、认证失败检测
- 不含任何业务逻辑
- 通过 `onMessage` 回调将原始 JSON 传递给上层

### 3.6 `src/bridge/media.ts`

Bridge REST API 客户端，对应原文件 160-231 行。

```ts
export function getBridgeHttpBaseUrl(wsUrl: string): string { ... }
export async function downloadMediaFromBridge(account: ResolvedAccount, mediaId: string): Promise<MediaDownloadResult> { ... }
export async function uploadMediaToBridge(account: ResolvedAccount, buffer: Buffer, fileName: string, contentType: string): Promise<MediaUploadResult> { ... }
export function inferMediaType(mimeType: string): "image" | "audio" | "video" | "file" { ... }
```

### 3.7 `src/bridge/monitor.ts`

Bridge 连接生命周期管理，对应原文件 1169-1254 行。

```ts
export function monitorBridgeProvider(account: ResolvedAccount, abortSignal?: AbortSignal): Promise<void> { ... }
export async function probeBridgeServer(account: ResolvedAccount): Promise<ProbeResult> { ... }
```

Gateway `startAccount` 中通过 **动态 import** 加载此模块（与 ADP-OpenClaw 一致）：
```ts
gateway: {
  startAccount: async (ctx) => {
    const { monitorBridgeProvider } = await import("./bridge/monitor.js");
    return monitorBridgeProvider(ctx.account, ctx.abortSignal);
  },
},
```

### 3.8 `src/messaging/handlers.ts`

消息处理策略集，对应原文件 233-477 行。

**关键优化**：提取公共媒体下载+保存逻辑，消除四个媒体 handler 的重复代码。

```ts
// 公共媒体处理
async function downloadAndSaveMedia(account: ResolvedAccount, mediaId: string): Promise<MediaItem> { ... }

// Handler 接口
type MessageHandler = {
  canHandle: (data: any) => boolean;
  getPreview: (data: any) => string;
  validate: (data: any) => ValidationResult;
  handle: (data: any, account: ResolvedAccount) => Promise<HandleResult>;
};

// 各 handler 实现
export const textMessageHandler: MessageHandler = { ... };
export const imageMessageHandler: MessageHandler = { ... };
export const audioMessageHandler: MessageHandler = { ... };
export const videoMessageHandler: MessageHandler = { ... };
export const fileMessageHandler: MessageHandler = { ... };
export const unsupportedMessageHandler: MessageHandler = { ... };

export function findHandler(data: any): MessageHandler { ... }
```

### 3.9 `src/messaging/inbound.ts`

入站消息处理，对应原文件 667-1054 行。这是最复杂的模块（~305 行），包含：

- `handleInboundMessage()` — 消息路由分发
- `handleJsonRpcPrompt()` — JSON-RPC session/prompt 处理 + streaming delta 计算
- `handleDirectMessage()` — 直发消息处理
- `createReplyDispatcher()` — 回复分发器

### 3.10 `src/messaging/outbound.ts`

出站消息发送，对应原文件 1085-1167 行。

```ts
export async function sendTextMessage(to: string, text: string, ctx: OutboundContext): Promise<void> { ... }
export async function sendMediaMessage(to: string, mediaUrl: string, options: MediaOptions, ctx: OutboundContext): Promise<void> { ... }
```

### 3.11 `src/messaging/target.ts`

地址归一化，对应原文件 1056-1083 行。

```ts
export function normalizeTarget(raw: string | null): string | null { ... }
export function isGroupTarget(target: string | null): boolean { ... }
```

### 3.12 `src/onboarding.ts`

引导配置流程，对应原文件 1256-1344 行。

```ts
export const astronClawOnboarding = {
  getStatus: async () => { ... },
  configure: async (interaction) => { ... },
  disable: async () => { ... },
};
```

### 3.13 `src/hooks.ts`

SDK 事件钩子，对应原文件 1649-1700 行。

```ts
export function registerToolHooks(api: OpenClawPluginApi): void {
  api.on("before_tool_call", (event, ctx) => { ... });
  api.on("after_tool_call", (event, ctx) => { ... });
}
```

### 3.14 `src/channel.ts`

ChannelPlugin 对象定义，对应原文件 1346-1630 行。纯胶水层，引用其他模块：

```ts
import { resolveAccountFromCfg } from "./config.js";
import { astronClawOnboarding } from "./onboarding.js";
import { normalizeTarget, isGroupTarget } from "./messaging/target.js";
import { sendTextMessage, sendMediaMessage } from "./messaging/outbound.js";

export const astronClawPlugin: ChannelPlugin<ResolvedAccount> = {
  id: PLUGIN_ID,
  meta: { ... },
  capabilities: { ... },
  onboarding: astronClawOnboarding,
  config: { ... },        // 调用 config.ts
  outbound: { ... },      // 调用 outbound.ts
  messaging: { ... },     // 调用 target.ts
  security: { ... },      // 调用 config.ts
  gateway: { ... },       // 动态 import monitor.ts
  status: { ... },        // 调用 monitor.ts probeBridgeServer
};
```

### 3.15 `index.ts`（根目录）

包入口，精简至 ~20 行（与 ADP-OpenClaw 风格一致）：

```ts
import type { OpenClawPluginApi } from "openclaw/plugin-sdk";
import { PLUGIN_ID, PLUGIN_VERSION } from "./src/constants.js";
import { setRuntime, setLogger, logger } from "./src/runtime.js";
import { astronClawPlugin } from "./src/channel.js";
import { registerToolHooks } from "./src/hooks.js";

const plugin = {
  id: PLUGIN_ID,
  name: PLUGIN_ID,
  version: PLUGIN_VERSION,
  description: "AstronClaw channel plugin - connects chat clients via bridge server to OpenClaw.",
  register(api: OpenClawPluginApi) {
    setRuntime(api.runtime);
    setLogger(api.runtime?.logger ?? api.logger);
    api.registerChannel({ plugin: astronClawPlugin });
    registerToolHooks(api);
    logger.info(`AstronClaw v${PLUGIN_VERSION} registered as channel plugin`);
  },
};

export default plugin;
```

---

## 4. TypeScript 引入方案

### 4.1 tsconfig.json

```json
{
  "compilerOptions": {
    "target": "ES2022",
    "module": "ES2022",
    "moduleResolution": "bundler",
    "strict": true,
    "noEmit": true,
    "esModuleInterop": true,
    "skipLibCheck": true,
    "declaration": false,
    "outDir": "dist"
  },
  "include": ["index.ts", "src/**/*.ts"]
}
```

> OpenClaw 插件宿主原生支持 `.ts` 文件（ADP-OpenClaw 已验证），`noEmit: true` 表示无需显式编译步骤——宿主通过内置 ESM loader 在运行时转译。

### 4.2 package.json 变更

```diff
 {
   "name": "astron-claw",
-  "version": "2.0.0",
+  "version": "2.1.0",
   "type": "module",
-  "main": "dist/index.js",
+  "main": "index.ts",
   "openclaw": {
     "extensions": [
-      "./dist/index.js"
+      "./index.ts"
     ]
   },
   "dependencies": {
     "ws": "^8.18.0"
+  },
+  "devDependencies": {
+    "@types/ws": "^8.5.10"
   }
 }
```

---

## 5. 迁移策略

采用**逐模块提取**方式，每次提取一个模块并验证功能不受影响：

| 阶段 | 操作 | 验证点 |
|------|------|--------|
| **Phase 0** | 创建 `src/` 目录结构、`tsconfig.json`；将原 `dist/index.js` 拆为根目录 `index.ts` + `src/` 模块骨架；更新 `package.json` 指向 `./index.ts` | 插件能加载 |
| **Phase 1** | 提取 `constants.ts`、`types.ts`、`runtime.ts` | 插件能加载，全局状态正常 |
| **Phase 2** | 提取 `config.ts`、`messaging/target.ts` | 账号解析、地址归一化正常 |
| **Phase 3** | 提取 `bridge/client.ts`、`bridge/media.ts` | WebSocket 连接、媒体上传下载正常 |
| **Phase 4** | 提取 `messaging/handlers.ts`（含去重） | 各类型消息处理正常 |
| **Phase 5** | 提取 `messaging/inbound.ts`、`messaging/outbound.ts` | 消息收发 + streaming 正常 |
| **Phase 6** | 提取 `bridge/monitor.ts`、`onboarding.ts` | 连接生命周期、引导流程正常 |
| **Phase 7** | 提取 `src/channel.ts`、`src/hooks.ts`，精简根目录 `index.ts` 至 ~20 行 | 全功能回归验证 |

每个 Phase 完成后执行：
1. 启动 OpenClaw，确认插件注册成功
2. 通过 Web 前端发送消息，验证入站 + 出站 + streaming
3. 测试媒体上传/下载
4. 测试断线重连

---

## 6. 拆分前后对比

### 行数预估

| 模块 | 预估行数 | 说明 |
|------|----------|------|
| `index.ts`（根目录） | ~20 | 包入口，仅注册接线 |
| `src/constants.ts` | ~25 | 常量 |
| `src/types.ts` | ~50 | 类型定义 |
| `src/runtime.ts` | ~70 | 全局状态集中管理 |
| `src/config.ts` | ~60 | 账号解析 |
| `src/channel.ts` | ~250 | ChannelPlugin 定义 |
| `src/onboarding.ts` | ~80 | 引导流程 |
| `src/bridge/client.ts` | ~180 | WebSocket 传输 |
| `src/bridge/media.ts` | ~80 | REST 媒体 API |
| `src/bridge/monitor.ts` | ~80 | 连接监控 |
| `src/messaging/handlers.ts` | ~160 | 消息策略集（去重后） |
| `src/messaging/inbound.ts` | ~300 | 入站处理 |
| `src/messaging/outbound.ts` | ~80 | 出站发送 |
| `src/messaging/target.ts` | ~25 | 地址归一化 |
| `src/hooks.ts` | ~60 | SDK 钩子 |
| **合计** | **~1520** | 相比原 1706 行减少 ~11%（主要来自媒体 handler 去重） |

### 关键收益

| 维度 | 改造前 | 改造后 |
|------|--------|--------|
| **定位效率** | 在 1706 行中搜索 | 按职责直接进入对应文件 |
| **类型安全** | 无，靠注释 | TypeScript strict 模式 |
| **最大单文件** | 1706 行 | ~300 行（inbound.ts） |
| **修改影响面** | 改一处可能影响全局 | 模块边界隔离 |
| **代码复用** | 4 个媒体 handler 重复 | 公共 `downloadAndSaveMedia` |
| **全局状态** | 6 处散落 | `runtime.ts` 集中管理 |
| **构建依赖** | 无 | 无（宿主原生支持 TS） |
