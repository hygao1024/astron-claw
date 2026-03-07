# 技术方案：过滤 onPartialReply 中的 NO_REPLY 静默令牌

## 一、问题根因

OpenClaw SDK 定义了静默回复令牌 `SILENT_REPLY_TOKEN = "NO_REPLY"`。当 LLM 不需要回复用户时（例如 `message` 工具已经发送了回复），LLM 会输出 `NO_REPLY` 表示"无需回复"。

SDK 的过滤架构：

| 路径 | 是否自动过滤 | 机制 |
|------|-------------|------|
| `deliver` 回调 | ✅ 自动 | `normalizeReplyPayloadsForDelivery` → `isSilentReplyText` → 丢弃 |
| `onPartialReply` 回调 | ❌ 不过滤 | SDK 直接传 `cleanedText`，由各渠道自行处理 |

SDK 内置渠道（Web、Telegram、WhatsApp）在 `onPartialReply` 中**全部手动调用** `isSilentReplyPrefixText` 过滤。我们的插件缺少这一步，导致 `NO_REPLY` 被拆成 `"NO"` + `"_REPLY"` 两个 delta 原样发给 bridge，用户看到 "NO" 文本。

## 二、SDK 源码验证

### 2.1 SDK 静默令牌定义（`tokens.ts`）

```js
const SILENT_REPLY_TOKEN = "NO_REPLY";
const HEARTBEAT_TOKEN = "HEARTBEAT_OK";

// 完整匹配：/^\s*NO_REPLY\s*$/
function isSilentReplyText(text, token = SILENT_REPLY_TOKEN) {
  const escaped = escapeRegExp(token);
  return new RegExp(`^\\s*${escaped}\\s*$`).test(text);
}

// 前缀匹配（流式场景）："NO_REPLY".startsWith(normalized)
// "NO_" → true, "NO_R" → true, "NO_REPLY" → true
// "NO" → false（不含 "_"，被第一个 guard 拦截）
function isSilentReplyPrefixText(text, token = SILENT_REPLY_TOKEN) {
  const normalized = text.trimStart().toUpperCase();
  if (!normalized) return false;
  if (!normalized.includes("_")) return false;
  if (/[^A-Z_]/.test(normalized)) return false;
  return token.toUpperCase().startsWith(normalized);
}
```

### 2.2 SDK 内置渠道如何处理 `onPartialReply`

**Web 渠道**（`reply-Deht_wOB.js:77429`）：
```js
const handlePartialForTyping = async (payload) => {
  if (isSilentReplyPrefixText(payload.text, SILENT_REPLY_TOKEN)) return;
  const { text, skip } = normalizeStreamingText(payload);
  if (skip || !text) return;
  await params.typingSignals.signalTextDelta(text);
  return text;
};
```

**Telegram/WhatsApp 渠道**（`reply-Deht_wOB.js:80207`）：
```js
if (silentToken && (isSilentReplyText(trimmed, silentToken) || isSilentReplyPrefixText(trimmed, silentToken))) return;
```

所有内置渠道都在 `onPartialReply` 中**手动调用 `isSilentReplyPrefixText`** 过滤。SDK 的设计意图：`onPartialReply` 是低级流式回调，过滤责任在渠道侧。

### 2.3 `plugin-sdk` 公开导出

| API | 导出自 `openclaw/plugin-sdk` | 说明 |
|-----|---|---|
| `SILENT_REPLY_TOKEN` | ✅ | 常量 `"NO_REPLY"` |
| `isSilentReplyText` | ✅ | 完整匹配 |
| `isSilentReplyPrefixText` | ❌ 未导出 | 前缀匹配（内部使用） |
| `HEARTBEAT_TOKEN` | ❌ 未导出 | 常量 `"HEARTBEAT_OK"` |

## 三、修复策略

与 SDK 内置渠道保持一致：在 `onPartialReply` 和 `sendFinal` 处手动过滤静默令牌。

由于 `isSilentReplyPrefixText` 和 `HEARTBEAT_TOKEN` 未从 `plugin-sdk` 导出，需在插件中自行实现等价逻辑（仅 6 行，直接复刻 SDK 源码）。

### 关键设计：`onPartialReply` 接收的是累积文本

`onPartialReply` 的 `payload.text` 是**从头到当前 token 的累积全文**，不是单个 delta。因此过滤逻辑是：

