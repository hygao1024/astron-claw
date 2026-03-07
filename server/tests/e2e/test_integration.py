#!/usr/bin/env python3
"""
End-to-end integration test for Astron Claw.
Tests: token API, bot WS, media upload/download, SSE chat message flow.

NOTE: Requires a running server at localhost:8765 with MySQL + Redis.
"""

import asyncio
import io
import json
import sys
import urllib.request
import urllib.error
import websockets

SERVER = "http://localhost:8765"
WS_SERVER = "ws://localhost:8765"


def http_post(url, data=None, headers=None):
    """Simple sync HTTP POST using urllib."""
    body = json.dumps(data).encode() if data else b""
    hdrs = {"Content-Type": "application/json"}
    if headers:
        hdrs.update(headers)
    req = urllib.request.Request(url, data=body, headers=hdrs, method="POST")
    with urllib.request.urlopen(req) as resp:
        return json.loads(resp.read().decode())


def http_get(url, headers=None):
    """Simple sync HTTP GET using urllib."""
    hdrs = headers or {}
    req = urllib.request.Request(url, headers=hdrs, method="GET")
    with urllib.request.urlopen(req) as resp:
        return resp.read(), resp.headers


def http_post_stream(url, data=None, headers=None, timeout=10):
    """POST that returns the raw response for streaming SSE reads."""
    body = json.dumps(data).encode() if data else b""
    hdrs = {"Content-Type": "application/json"}
    if headers:
        hdrs.update(headers)
    req = urllib.request.Request(url, data=body, headers=hdrs, method="POST")
    return urllib.request.urlopen(req, timeout=timeout)


def multipart_upload(url, file_name, file_data, mime_type, token):
    """Upload a file using multipart/form-data."""
    boundary = "----AstronClawTestBoundary12345"
    body = io.BytesIO()

    # File part
    body.write(f"--{boundary}\r\n".encode())
    body.write(f'Content-Disposition: form-data; name="file"; filename="{file_name}"\r\n'.encode())
    body.write(f"Content-Type: {mime_type}\r\n\r\n".encode())
    body.write(file_data)
    body.write(b"\r\n")
    body.write(f"--{boundary}--\r\n".encode())

    data = body.getvalue()
    req = urllib.request.Request(
        url,
        data=data,
        headers={
            "Content-Type": f"multipart/form-data; boundary={boundary}",
            "Authorization": f"Bearer {token}",
        },
        method="POST",
    )
    with urllib.request.urlopen(req) as resp:
        return json.loads(resp.read().decode())


PASS = "\033[92mPASS\033[0m"
FAIL = "\033[91mFAIL\033[0m"
results = []


def report(name, ok, detail=""):
    tag = PASS if ok else FAIL
    results.append(ok)
    msg = f"  [{tag}] {name}"
    if detail:
        msg += f"  ({detail})"
    print(msg)


