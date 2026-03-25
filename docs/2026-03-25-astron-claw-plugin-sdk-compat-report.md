# AstronClaw 插件 `openclaw/plugin-sdk` 兼容性修复报告

日期：2026-03-25

## 1. 背景

在 OpenClaw `2026.3.23` 环境中安装 `astron-claw` 插件时，插件加载失败，报错如下：

```text
[plugins] astron-claw failed to load from /root/.openclaw/extensions/astron-claw/index.ts:
Error: Cannot find module 'openclaw/plugin-sdk'
```

同一插件在部分本机环境可以正常安装和加载，因此需要确认这是插件自身缺陷，还是宿主版本与插件加载方式之间的兼容性问题。

## 2. 现象与影响

### 2.1 现象

- 插件源码以 TypeScript 源文件形式安装到 `/root/.openclaw/extensions/astron-claw`
- 宿主通过 `index.ts` 加载插件
- 插件在加载阶段解析 `openclaw/plugin-sdk` 失败

### 2.2 影响

- 插件无法注册 channel
- 安装流程中断或完成但功能不可用
- `AstronClaw` channel 无法出现在 OpenClaw 运行时

## 3. 根因分析

### 3.1 直接原因

插件以下文件使用了对 `openclaw/plugin-sdk` root surface 的静态导入：

- `plugin/src/messaging/inbound.ts`
- `plugin/src/messaging/outbound.ts`
- `plugin/src/messaging/handlers.ts`

这要求宿主插件加载器在“源码插件 + TypeScript + 静态 ESM import”的场景下，必须正确为 `openclaw/plugin-sdk` 提供 alias 或等价解析。

### 3.2 宿主侧兼容性特征

OpenClaw `2026.3.x` 源码中确实存在将 `openclaw/plugin-sdk` 映射到 `root-alias.cjs` 的逻辑，因此设计意图是支持插件通过公共 SDK surface 访问运行时能力。

但现有 loader 测试主要覆盖了两类场景：

- `require("openclaw/plugin-sdk")` 的 CJS 场景
- 源码插件内部的相对导入场景

缺少针对“源码插件 + TypeScript + 静态 `import { ... } from "openclaw/plugin-sdk"`”的显式回归覆盖。

### 3.3 为什么本机可能正常、目标机异常

这类问题通常与以下因素有关：

- 宿主 OpenClaw 版本差异
- 插件加载器实现差异
- 安装方式差异
- 运行时 alias 生效路径差异

因此，这不是一个适合继续依赖宿主兼容层的点。最稳妥的修复是让插件不再依赖 `openclaw/plugin-sdk` root import 是否能被宿主正确接管。

## 4. 修复目标

修复方案要求满足以下条件：

- 在 OpenClaw `2026.3.23` 环境中可正常加载
- 不依赖宿主对 `openclaw/plugin-sdk` root surface 的兼容性
- 不要求安装后重新 `npm install`
- 尽量保持现有插件业务逻辑不变

## 5. 最终修复方案

采用“插件侧兼容层”方案。

### 5.1 方案概要

新增插件内部兼容模块 `plugin/src/sdk-compat.ts`，把原本直接从 `openclaw/plugin-sdk` 获取的能力，收敛为插件本地实现或运行时适配：

- `SILENT_REPLY_TOKEN`
- `HEARTBEAT_TOKEN`
- `isSilentReplyText`
- `extensionForMime`
- `loadMedia`

其中：

- `loadMedia` 优先调用宿主在 `api.runtime` 中注入的 media loader
- 若宿主 runtime 未提供对应能力，则回退到插件内部 fallback 实现
- 其余常量和工具函数由插件本地维护，避免 root SDK 解析依赖

### 5.2 修改文件

- 新增：`plugin/src/sdk-compat.ts`
- 修改：`plugin/src/messaging/inbound.ts`
- 修改：`plugin/src/messaging/outbound.ts`
- 修改：`plugin/src/messaging/handlers.ts`
- 新增回归测试：`plugin/tests/test_no_root_sdk_imports.ts`

### 5.3 关键设计点

1. 不再从 `openclaw/plugin-sdk` root surface 静态 import

