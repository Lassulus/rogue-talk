"""Viewport management for rendering levels larger than the screen."""

from dataclasses import dataclass


@dataclass
class Viewport:
    """Manages camera position for viewport rendering."""

    width: int
    height: int

    def calculate_camera(
        self, player_x: int, player_y: int, level_width: int, level_height: int
    ) -> tuple[int, int]:
        """
        Calculate camera position to center on player.

        Returns (cam_x, cam_y) - the top-left corner of the viewport in level coordinates.
        """
        # Target: center player in viewport
        cam_x = player_x - self.width // 2
        cam_y = player_y - self.height // 2

        # Handle levels smaller than viewport - center the level
        if level_width <= self.width:
            cam_x = -(self.width - level_width) // 2
        else:
            # Clamp to level bounds
            cam_x = max(0, min(cam_x, level_width - self.width))

        if level_height <= self.height:
            cam_y = -(self.height - level_height) // 2
        else:
            # Clamp to level bounds
            cam_y = max(0, min(cam_y, level_height - self.height))

        return cam_x, cam_y
