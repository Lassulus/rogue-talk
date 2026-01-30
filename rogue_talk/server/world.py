"""Game world with level-based layout."""

from dataclasses import dataclass

from .level import Level
from .player import Player


@dataclass
class World:
    level: Level

    @property
    def width(self) -> int:
        return self.level.width

    @property
    def height(self) -> int:
        return self.level.height

    def is_valid_position(self, x: int, y: int) -> bool:
        """Check if position is walkable."""
        return self.level.is_walkable(x, y)

    def get_spawn_position(self) -> tuple[int, int]:
        """Get a valid spawn position."""
        return self.level.get_spawn_position()

    def try_move(self, player: Player, dx: int, dy: int) -> bool:
        """Try to move player by delta. Returns True if successful."""
        new_x = player.x + dx
        new_y = player.y + dy
        if self.is_valid_position(new_x, new_y):
            player.x = new_x
            player.y = new_y
            return True
        return False