```
累积文本是静默令牌前缀（如 "NO_"、"NO_RE"）→ 暂缓发送，等更多 token
累积文本完整匹配静默令牌（如 "NO_REPLY"）    → 不发送
累积文本不匹配任何前缀                        → 正常发送（含补发之前缓冲的 delta）
```

### 关于 `isSilentReplyPrefixText("NO")` 返回 `false` 的说明

SDK 的 `isSilentReplyPrefixText` 要求文本必须包含 `"_"` 才视为前缀。因此 `"NO"` 不会被 SDK 的前缀匹配拦截。

但在 `onPartialReply` 累积文本场景下，这不是问题：
- 当 `fullText = "NO"` 时，前缀匹配不拦截，delta `"NO"` 会被发送
- 当 `fullText = "NO_REPLY"` 时，完整匹配拦截，`lastPartialText` 仍为 `"NO"`
- LLM 结束时，`sendFinal` 检查 `lastPartialText = "NO"` — 不是完整匹配，会被发送

**问题**：用户仍会看到一个 `"NO"` chunk。这与 SDK Web 渠道行为一致（Web 渠道同样无法拦截首个 `"NO"` delta）。

### 更彻底的方案：自定义前缀匹配覆盖 `"NO"`

如果要完全消除 `"NO"` chunk，可以放宽前缀匹配条件（去掉 `includes("_")` 的 guard）：

```ts
function isSilentTokenPrefix(text: string): boolean {
  const normalized = text.trim().toUpperCase();
  if (!normalized) return false;
  if (/[^A-Z_]/.test(normalized)) return false;
  return SILENT_TOKENS.some(token => token.startsWith(normalized));
}
```

这样 `"NO"` 也会被视为 `"NO_REPLY"` 的前缀而暂缓发送。代价是所有以纯大写字母开头的文本（如 `"NOTICE: ..."` 的 `"NO"` 前缀）会被短暂延迟，但不会丢失内容。

## 四、改动范围

| 文件 | 改动 | 说明 |
|------|------|------|
| `plugin/src/messaging/inbound.ts` | ~25 行 | 新增过滤辅助函数；`onPartialReply` 增加静默检测；`deliver` final 和 fallback `sendFinal` 增加完整匹配检查 |

**无新文件、不改类型定义、不改 hooks.ts、不改 runtime.ts。**

## 五、详细实现

### 5.1 新增静默令牌过滤辅助函数

在 `inbound.ts` 文件顶部（import 区域之后）新增模块级辅助函数：

```ts
import { SILENT_REPLY_TOKEN, isSilentReplyText } from "openclaw/plugin-sdk";

// HEARTBEAT_TOKEN 未从 plugin-sdk 导出，手动定义
const HEARTBEAT_TOKEN = "HEARTBEAT_OK";
const SILENT_TOKENS = [SILENT_REPLY_TOKEN, HEARTBEAT_TOKEN];

/**
 * 流式前缀匹配（复刻 SDK 内部 isSilentReplyPrefixText 逻辑，去掉 includes("_") guard
 * 以覆盖首个 delta "NO" 的场景）。
 *
 * 当累积文本可能是某个静默令牌的前缀时返回 true，用于暂缓发送 chunk。
 */
function isSilentTokenPrefix(text: string): boolean {
  const normalized = text.trim().toUpperCase();
  if (!normalized) return false;
  // 仅含大写字母和下划线才可能是令牌前缀
  if (/[^A-Z_]/.test(normalized)) return false;
  return SILENT_TOKENS.some(token => token.startsWith(normalized));
}
```

### 5.2 修改 `onPartialReply` 回调

```ts
onPartialReply: async (payload: any) => {
  const fullText = payload?.text ?? "";
  if (!fullText) return;

  // 静默令牌过滤（与 SDK 内置渠道一致）
  if (isSilentReplyText(fullText, SILENT_REPLY_TOKEN)) return;
  if (isSilentTokenPrefix(fullText)) return;

  // 正常计算 delta 并发送
  let delta = fullText;
  if (fullText.startsWith(lastPartialText)) {
    delta = fullText.slice(lastPartialText.length);
  }
  if (!delta) return;
  lastPartialText = fullText;
  sendChunk(delta);
},
```

### 5.3 修改 `deliver` final 分支

```ts
if (kind === "final" || kind === undefined) {
  const finalText = text || lastPartialText;
  if (!isSilentReplyText(finalText, SILENT_REPLY_TOKEN)) {
    sendFinal(finalText);
  }
}
```

