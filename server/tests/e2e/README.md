# E2E / Integration Tests

这些是黑盒测试脚本，需要真实运行的服务器（`localhost:8765`）+ MySQL + Redis。

**不会**被 `pytest` 自动收集（已在 `pytest.ini` 中 ignore）。

## 前置条件

1. 启动服务器：`cd server && python3 run.py`
2. 确保 MySQL 和 Redis 可访问（参考 `server/.env`）

## 脚本说明

| 脚本 | 用途 | 运行方式 |
|---|---|---|
| `test_integration.py` | 全流程集成测试：Token API、Bot WS 鉴权、重复 bot 防护、媒体上传/下载、SSE Chat 消息流转 | `python3 test_integration.py` |

## 运行示例

```bash
cd server/tests/e2e
python3 test_integration.py
```
