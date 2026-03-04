#!/usr/bin/env python3
"""Astron Claw Bridge Server — production entry point.

Uses uvloop (high-performance event loop) + httptools (C-level HTTP parsing)
for maximum single-process throughput. Configuration is loaded from .env.

NOTE: workers must remain 1 for this application because WebSocket
connections and in-memory bot/chat registries are process-local.
"""

import uvicorn

from infra.log import setup_logging
from infra.config import load_config

config = load_config()
server = config.server

setup_logging(level=server.log_level.upper())

if __name__ == "__main__":
    uvicorn.run(
        "app:app",
        host=server.host,
        port=server.port,
        workers=server.workers,
        loop="uvloop",
        http="httptools",
        ws="websockets",
        log_config=None,
        log_level=server.log_level,
        access_log=server.access_log,
        timeout_keep_alive=30,
        timeout_graceful_shutdown=10,
    )