### 5.4 修改 fallback sendFinal

```ts
// Ensure final is sent even if SDK didn't call deliver with "final"
if (!finalSent && chunkCount > 0) {
  if (!isSilentReplyText(lastPartialText, SILENT_REPLY_TOKEN)) {
    sendFinal(lastPartialText);
  }
}
```

## 六、边界场景分析

| 场景 | `fullText` 序列 | `isSilentTokenPrefix` | 行为 |
|------|-----------------|----------------------|------|
| LLM 输出 `NO_REPLY` | `"NO"` → `"NO_REPLY"` | `true` → `isSilentReplyText: true` | 全程拦截，不发任何 chunk ✅ |
| LLM 输出 `HEARTBEAT_OK` | `"HEARTBEAT"` → `"HEARTBEAT_OK"` | `true` → `isSilentReplyText: true` | 全程拦截 ✅ |
| LLM 输出 `"No, I can't..."` | `"No"` → `"No,"` → ... | `false`（小写含 `,`） → `false` | 首个 delta 直接发送，无延迟 ✅ |
| LLM 输出 `"NOTICE: ..."` | `"NO"` → `"NOT"` → `"NOTI"` → `"NOTICE"` → `"NOTICE:"` | `true` → `true` → `true` → `true` → `false` | 暂缓到 `"NOTICE:"` 时补发全部 `"NOTICE:"` ✅ |
| LLM 输出 `"Hello world"` | `"Hello"` → `"Hello world"` | `false` | 首个 delta 直接发送 ✅ |
| LLM 输出 `"NO_REPLY is an email"` | `"NO"` → `"NO_"` → `"NO_R"` → ... → `"NO_REPLY"` → `"NO_REPLY "` → `"NO_REPLY i"` | 前缀匹配暂缓 → ... → `isSilentReplyText: true` → `false`（trim 后仍匹配）→ `false` | 暂缓到 `"NO_REPLY i"` 时补发全部文本 ✅ |
| `message` 工具后 LLM 输出 `NO_REPLY` | tool events + `"NO"` → `"NO_REPLY"` | 全程拦截 | 工具事件正常，残余 `NO_REPLY` 被过滤 ✅ |

**关于 `"NOTICE:"` 场景**：`"NO"` 全大写且仅含 `[A-Z_]`，被 `isSilentTokenPrefix` 暂缓。但下一个 token 到达后 `"NOTICE:"` 含 `":"`，不满足 `[A-Z_]` 约束，前缀匹配返回 false。此时 delta 计算为 `"NOTICE:".slice(0) = "NOTICE:"`（因 `lastPartialText` 仍为 `""`），完整补发。用户感知到的是首个 chunk 延迟几十毫秒，内容无丢失。

**关于 `"NO_REPLY is an email"` 场景**：当 `fullText = "NO_REPLY "` 时，`trim()` 后为 `"NO_REPLY"`，`isSilentReplyText` 返回 true，被拦截。当 `fullText = "NO_REPLY i"` 时，`trim().toUpperCase()` 为 `"NO_REPLY I"`，含空格不满足 `[A-Z_]`，前缀匹配返回 false；`isSilentReplyText` 也返回 false。此时补发全部文本 `"NO_REPLY i"`。

## 七、方案对比

| 方案 | 优点 | 缺点 |
|------|------|------|
| A. 与 SDK 完全一致（保留 `includes("_")` guard） | 行为与内置渠道完全一致 | 首个 delta `"NO"` 无法拦截，用户会短暂看到 "NO" |
| **B. 放宽前缀匹配（去掉 `includes("_")` guard）** | 完全消除 `"NO"` chunk | 纯大写文本首个 chunk 可能延迟几十 ms |

**推荐方案 B**：去掉 `includes("_")` guard。代价极小（纯大写开头的文本延迟几十 ms），收益明确（用户完全看不到 "NO"）。

## 八、验证计划

1. **构建检查**：plugin 目录下 TypeScript 编译通过
2. **场景 A**：发送触发 `message` 工具的消息 → `tool_result` 后不出现 "NO" chunk
3. **场景 B**：发送触发纯文本回复的消息 → 正常流式输出
4. **场景 C**：发送触发以 `"No"` 开头回复的消息 → 首个 chunk 正常（小写不受影响）
5. **场景 D**：两个 session 同时对话 → 各自独立过滤，互不影响
6. **场景 E**：发送触发 `HEARTBEAT_OK` 的消息 → 被过滤
