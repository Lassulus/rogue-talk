"""Player state for the server."""

from __future__ import annotations

import time
from asyncio import StreamReader, StreamWriter
from dataclasses import dataclass, field


@dataclass
class Player:
    id: int
    name: str
    x: int
    y: int
    # TCP connection (stays open for entire session)
    reader: StreamReader | None = None
    writer: StreamWriter | None = None
    # LiveKit identity (same as player name)
    livekit_identity: str = ""
    # Set of LiveKit identities this player is currently subscribed to
    livekit_subscriptions: set[str] = field(default_factory=set)
    # State
    is_muted: bool = False
    current_level: str = "main"  # Name of the level the player is currently on
    public_key: bytes = b""  # Ed25519 public key for authentication
    last_pong_time: float = field(default_factory=time.monotonic)
    last_ping_sent_time: float = (
        0.0  # Time when last PING was sent (for RTT measurement)
    )
    last_move_time: float = 0.0  # Time of last movement (for speed limiting)
    ping_ms: int = 0  # RTT in milliseconds measured from PING/PONG
