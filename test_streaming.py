#!/usr/bin/env python3
"""
Streaming test for Astron Claw bridge.

Simulates a Bot that replies with multiple small chunks (like a real LLM streaming),
and verifies the Chat client receives each chunk individually in real-time,
rather than one big blob at the end.

Usage: python3 test_streaming.py
"""

import asyncio
import json
import time
import urllib.request
import websockets

SERVER = "http://localhost:8765"
WS_SERVER = "ws://localhost:8765"

# ── Helpers ──────────────────────────────────────────────────────────────────

def http_post(url, data=None):
    body = json.dumps(data).encode() if data else b""
    req = urllib.request.Request(
        url, data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req) as resp:
        return json.loads(resp.read().decode())


def create_token():
    result = http_post(f"{SERVER}/api/token")
    return result["token"]


# ── Test: Bot sends multiple chunks, Chat receives them individually ─────────

async def test_streaming_chunks():
    """
    1. Create token
    2. Connect Bot WS and Chat WS
    3. Chat sends a message
    4. Bot receives JSON-RPC prompt
    5. Bot replies with 5 individual 'agent_message_chunk' notifications (simulating streaming)
    6. Bot sends a 'done' signal
    7. Verify Chat receives each chunk separately with measurable time gaps
    """
    token = create_token()
    print(f"[setup] Token: {token[:20]}...")

    # Connect Bot
    bot_ws = await websockets.connect(f"{WS_SERVER}/bridge/bot?token={token}")
    print("[setup] Bot connected")

    # Connect Chat
    chat_ws = await websockets.connect(f"{WS_SERVER}/bridge/chat?token={token}")
    print("[setup] Chat connected")

    # Drain initial messages from chat (bot_status, session_info)
    initial_msgs = []
    for _ in range(5):
        try:
            raw = await asyncio.wait_for(chat_ws.recv(), timeout=1.0)
            msg = json.loads(raw)
            initial_msgs.append(msg["type"])
        except asyncio.TimeoutError:
            break
    print(f"[setup] Chat initial messages: {initial_msgs}")

    # Chat sends a message
    chat_ws_msg = {
        "type": "message",
        "msgType": "text",
        "content": "Hello, tell me a story",
    }
    await chat_ws.send(json.dumps(chat_ws_msg))
    print("[chat] Sent: 'Hello, tell me a story'")

    # Bot receives JSON-RPC prompt
    bot_raw = await asyncio.wait_for(bot_ws.recv(), timeout=5.0)
    bot_msg = json.loads(bot_raw)
    request_id = bot_msg.get("id", "")
    print(f"[bot] Received JSON-RPC prompt (id={request_id})")

    # Simulate streaming: Bot sends 5 chunks with small delays
    chunks = [
        "Once upon ",
        "a time, ",
        "there was ",
        "a brave ",
        "knight.",
    ]

    print(f"\n[bot] Sending {len(chunks)} chunks with 300ms delay between each...")
    for i, chunk_text in enumerate(chunks):
        notification = {
            "jsonrpc": "2.0",
            "method": "session/update",
            "params": {
                "update": {
                    "sessionUpdate": "agent_message_chunk",
                    "content": {"type": "text", "text": chunk_text},
                },
            },
        }
        await bot_ws.send(json.dumps(notification))
        print(f"  [bot] Sent chunk {i+1}: {chunk_text!r}")
        await asyncio.sleep(0.3)

    # Bot sends completion
    completion = {
        "jsonrpc": "2.0",
        "id": request_id,
        "result": {"stopReason": "end_turn"},
    }
    await bot_ws.send(json.dumps(completion))
    print("[bot] Sent completion (end_turn)")

    # Collect all messages from Chat side with timestamps
    print(f"\n[chat] Collecting received messages...")
    received = []
    t_start = time.monotonic()
    while True:
        try:
            raw = await asyncio.wait_for(chat_ws.recv(), timeout=3.0)
            t_elapsed = time.monotonic() - t_start
            msg = json.loads(raw)
            received.append({"time": t_elapsed, "msg": msg})
        except asyncio.TimeoutError:
            break

    # Analyze results
    print(f"\n{'='*60}")
    print("RESULTS")
    print(f"{'='*60}")

    chunk_events = [r for r in received if r["msg"].get("type") == "chunk"]
    done_events = [r for r in received if r["msg"].get("type") == "done"]
    other_events = [r for r in received if r["msg"].get("type") not in ("chunk", "done")]

    if other_events:
        print(f"\nOther events: {[r['msg']['type'] for r in other_events]}")

    print(f"\nChunk events received: {len(chunk_events)}")
    for i, r in enumerate(chunk_events):
        content = r["msg"].get("content", "")
        print(f"  [{r['time']:.3f}s] chunk {i+1}: {content!r}")

    print(f"\nDone events received: {len(done_events)}")
    for r in done_events:
        print(f"  [{r['time']:.3f}s] done")

    # Verdict
    print(f"\n{'='*60}")
    if len(chunk_events) >= len(chunks):
        # Check if chunks arrived with time gaps (streaming) vs all at once (buffered)
        if len(chunk_events) >= 2:
            first_time = chunk_events[0]["time"]
            last_time = chunk_events[-1]["time"]
            spread = last_time - first_time

            if spread > 0.5:
                print(f"PASS: Streaming works! {len(chunk_events)} chunks over {spread:.2f}s")
                print(f"  Chunks arrived with time spread, confirming real-time streaming.")
            else:
                print(f"WARN: Got {len(chunk_events)} chunks but time spread is only {spread:.3f}s")
                print(f"  Chunks may have been buffered and delivered all at once.")
        else:
            print(f"WARN: Only {len(chunk_events)} chunk(s) received")
    elif len(chunk_events) == 1:
        content = chunk_events[0]["msg"].get("content", "")
        expected_full = "".join(chunks)
        if content == expected_full:
            print(f"FAIL: Only 1 chunk received with full text — NOT streaming")
            print(f"  All chunks were merged into a single delivery.")
        else:
            print(f"FAIL: Only 1 chunk received: {content!r}")
    else:
        print(f"FAIL: No chunk events received!")
        print(f"  All received messages: {[r['msg'] for r in received]}")

    print(f"{'='*60}\n")

    await bot_ws.close()
    await chat_ws.close()


