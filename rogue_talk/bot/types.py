"""Type definitions for the Bot SDK."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path


class Direction(Enum):
    """Movement directions."""

    NORTH = (0, -1)
    SOUTH = (0, 1)
    EAST = (1, 0)
    WEST = (-1, 0)
    NORTHEAST = (1, -1)
    NORTHWEST = (-1, -1)
    SOUTHEAST = (1, 1)
    SOUTHWEST = (-1, 1)

    @property
    def dx(self) -> int:
        """X component of direction."""
        return self.value[0]

    @property
    def dy(self) -> int:
        """Y component of direction."""
        return self.value[1]


@dataclass
class BotConfig:
    """Configuration for a BotClient."""

    identity_dir: Path | None = None  # Custom identity location
    audio_enabled: bool = True
    auto_reconnect: bool = False


@dataclass
class PlayerState:
    """State of a player in the world."""

    player_id: int
    x: int
    y: int
    is_muted: bool
    name: str
    level: str


@dataclass
class WorldState:
    """Current state of the world visible to the bot."""

    players: list[PlayerState] = field(default_factory=list)

    def get_player(self, player_id: int) -> PlayerState | None:
        """Get a player by ID."""
        for p in self.players:
            if p.player_id == player_id:
                return p
        return None

    def get_player_by_name(self, name: str) -> PlayerState | None:
        """Get a player by name."""
        for p in self.players:
            if p.name == name:
                return p
        return None