这一步直接切断了本次兼容性问题的触发条件。

2. 媒体加载保留宿主能力优先

为了不损失宿主已有的 media runtime 行为，`loadMedia` 先尝试使用：

```ts
api.runtime.media.loadWebMedia
```

若该能力不存在，则回退到本地实现，兼容 HTTP URL 与本地文件路径。

3. 静默回复逻辑保留

原先依赖 SDK 的静默令牌逻辑仍然保留，只是实现位置从宿主 SDK 切换到了插件内部兼容层。

## 6. 验证过程

### 6.1 回归测试

新增测试：

- `plugin/tests/test_no_root_sdk_imports.ts`

测试目标：

- 确认 `inbound.ts`
- 确认 `outbound.ts`
- 确认 `handlers.ts`

均不再包含对 `openclaw/plugin-sdk` root surface 的静态导入。

### 6.2 既有测试验证

已执行并通过：

```text
../clawdbot-feishu/node_modules/.bin/tsx plugin/tests/test_no_root_sdk_imports.ts
../clawdbot-feishu/node_modules/.bin/tsx plugin/tests/test_agent_end_abort_error.ts
../clawdbot-feishu/node_modules/.bin/tsx plugin/tests/test_stale_socket_events.ts
../clawdbot-feishu/node_modules/.bin/tsx plugin/tests/test_unexpected_response_reconnect.ts
../clawdbot-feishu/node_modules/.bin/tsx plugin/tests/test_eviction_pingpong.ts
```

### 6.3 目标环境安装验证

在 OpenClaw `2026.3.23` 机器上的安装日志显示：

- 插件成功注册
- `AstronClaw v0.1.0 registered as channel plugin`
- 安装脚本完成并提示安装成功

这说明原始报错 `Cannot find module 'openclaw/plugin-sdk'` 已不再出现，兼容性问题已被实际消除。

## 7. 安装日志中的剩余告警说明

### 7.1 `tar` 时间戳告警

日志中大量出现：

```text
time stamp ... is XX s in the future
```

这不是插件功能问题，而是打包机与解包机时间存在数十秒偏差，导致归档中文件时间略超前。

影响：

- 不影响插件加载
- 不影响安装结果
- 仅影响日志整洁度

### 7.2 `duplicate plugin id detected`

该告警来自安装器将旧版本备份到：

```text
/root/.openclaw/extensions/astron-claw.bak.*
```

而宿主同时扫描了备份目录，导致发现重复插件 ID。

影响：

- 新插件仍然成功加载
- 但日志会出现重复插件告警

### 7.3 `plugin disabled (disabled in config) but config is present`

这是安装流程中的中间态提示，后续流程已经执行了启用操作：

```text
Enabled plugin "astron-claw"
```

因此这不是最终失败状态。

## 8. 结论

本次问题的根因不是 `astron-claw` 业务逻辑错误，而是插件对 `openclaw/plugin-sdk` root surface 的静态依赖，暴露了宿主在“源码插件 + TypeScript + 静态 ESM import”场景下的兼容性缺口。

最终修复通过插件内部兼容层彻底绕开该依赖点，结果如下：

- OpenClaw `2026.3.23` 环境下已可成功安装与加载
- 原始错误不再出现
- 现有关键插件测试全部通过
- 安装包可继续保留 `node_modules`，无需重新安装依赖

## 9. 后续建议

### 9.1 插件侧

- 继续避免直接依赖 `openclaw/plugin-sdk` root surface
- 如果未来需要新增宿主能力，优先通过 `api.runtime` 注入面访问

### 9.2 安装与打包侧

- 保留 `plugin/node_modules` 以减少安装耗时
- 打包时统一文件时间戳，消除 `tar` 的 future timestamp 告警
- 安装器备份目录移出 `extensions` 扫描路径，避免 duplicate plugin id 告警

### 9.3 宿主侧

- 为 OpenClaw loader 增加针对“源码插件 + TypeScript + 静态 `import` root SDK”的回归测试
- 明确插件开发建议，避免 root surface 在跨版本场景下成为不稳定依赖点