# ── Test: Verify bridge transparently forwards chunks from real plugin ───────

async def test_bridge_passthrough():
    """
    Quick test to verify the bridge server correctly passes through
    different sessionUpdate types (chunk, tool_result, final).
    """
    token = create_token()
    print(f"\n[passthrough] Token: {token[:20]}...")

    bot_ws = await websockets.connect(f"{WS_SERVER}/bridge/bot?token={token}")
    chat_ws = await websockets.connect(f"{WS_SERVER}/bridge/chat?token={token}")

    # Drain initial messages
    for _ in range(5):
        try:
            await asyncio.wait_for(chat_ws.recv(), timeout=1.0)
        except asyncio.TimeoutError:
            break

    # Chat sends message to trigger the flow
    await chat_ws.send(json.dumps({"type": "message", "msgType": "text", "content": "test"}))

    # Bot receives prompt
    bot_raw = await asyncio.wait_for(bot_ws.recv(), timeout=5.0)
    request_id = json.loads(bot_raw).get("id", "")

    # Bot sends different event types
    events_to_send = [
        ("agent_thought_chunk", "Let me think about this..."),
        ("agent_message_chunk", "Here is "),
        ("agent_message_chunk", "my answer."),
        ("tool_result", "Tool output: success"),
        ("agent_message_final", "Here is my answer."),
    ]

    for update_type, text in events_to_send:
        await bot_ws.send(json.dumps({
            "jsonrpc": "2.0",
            "method": "session/update",
            "params": {
                "update": {
                    "sessionUpdate": update_type,
                    "content": {"type": "text", "text": text},
                },
            },
        }))
        await asyncio.sleep(0.1)

    # Send completion
    await bot_ws.send(json.dumps({
        "jsonrpc": "2.0",
        "id": request_id,
        "result": {"stopReason": "end_turn"},
    }))

    # Collect Chat messages
    received = []
    while True:
        try:
            raw = await asyncio.wait_for(chat_ws.recv(), timeout=2.0)
            received.append(json.loads(raw))
        except asyncio.TimeoutError:
            break

    print(f"[passthrough] Events received by Chat:")
    for msg in received:
        msg_type = msg.get("type", "?")
        content = msg.get("content", msg.get("name", ""))
        print(f"  type={msg_type:15s} content={str(content)[:60]}")

    # Check mapping
    expected_types = ["thinking", "chunk", "chunk", "tool_result", "done", "done"]
    actual_types = [m.get("type") for m in received]
    if actual_types == expected_types:
        print("[passthrough] PASS: All event types correctly mapped!")
    else:
        print(f"[passthrough] MISMATCH:")
        print(f"  expected: {expected_types}")
        print(f"  actual:   {actual_types}")

    await bot_ws.close()
    await chat_ws.close()


async def main():
    print("=" * 60)
    print("Astron Claw Streaming Test")
    print("=" * 60)

    print("\n--- Test 1: Streaming Chunks ---")
    await test_streaming_chunks()

    print("\n--- Test 2: Bridge Event Passthrough ---")
    await test_bridge_passthrough()


if __name__ == "__main__":
    asyncio.run(main())