async def main():
    print("\n=== Astron Claw Integration Test ===\n")

    # ── 1. Test Token API ──
    print("1. Token API")
    token_data = http_post(f"{SERVER}/api/token")
    token = token_data.get("token", "")
    report("POST /api/token", token.startswith("sk-"), f"token={token[:16]}...")

    vdata = http_post(f"{SERVER}/api/token/validate", {"token": token})
    report("POST /api/token/validate (valid)", vdata.get("valid") is True)

    vdata = http_post(f"{SERVER}/api/token/validate", {"token": "sk-bad"})
    report("POST /api/token/validate (invalid)", vdata.get("valid") is False)

    # ── 2. Test WebSocket auth rejection (bot) ──
    print("\n2. WebSocket Auth")
    try:
        async with websockets.connect(f"{WS_SERVER}/bridge/bot?token=sk-bad") as ws_test:
            try:
                msg = await asyncio.wait_for(ws_test.recv(), timeout=3)
                report("WS /bridge/bot bad token rejects", False, f"got message: {msg[:60]}")
            except websockets.exceptions.ConnectionClosed as e:
                report("WS /bridge/bot bad token rejects", e.code == 4001, f"code={e.code}")
    except Exception as e:
        report("WS /bridge/bot bad token rejects", "4001" in str(e) or "403" in str(e), str(e)[:60])

    # ── 3. Test Bot WS connects ──
    print("\n3. Bot WebSocket Connection")
    try:
        bot_ws = await websockets.connect(
            f"{WS_SERVER}/bridge/bot?token={token}",
            additional_headers={"X-Astron-Bot-Token": token}
        )
        report("Bot WS connects", True)
    except Exception as e:
        report("Bot WS connects", False, str(e)[:80])
        print("\nCannot proceed without bot connection.")
        _summarize()
        return

    await bot_ws.close()

    # ── 4. Duplicate bot prevention ──
    print("\n4. Duplicate Bot Prevention")
    token2 = http_post(f"{SERVER}/api/token")["token"]

    bot1 = await websockets.connect(f"{WS_SERVER}/bridge/bot?token={token2}")
    try:
        bot2 = await websockets.connect(f"{WS_SERVER}/bridge/bot?token={token2}")
        # Should receive error and close
        raw = await asyncio.wait_for(bot2.recv(), timeout=3)
        msg = json.loads(raw)
        await bot2.close()
        report("Second bot rejected", "error" in msg or "already" in msg.get("error", "").lower(), f"got: {json.dumps(msg)[:60]}")
    except (websockets.exceptions.ConnectionClosedError, websockets.exceptions.ConnectionClosed) as e:
        report("Second bot rejected", True, str(e)[:60])
    except Exception as e:
        report("Second bot rejected", False, str(e)[:80])
    finally:
        await bot1.close()

    # ── 5. Media Upload API ──
    print("\n5. Media Upload API")

    # Create a fresh token for media tests
    token3 = http_post(f"{SERVER}/api/token")["token"]

    # Test upload with valid token
    try:
        test_image_data = b"\x89PNG\r\n\x1a\n" + b"\x00" * 100  # Minimal PNG-like data
        result = multipart_upload(
            f"{SERVER}/api/media/upload",
            "test_image.png",
            test_image_data,
            "image/png",
            token3,
        )
        media_id = result.get("mediaId", "")
        report(
            "Media upload succeeds",
            media_id.startswith("media_")
            and result.get("fileName") == "test_image.png"
            and result.get("mimeType") == "image/png"
            and result.get("fileSize") == len(test_image_data)
            and "downloadUrl" in result,
            f"mediaId={media_id[:20]}..., size={result.get('fileSize')}"
        )
    except Exception as e:
        media_id = ""
        report("Media upload succeeds", False, str(e)[:80])

    # Test upload with invalid token
    try:
        multipart_upload(
            f"{SERVER}/api/media/upload",
            "bad.png",
            b"data",
            "image/png",
            "sk-invalid-token",
        )
        report("Media upload rejects bad token", False, "should have failed")
    except urllib.error.HTTPError as e:
        report("Media upload rejects bad token", e.code == 401, f"status={e.code}")
    except Exception as e:
        report("Media upload rejects bad token", False, str(e)[:80])

    # ── 6. Media Download API ──
    print("\n6. Media Download API")

    if media_id:
        # Download with valid token via query param
        try:
            download_url = f"{SERVER}/api/media/download/{media_id}?token={token3}"
            data, headers = http_get(download_url)
            report(
                "Media download succeeds (query param)",
                len(data) == len(test_image_data),
                f"downloaded {len(data)} bytes, content-type={headers.get('content-type', '')}"
            )
        except Exception as e:
            report("Media download succeeds (query param)", False, str(e)[:80])

        # Download with valid token via header
        try:
            download_url = f"{SERVER}/api/media/download/{media_id}"
            data, headers = http_get(download_url, {"Authorization": f"Bearer {token3}"})
            report(
                "Media download succeeds (auth header)",
                len(data) == len(test_image_data),
                f"downloaded {len(data)} bytes"
            )
        except Exception as e:
            report("Media download succeeds (auth header)", False, str(e)[:80])

        # Download with invalid token
        try:
            download_url = f"{SERVER}/api/media/download/{media_id}?token=sk-bad"
            http_get(download_url)
            report("Media download rejects bad token", False, "should have failed")
        except urllib.error.HTTPError as e:
            report("Media download rejects bad token", e.code == 401, f"status={e.code}")
        except Exception as e:
            report("Media download rejects bad token", False, str(e)[:80])

        # Download non-existent media
        try:
            download_url = f"{SERVER}/api/media/download/media_nonexistent?token={token3}"
            http_get(download_url)
            report("Media download 404 for missing ID", False, "should have failed")
        except urllib.error.HTTPError as e:
            report("Media download 404 for missing ID", e.code == 404, f"status={e.code}")
        except Exception as e:
            report("Media download 404 for missing ID", False, str(e)[:80])
    else:
        report("Media download succeeds (query param)", False, "skipped - no media_id")
        report("Media download succeeds (auth header)", False, "skipped - no media_id")
        report("Media download rejects bad token", False, "skipped - no media_id")
        report("Media download 404 for missing ID", False, "skipped - no media_id")

    # ── 7. Upload Validation ──
    print("\n7. Upload Validation")

    token6 = http_post(f"{SERVER}/api/token")["token"]

    # Upload empty file
    try:
        multipart_upload(
            f"{SERVER}/api/media/upload",
            "empty.txt",
            b"",
            "text/plain",
            token6,
        )
        report("Empty file upload rejected", False, "should have failed")
    except urllib.error.HTTPError as e:
        report("Empty file upload rejected", e.code == 400, f"status={e.code}")
    except Exception as e:
        report("Empty file upload rejected", False, str(e)[:80])

    # Upload file with valid type
    try:
        result = multipart_upload(
            f"{SERVER}/api/media/upload",
            "document.pdf",
            b"%PDF-1.4 fake pdf content for testing",
            "application/pdf",
            token6,
        )
        report(
            "PDF upload succeeds",
            result.get("mediaId", "").startswith("media_")
            and result.get("mimeType") == "application/pdf",
            f"mediaId={result.get('mediaId', '')[:20]}..."
        )
    except Exception as e:
        report("PDF upload succeeds", False, str(e)[:80])

    # ── 8. SSE Chat Message Flow ──
    print("\n8. SSE Chat Message Flow")

    token7 = http_post(f"{SERVER}/api/token")["token"]

    # Connect a bot that echoes back
    try:
        echo_bot = await websockets.connect(f"{WS_SERVER}/bridge/bot?token={token7}")
        report("Echo bot connects", True)
    except Exception as e:
        report("Echo bot connects", False, str(e)[:80])
        echo_bot = None

    if echo_bot:
        # SSE POST without bot → should work since bot is connected
        # Run SSE request + bot echo in parallel
        async def bot_echo_loop(ws):
            """Read one JSON-RPC request from bot inbox, send chunks + done."""
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=5)
                rpc = json.loads(raw)
                rpc_id = rpc.get("rpc_request", rpc).get("id", rpc.get("id", ""))
                session_id = rpc.get("rpc_request", rpc).get("params", {}).get("sessionId", "")
                # Send chunk notifications
                for text in ["Hello", " World"]:
                    await ws.send(json.dumps({
                        "method": "session/update",
                        "params": {
                            "sessionId": session_id,
                            "update": {
                                "sessionUpdate": "agent_message_chunk",
                                "content": {"text": text},
                            },
                        },
                    }))
                # Send final
                await ws.send(json.dumps({
                    "method": "session/update",
                    "params": {
                        "sessionId": session_id,
                        "update": {
                            "sessionUpdate": "agent_message_final",
                            "content": {"text": "Hello World"},
                        },
                    },
                }))
                # Send JSON-RPC result
                await ws.send(json.dumps({
                    "jsonrpc": "2.0",
                    "id": rpc_id,
                    "result": {"stopReason": "end_turn"},
                }))
            except Exception as exc:
                print(f"    bot_echo_loop error: {exc}")

        def parse_sse_events(raw_bytes):
            """Parse SSE events from raw response bytes."""
            events = []
            text = raw_bytes.decode("utf-8", errors="replace")
            for block in text.split("\n\n"):
                block = block.strip()
                if not block or block.startswith(":"):
                    continue
                event_type = "message"
                data = ""
                for line in block.split("\n"):
                    if line.startswith("event: "):
                        event_type = line[7:]
                    elif line.startswith("data: "):
                        data = line[6:]
                if data:
                    try:
                        events.append((event_type, json.loads(data)))
                    except json.JSONDecodeError:
                        events.append((event_type, data))
            return events

        try:
            # Start bot echo in background
            bot_task = asyncio.create_task(bot_echo_loop(echo_bot))

            # Small delay so bot is ready to receive
            await asyncio.sleep(0.3)

            # Send SSE chat request (sync, blocking read)
            loop = asyncio.get_event_loop()
            resp = await loop.run_in_executor(None, lambda: http_post_stream(
                f"{SERVER}/bridge/chat",
                data={"content": "Hi bot"},
                headers={"Authorization": f"Bearer {token7}"},
                timeout=10,
            ))
            raw_body = await loop.run_in_executor(None, resp.read)
            resp.close()
            await bot_task

            events = parse_sse_events(raw_body)
            event_types = [e[0] for e in events]

            # Verify: first event is session, contains sessionId
            has_session = (
                len(events) > 0
                and events[0][0] == "session"
                and "sessionId" in events[0][1]
            )
            report("SSE first event is session", has_session,
                   f"types={event_types[:5]}")

            # Verify: has chunk events
            chunk_events = [e for e in events if e[0] == "chunk"]
            report("SSE receives chunk events", len(chunk_events) >= 1,
                   f"chunk_count={len(chunk_events)}")

            # Verify: last meaningful event is done
            non_heartbeat = [e for e in events if e[0] != "heartbeat"]
            has_done = len(non_heartbeat) > 0 and non_heartbeat[-1][0] == "done"
            report("SSE ends with done event", has_done,
                   f"last_type={non_heartbeat[-1][0] if non_heartbeat else '?'}")

        except Exception as e:
            report("SSE first event is session", False, str(e)[:80])
            report("SSE receives chunk events", False, "skipped")
            report("SSE ends with done event", False, "skipped")
        finally:
            await echo_bot.close()

        # Test SSE auth rejection
        try:
            http_post_stream(
                f"{SERVER}/bridge/chat",
                data={"content": "hi"},
                headers={"Authorization": "Bearer sk-invalid"},
            )
            report("SSE rejects bad token", False, "should have failed")
        except urllib.error.HTTPError as e:
            report("SSE rejects bad token", e.code == 401, f"status={e.code}")
        except Exception as e:
            report("SSE rejects bad token", False, str(e)[:80])
    else:
        for name in ["SSE first event is session", "SSE receives chunk events",
                      "SSE ends with done event", "SSE rejects bad token"]:
            report(name, False, "skipped - no echo bot")

    _summarize()


def _summarize():
    print(f"\n{'='*50}")
    passed = sum(1 for r in results if r)
    total = len(results)
    color = "\033[92m" if passed == total else "\033[93m" if passed > total // 2 else "\033[91m"
    print(f"Results: {color}{passed}/{total} passed\033[0m")
    if passed < total:
        print(f"  {total - passed} test(s) failed")
    print()
    sys.exit(0 if passed == total else 1)


if __name__ == "__main__":
    asyncio.run(main())
