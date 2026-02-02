"""Tests for door/teleporter consistency between tiles and level.json.

In the codebase, both doors and teleporters use the same mechanism:
- Both tile types have `is_door: true` in tiles.json
- Both need entries in level.json's `doors` array
- Doors have `target_level` set to another level name
- Teleporters have `target_level` set to null (same level)
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from rogue_talk.server.game_server import GameServer
from rogue_talk.server.level import Level


class TestDoorConsistency:
    """Tests that door definitions are consistent with tile definitions."""

    @pytest.fixture
    def levels_dir(self) -> Path:
        """Get the levels directory path."""
        return Path(__file__).parent.parent.parent / "levels"

    @pytest.fixture
    def level_names(self, levels_dir: Path) -> list[str]:
        """Get all level pack names."""
        return [d.name for d in levels_dir.iterdir() if d.is_dir()]

    def test_door_tiles_match_level_json(
        self, levels_dir: Path, level_names: list[str]
    ) -> None:
        """Test that every door in level.json has a corresponding is_door tile.

        This catches the bug where level.json defines doors at positions
        that don't have door tiles, causing teleporters to show previews
        but not actually work.
        """
        errors = []

        for level_name in level_names:
            level_path = levels_dir / level_name
            level_txt = level_path / "level.txt"
            level_json_path = level_path / "level.json"
            tiles_json_path = level_path / "tiles.json"

            if not level_txt.exists():
                continue

            # Load level tiles
            lines = level_txt.read_text().rstrip("\n").split("\n")

            # Load tiles definition
            if tiles_json_path.exists():
                with open(tiles_json_path) as f:
                    tiles_data = json.load(f)
                tiles = tiles_data.get("tiles", {})
            else:
                tiles = {}

            # Load doors from level.json
            if level_json_path.exists():
                with open(level_json_path) as f:
                    level_data = json.load(f)
                doors = level_data.get("doors", [])
            else:
                doors = []

            # Check each door position
            for door in doors:
                x = door["x"]
                y = door["y"]

                # Get the tile character at this position
                if y < len(lines) and x < len(lines[y]):
                    tile_char = lines[y][x]
                else:
                    errors.append(
                        f"{level_name}: Door at ({x}, {y}) is outside level bounds"
                    )
                    continue

                # Check if tile has is_door property
                tile_def = tiles.get(tile_char, {})
                is_door = tile_def.get("is_door", False)

                if not is_door:
                    target = door.get("target_level", "same level")
                    errors.append(
                        f"{level_name}: Door at ({x}, {y}) -> {target} has tile "
                        f"'{tile_char}' which lacks is_door=true"
                    )

        if errors:
            pytest.fail(
                "Door/tile inconsistencies found:\n"
                + "\n".join(f"  - {e}" for e in errors)
            )

    def test_door_tiles_have_definitions(
        self, levels_dir: Path, level_names: list[str]
    ) -> None:
        """Test that every is_door tile has a matching entry in level.json.

        This catches orphaned door tiles that look like teleporters but
        have no destination defined.
        """
        errors = []

        for level_name in level_names:
            level_path = levels_dir / level_name
            level_txt = level_path / "level.txt"
            level_json_path = level_path / "level.json"
            tiles_json_path = level_path / "tiles.json"

            if not level_txt.exists():
                continue

            # Load level tiles
            lines = level_txt.read_text().rstrip("\n").split("\n")

            # Load tiles definition
            if tiles_json_path.exists():
                with open(tiles_json_path) as f:
                    tiles_data = json.load(f)
                tiles = tiles_data.get("tiles", {})
            else:
                tiles = {}

            # Get door tile characters
            door_chars = {
                char for char, tile in tiles.items() if tile.get("is_door", False)
            }

            # Load doors from level.json to get defined positions
            if level_json_path.exists():
                with open(level_json_path) as f:
                    level_data = json.load(f)
                doors = level_data.get("doors", [])
            else:
                doors = []

            defined_door_positions = {(d["x"], d["y"]) for d in doors}

            # Find all door tiles in the level
            for y, line in enumerate(lines):
                for x, char in enumerate(line):
                    if char in door_chars:
                        if (x, y) not in defined_door_positions:
                            errors.append(
                                f"{level_name}: Door tile '{char}' at ({x}, {y}) "
                                f"has no entry in level.json"
                            )

        if errors:
            pytest.fail(
                "Orphaned door tiles found:\n" + "\n".join(f"  - {e}" for e in errors)
            )

    def test_teleporter_targets_valid(
        self, levels_dir: Path, level_names: list[str]
    ) -> None:
        """Test that same-level teleporters have valid target positions.

        Teleporters (doors with target_level=null) should have targets that:
        - Are within level bounds
        - Are walkable tiles
        """
        errors = []

        for level_name in level_names:
            level_path = levels_dir / level_name
            level_txt = level_path / "level.txt"
            level_json_path = level_path / "level.json"
            tiles_json_path = level_path / "tiles.json"

            if not level_txt.exists():
                continue

            # Load level tiles
            lines = level_txt.read_text().rstrip("\n").split("\n")
            height = len(lines)
            width = max(len(line) for line in lines) if lines else 0

            # Load tiles definition
            if tiles_json_path.exists():
                with open(tiles_json_path) as f:
                    tiles_data = json.load(f)
                tiles = tiles_data.get("tiles", {})
            else:
                tiles = {}

            # Load doors from level.json
            if level_json_path.exists():
                with open(level_json_path) as f:
                    level_data = json.load(f)
                doors = level_data.get("doors", [])
            else:
                doors = []

            # Check same-level teleporters
            for door in doors:
                # Skip cross-level doors
                if door.get("target_level") is not None:
                    continue

                x, y = door["x"], door["y"]
                tx, ty = door["target_x"], door["target_y"]

                # Check bounds
                if tx < 0 or tx >= width or ty < 0 or ty >= height:
                    errors.append(
                        f"{level_name}: Teleporter at ({x}, {y}) has target "
                        f"({tx}, {ty}) outside level bounds ({width}x{height})"
                    )
                    continue

                # Check walkability
                if ty < len(lines) and tx < len(lines[ty]):
                    target_tile = lines[ty][tx]
                    tile_def = tiles.get(target_tile, {})
                    if not tile_def.get("walkable", False):
                        errors.append(
                            f"{level_name}: Teleporter at ({x}, {y}) has "
                            f"non-walkable target ({tx}, {ty}) tile '{target_tile}'"
                        )

        if errors:
            pytest.fail(
                "Invalid teleporter targets found:\n"
                + "\n".join(f"  - {e}" for e in errors)
            )
