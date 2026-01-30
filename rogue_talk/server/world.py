"""Game world with room layout."""

import random
from dataclasses import dataclass, field

from .player import Player


@dataclass
class World:
    width: int
    height: int
    # Simple room: walls on edges, open in the middle

    def is_valid_position(self, x: int, y: int) -> bool:
        """Check if position is walkable (not a wall)."""
        # Walls on the border
        if x <= 0 or x >= self.width - 1:
            return False
        if y <= 0 or y >= self.height - 1:
            return False
        return True

    def get_spawn_position(self) -> tuple[int, int]:
        """Get a random valid spawn position."""
        x = random.randint(1, self.width - 2)
        y = random.randint(1, self.height - 2)
        return x, y

    def try_move(self, player: Player, dx: int, dy: int) -> bool:
        """Try to move player by delta. Returns True if successful."""
        new_x = player.x + dx
        new_y = player.y + dy
        if self.is_valid_position(new_x, new_y):
            player.x = new_x
            player.y = new_y
            return True
        return False
