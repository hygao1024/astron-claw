# E2E / Integration Tests

这些是黑盒测试脚本，需要真实运行的服务器（`localhost:8765`）+ MySQL + Redis。

**不会**被 `pytest` 自动收集（已在 `pytest.ini` 中 ignore）。

## 前置条件

1. 启动服务器：`cd server && python3 run.py`
2. 确保 MySQL 和 Redis 可访问（参考 `server/.env`）

## 脚本说明

| 脚本 | 用途 | 运行方式 |
|---|---|---|
| `test_integration.py` | 全流程集成测试：Token API、WS 鉴权、消息流转、流式响应、媒体上传/下载、重复 bot 防护 | `python3 test_integration.py` |
| `test_streaming.py` | 模拟 bot 发送多个 chunk，验证 chat 端逐条实时接收（非合并） | `python3 test_streaming.py` |
| `test_e2e_streaming.py` | 使用真实 plugin token 连接，验证端到端流式响应 | `python3 test_e2e_streaming.py` |

## 运行示例

```bash
cd server/tests/e2e
python3 test_integration.py
```
