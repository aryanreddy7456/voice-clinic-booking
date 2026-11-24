"""Small event broadcaster: server -> dashboard over WebSocket.

The voice bot calls `publish(...)`; every open dashboard tab receives it.
"""

from __future__ import annotations

import asyncio
import json
import time
from typing import Any

_clients: set = set()


def register(ws) -> None:
    _clients.add(ws)


def unregister(ws) -> None:
    _clients.discard(ws)


async def broadcast(message: dict) -> None:
    if not _clients:
        return
    payload = json.dumps(message)
    dead = []
    for ws in list(_clients):
        try:
            await ws.send_text(payload)
        except Exception:
            dead.append(ws)
    for ws in dead:
        unregister(ws)


def publish(message: dict) -> None:
    """Fire-and-forget from pipeline code."""
    message.setdefault("ts", round(time.time(), 3))
    try:
        loop = asyncio.get_running_loop()
        loop.create_task(broadcast(message))
    except RuntimeError:
        pass


def publish_threadsafe(message: dict) -> None:
    publish(message)
