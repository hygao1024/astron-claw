#!/usr/bin/env python3
"""End-to-end streaming test using the real plugin bridge token."""

import asyncio
import json
import time
import websockets

TOKEN = "sk-d7dd366ab3cb2e33484d073e66ae97b3f018687af8977fe4"
WS = "ws://localhost:8765"


async def main():
    print("[test] Connecting as chat client...")
    ws = await websockets.connect(f"{WS}/bridge/chat?token={TOKEN}")

    # Drain initial messages
    initial = []
    for _ in range(5):
        try:
            raw = await asyncio.wait_for(ws.recv(), timeout=2.0)
            msg = json.loads(raw)
            initial.append(msg.get("type", "?"))
        except asyncio.TimeoutError:
            break
    print(f"[test] Initial messages: {initial}")

    # Send test message
    await ws.send(json.dumps({
        "type": "message",
        "msgType": "text",
        "content": "Say hello in exactly 3 words",
    }))
    print("[test] Sent message, waiting for streaming response...")

    received = []
    t_start = time.monotonic()
    while True:
        try:
            raw = await asyncio.wait_for(ws.recv(), timeout=90.0)
            t = time.monotonic() - t_start
            msg = json.loads(raw)
            received.append({"time": t, "msg": msg})
            mtype = msg.get("type", "?")
            content_str = msg.get("content", "")
            content_len = len(content_str)
            preview = content_str[:80]
            print(f"  [{t:6.2f}s] type={mtype:15s} len={content_len:<5d} content={preview!r}")
            if mtype == "done":
                break
        except asyncio.TimeoutError:
            print("[test] Timeout waiting for response")
            break

    chunks = [r for r in received if r["msg"].get("type") == "chunk"]
    print(f"\n[result] Total chunks: {len(chunks)}")
    if len(chunks) >= 2:
        spread = chunks[-1]["time"] - chunks[0]["time"]
        total_chars = sum(len(r["msg"].get("content", "")) for r in chunks)
        avg_len = total_chars / len(chunks)
        print(f"[result] Time spread: {spread:.2f}s, avg chunk size: {avg_len:.0f} chars")
        if spread > 1.0:
            print("[result] PASS: True streaming confirmed!")
        else:
            print("[result] WARN: Chunks arrived too quickly, may be buffered")
    elif len(chunks) == 1:
        print("[result] FAIL: Only 1 chunk - not streaming")
    else:
        print("[result] FAIL: No chunks received")

    await ws.close()


if __name__ == "__main__":
    asyncio.run(main())
