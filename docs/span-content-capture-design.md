# Span 内容采集设计：用户输入与 Bot 回复

## 背景

当前 OTel span 只记录了消息长度 (`astron.message_size`)，没有记录实际的用户输入和 bot 回复内容。排查问题时无法直接看到对话内容，需要在 span attribute 中采集这些信息。

## 设计方案

### 核心逻辑

- 内容 <= 1024 字节：直接写入 span attribute
- 内容 > 1024 字节：上传到 S3，span attribute 存 S3 URL
- S3 上传失败：降级为截断内容（前 1024 字节 + `...(truncated)`），不影响主流程

### S3 Key 格式

```
otel/{uuid}.txt
```

使用 UUID 命名，扁平结构，无需按 traceID 分目录。

### Span Attribute

| Span | Attribute | 内容 |
|------|-----------|------|
| `chat.turn` | `astron.user_input` | 用户发送的文本内容 |
| `chat.response.stream` | `astron.bot_reply` | Bot 回复的完整文本（所有 chunk 拼接） |

### 实现要点

1. **用户输入**：在 `turnSpan.SetAttributes` 之后、进入 SSE 流之前同步记录
2. **Bot 回复**：在流式循环中用 `strings.Builder` 累积所有 chunk 内容，流结束时在 `streamSpan` 的 defer 中记录
3. **Helper 函数**：`storeSpanContent(content, span, attrKey)` 封装阈值判断、S3 上传、降级逻辑

### 修改文件

- `backend/internal/router/sse.go`
