#!/usr/bin/env python3
"""
End-to-end integration test for Astron Claw.
Tests: token API, bot WS, chat WS, message flow, streaming, media upload/download.
"""

import asyncio
import io
import json
import sys
import urllib.request
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

    # ── 2. Test WebSocket auth rejection ──
    print("\n2. WebSocket Auth")
    try:
        async with websockets.connect(f"{WS_SERVER}/bridge/chat?token=sk-bad") as ws_test:
            try:
                msg = await asyncio.wait_for(ws_test.recv(), timeout=3)
                report("WS /bridge/chat bad token rejects", False, f"got message: {msg[:60]}")
            except websockets.exceptions.ConnectionClosed as e:
                report("WS /bridge/chat bad token rejects", e.code == 4001, f"code={e.code}")
    except Exception as e:
        report("WS /bridge/chat bad token rejects", "4001" in str(e) or "403" in str(e), str(e)[:60])

    try:
        async with websockets.connect(f"{WS_SERVER}/bridge/bot?token=sk-bad") as ws_test:
            try:
                msg = await asyncio.wait_for(ws_test.recv(), timeout=3)
                report("WS /bridge/bot bad token rejects", False, f"got message: {msg[:60]}")
            except websockets.exceptions.ConnectionClosed as e:
                report("WS /bridge/bot bad token rejects", e.code == 4001, f"code={e.code}")
    except Exception as e:
        report("WS /bridge/bot bad token rejects", "4001" in str(e) or "403" in str(e), str(e)[:60])

    # ── 3. Test Chat WS connects, receives bot status ──
    print("\n3. Chat WebSocket Connection")
    try:
        async with websockets.connect(f"{WS_SERVER}/bridge/chat?token={token}") as chat_ws:
            raw = await asyncio.wait_for(chat_ws.recv(), timeout=5)
            status_msg = json.loads(raw)
            report(
                "Chat WS connects + receives status",
                status_msg.get("type") == "bot_status" and status_msg.get("connected") is False,
                f"got: {json.dumps(status_msg)}"
            )

            # Send message without bot connected - should get error
            await chat_ws.send(json.dumps({"type": "message", "content": "hello"}))
            raw = await asyncio.wait_for(chat_ws.recv(), timeout=5)
            err_msg = json.loads(raw)
            report(
                "Chat msg without bot -> error",
                err_msg.get("type") == "error",
                f"got: {json.dumps(err_msg)}"
            )
    except Exception as e:
        report("Chat WS connects + receives status", False, str(e)[:80])
        report("Chat msg without bot -> error", False, "skipped")

    # ── 4. Test Bot WS connects ──
    print("\n4. Bot WebSocket Connection")
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

    # ── 5. Chat connects and sees bot online ──
    print("\n5. Chat + Bot Interaction")
    try:
        chat_ws = await websockets.connect(f"{WS_SERVER}/bridge/chat?token={token}")
        raw = await asyncio.wait_for(chat_ws.recv(), timeout=5)
        status_msg = json.loads(raw)
        report(
            "Chat sees bot_connected=true",
            status_msg.get("type") == "bot_status" and status_msg.get("connected") is True,
            f"got: {json.dumps(status_msg)}"
        )
    except Exception as e:
        report("Chat sees bot_connected=true", False, str(e)[:80])
        await bot_ws.close()
        _summarize()
        return

    # ── 6. Full message flow: chat -> server -> bot -> server -> chat ──
    print("\n6. Message Flow (Chat -> Bot -> Chat)")

    # Chat sends a message
    await chat_ws.send(json.dumps({"type": "message", "content": "What is 2+2?"}))

    # Bot should receive a JSON-RPC session/prompt request
    try:
        raw = await asyncio.wait_for(bot_ws.recv(), timeout=5)
        rpc_req = json.loads(raw)
        report(
            "Bot receives session/prompt",
            rpc_req.get("method") == "session/prompt"
            and rpc_req.get("jsonrpc") == "2.0"
            and rpc_req.get("id") is not None,
            f"method={rpc_req.get('method')}, id={rpc_req.get('id')}"
        )

        # Verify prompt content structure
        prompt_params = rpc_req.get("params", {})
        prompt_content = prompt_params.get("prompt", {}).get("content", [])
        has_text = any(
            c.get("type") == "text" and "2+2" in c.get("text", "")
            for c in prompt_content
        ) if prompt_content else False
        report(
            "Prompt contains user text",
            has_text,
            f"content={json.dumps(prompt_content)[:80]}"
        )

        rpc_id = rpc_req.get("id")
        session_id = prompt_params.get("sessionId", "")

    except Exception as e:
        report("Bot receives session/prompt", False, str(e)[:80])
        report("Prompt contains user text", False, "skipped")
        await bot_ws.close()
        await chat_ws.close()
        _summarize()
        return

    # ── 7. Bot sends streaming response (simulate plugin behavior) ──
    print("\n7. Streaming Response (Bot -> Chat)")

    # Send a thinking chunk
    thinking_update = {
        "jsonrpc": "2.0",
        "method": "session/update",
        "params": {
            "sessionId": session_id,
            "update": {
                "sessionUpdate": "agent_thought_chunk",
                "content": {"type": "text", "text": "Let me calculate 2+2..."}
            }
        }
    }
    await bot_ws.send(json.dumps(thinking_update))

    try:
        raw = await asyncio.wait_for(chat_ws.recv(), timeout=5)
        think_msg = json.loads(raw)
        report(
            "Chat receives thinking",
            think_msg.get("type") == "thinking" and "calculate" in think_msg.get("content", ""),
            f"type={think_msg.get('type')}, content={think_msg.get('content', '')[:40]}"
        )
    except Exception as e:
        report("Chat receives thinking", False, str(e)[:80])

    # Send text chunks (streaming)
    chunks = ["The answer", " is ", "4."]
    for chunk in chunks:
        chunk_update = {
            "jsonrpc": "2.0",
            "method": "session/update",
            "params": {
                "sessionId": session_id,
                "update": {
                    "sessionUpdate": "agent_message_chunk",
                    "content": {"type": "text", "text": chunk}
                }
            }
        }
        await bot_ws.send(json.dumps(chunk_update))

    received_chunks = []
    try:
        for _ in range(len(chunks)):
            raw = await asyncio.wait_for(chat_ws.recv(), timeout=5)
            msg = json.loads(raw)
            if msg.get("type") == "chunk":
                received_chunks.append(msg.get("content", ""))
        full_text = "".join(received_chunks)
        report(
            "Chat receives all chunks",
            full_text == "The answer is 4.",
            f"assembled='{full_text}'"
        )
    except Exception as e:
        report("Chat receives all chunks", False, str(e)[:80])

    # Send a tool_call
    tool_update = {
        "jsonrpc": "2.0",
        "method": "session/update",
        "params": {
            "sessionId": session_id,
            "update": {
                "sessionUpdate": "tool_call",
                "toolCallId": "tc_123",
                "title": "calculator",
                "status": "in_progress",
                "content": [{"type": "content", "content": {"type": "text", "text": "{\"expr\": \"2+2\"}"}}]
            }
        }
    }
    await bot_ws.send(json.dumps(tool_update))

    try:
        raw = await asyncio.wait_for(chat_ws.recv(), timeout=5)
        tool_msg = json.loads(raw)
        report(
            "Chat receives tool_call",
            tool_msg.get("type") == "tool_call" and tool_msg.get("name") == "calculator",
            f"type={tool_msg.get('type')}, name={tool_msg.get('name')}"
        )
    except Exception as e:
        report("Chat receives tool_call", False, str(e)[:80])

    # Send tool_call_update (result)
    tool_result_update = {
        "jsonrpc": "2.0",
        "method": "session/update",
        "params": {
            "sessionId": session_id,
            "update": {
                "sessionUpdate": "tool_call_update",
                "toolCallId": "tc_123",
                "title": "calculator",
                "status": "completed",
                "content": [{"type": "content", "content": {"type": "text", "text": "4"}}]
            }
        }
    }
    await bot_ws.send(json.dumps(tool_result_update))

    try:
        raw = await asyncio.wait_for(chat_ws.recv(), timeout=5)
        result_msg = json.loads(raw)
        report(
            "Chat receives tool_result",
            result_msg.get("type") == "tool_result" and result_msg.get("content") == "4",
            f"type={result_msg.get('type')}, content={result_msg.get('content')}"
        )
    except Exception as e:
        report("Chat receives tool_result", False, str(e)[:80])

    # Send completion (JSON-RPC result with stopReason)
    completion = {
        "jsonrpc": "2.0",
        "id": rpc_id,
        "result": {
            "stopReason": "end_turn",
            "_meta": {"requestId": rpc_id, "sessionId": session_id}
        }
    }
    await bot_ws.send(json.dumps(completion))

    try:
        raw = await asyncio.wait_for(chat_ws.recv(), timeout=5)
        done_msg = json.loads(raw)
        report(
            "Chat receives done",
            done_msg.get("type") == "done",
            f"got: {json.dumps(done_msg)}"
        )
    except Exception as e:
        report("Chat receives done", False, str(e)[:80])

    # ── 8. Bot disconnect notification ──
    print("\n8. Bot Disconnect Notification")
    await bot_ws.close()
    try:
        raw = await asyncio.wait_for(chat_ws.recv(), timeout=5)
        disconnect_msg = json.loads(raw)
        report(
            "Chat notified of bot disconnect",
            disconnect_msg.get("type") == "bot_status" and disconnect_msg.get("connected") is False,
            f"got: {json.dumps(disconnect_msg)}"
        )
    except Exception as e:
        report("Chat notified of bot disconnect", False, str(e)[:80])

    await chat_ws.close()

    # ── 9. Duplicate bot prevention ──
    print("\n9. Duplicate Bot Prevention")
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

    # ── 10. Media Upload API ──
    print("\n10. Media Upload API")

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

    # ── 11. Media Download API ──
    print("\n11. Media Download API")

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

    # ── 12. Media Message Flow ──
    print("\n12. Media Message Flow (Chat -> Bot with media)")

    # Create a token and connect bot + chat
    token4 = http_post(f"{SERVER}/api/token")["token"]
    media_bot_ws = await websockets.connect(f"{WS_SERVER}/bridge/bot?token={token4}")
    media_chat_ws = await websockets.connect(f"{WS_SERVER}/bridge/chat?token={token4}")

    # Consume initial bot_status
    raw = await asyncio.wait_for(media_chat_ws.recv(), timeout=5)

    # Upload a file first
    try:
        test_file_data = b"Hello, this is a test document content."
        upload_result = multipart_upload(
            f"{SERVER}/api/media/upload",
            "test_doc.txt",
            test_file_data,
            "text/plain",
            token4,
        )
        file_media_id = upload_result.get("mediaId", "")

        # Chat sends a file message
        await media_chat_ws.send(json.dumps({
            "type": "message",
            "msgType": "file",
            "content": "Here is a document",
            "media": {
                "mediaId": file_media_id,
                "fileName": "test_doc.txt",
                "mimeType": "text/plain",
                "fileSize": len(test_file_data),
                "downloadUrl": upload_result.get("downloadUrl", ""),
            },
        }))

        # Bot should receive a JSON-RPC prompt with media content
        raw = await asyncio.wait_for(media_bot_ws.recv(), timeout=5)
        rpc_req = json.loads(raw)
        prompt_content = rpc_req.get("params", {}).get("prompt", {}).get("content", [])

        has_text = any(c.get("type") == "text" for c in prompt_content)
        has_media = any(c.get("type") == "media" for c in prompt_content)
        report(
            "Bot receives media message",
            rpc_req.get("method") == "session/prompt" and has_text and has_media,
            f"has_text={has_text}, has_media={has_media}"
        )

        # Verify media reference in the prompt
        media_items = [c for c in prompt_content if c.get("type") == "media"]
        if media_items:
            media_ref = media_items[0].get("media", {})
            report(
                "Media reference has correct fields",
                media_ref.get("mediaId") == file_media_id
                and media_ref.get("fileName") == "test_doc.txt",
                f"mediaId={media_ref.get('mediaId', '')[:20]}..."
            )
        else:
            report("Media reference has correct fields", False, "no media items in prompt")

    except Exception as e:
        report("Bot receives media message", False, str(e)[:80])
        report("Media reference has correct fields", False, "skipped")

    # ── 13. Bot sends media to chat ──
    print("\n13. Bot Media Message (Bot -> Chat)")

    try:
        # Upload a file as the bot
        bot_file_data = b"Bot generated content for testing purposes."
        bot_upload = multipart_upload(
            f"{SERVER}/api/media/upload",
            "bot_result.txt",
            bot_file_data,
            "text/plain",
            token4,
        )
        bot_media_id = bot_upload.get("mediaId", "")

        # Bot sends agent_media update
        media_update = {
            "jsonrpc": "2.0",
            "method": "session/update",
            "params": {
                "sessionId": "test-session",
                "update": {
                    "sessionUpdate": "agent_media",
                    "content": {
                        "msgType": "file",
                        "text": "Here is the result file",
                        "media": {
                            "mediaId": bot_media_id,
                            "fileName": "bot_result.txt",
                            "mimeType": "text/plain",
                            "fileSize": len(bot_file_data),
                        },
                    },
                },
            },
        }
        await media_bot_ws.send(json.dumps(media_update))

        raw = await asyncio.wait_for(media_chat_ws.recv(), timeout=5)
        media_msg = json.loads(raw)
        report(
            "Chat receives bot media message",
            media_msg.get("type") == "message"
            and media_msg.get("msgType") == "file"
            and media_msg.get("media", {}).get("mediaId") == bot_media_id,
            f"type={media_msg.get('type')}, msgType={media_msg.get('msgType')}"
        )

        # Verify download URL in forwarded message
        download_url = media_msg.get("media", {}).get("downloadUrl", "")
        report(
            "Media message includes downloadUrl",
            bot_media_id in download_url,
            f"downloadUrl={download_url[:60]}"
        )

    except Exception as e:
        report("Chat receives bot media message", False, str(e)[:80])
        report("Media message includes downloadUrl", False, "skipped")

    await media_bot_ws.close()
    await media_chat_ws.close()

    # ── 14. Chat sends media message without media info -> error ──
    print("\n14. Media Validation")

    token5 = http_post(f"{SERVER}/api/token")["token"]
    val_bot_ws = await websockets.connect(f"{WS_SERVER}/bridge/bot?token={token5}")
    val_chat_ws = await websockets.connect(f"{WS_SERVER}/bridge/chat?token={token5}")
    raw = await asyncio.wait_for(val_chat_ws.recv(), timeout=5)  # consume bot_status

    try:
        # Send image message without media object
        await val_chat_ws.send(json.dumps({
            "type": "message",
            "msgType": "image",
            "content": "",
        }))

        raw = await asyncio.wait_for(val_chat_ws.recv(), timeout=5)
        err_msg = json.loads(raw)
        report(
            "Image msg without media -> error",
            err_msg.get("type") == "error",
            f"got: {json.dumps(err_msg)[:60]}"
        )
    except Exception as e:
        report("Image msg without media -> error", False, str(e)[:80])

    try:
        # Send empty text message -> error
        await val_chat_ws.send(json.dumps({
            "type": "message",
            "msgType": "text",
            "content": "",
        }))

        raw = await asyncio.wait_for(val_chat_ws.recv(), timeout=5)
        err_msg = json.loads(raw)
        report(
            "Empty text msg -> error",
            err_msg.get("type") == "error",
            f"got: {json.dumps(err_msg)[:60]}"
        )
    except Exception as e:
        report("Empty text msg -> error", False, str(e)[:80])

    await val_bot_ws.close()
    await val_chat_ws.close()

    # ── 15. Upload file type validation ──
    print("\n15. Upload Validation")

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
