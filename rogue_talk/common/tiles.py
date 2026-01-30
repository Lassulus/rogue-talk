"""Tile definitions with visual and gameplay properties."""

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from blessed import Terminal


@dataclass
class TileDef:
    """Definition for a tile type."""

    char: str
    walkable: bool
    color: str  # blessed color name like "green", "blue", "white"
    name: str = ""
    walking_sound: str | None = None
    nearby_sound: str | None = None
    # For animated tiles: list of colors to cycle through
    animation_colors: list[str] = field(default_factory=list)
    blocks_sight: bool | None = None  # None means use !walkable as default
    blocks_sound: bool | None = None  # None means use !walkable as default

    def __post_init__(self) -> None:
        """Set default values for blocks_sight and blocks_sound based on walkable."""
        if self.blocks_sight is None:
            self.blocks_sight = not self.walkable
        if self.blocks_sound is None:
            self.blocks_sound = not self.walkable


def _load_tiles_from_json() -> tuple[dict[str, TileDef], TileDef]:
    """Load tile definitions from JSON file."""
    json_path = Path(__file__).parent / "tiles.json"

    with open(json_path) as f:
        data = json.load(f)

    tiles: dict[str, TileDef] = {}

    for char, tile_data in data["tiles"].items():
        tiles[char] = TileDef(
            char=char,
            walkable=tile_data["walkable"],
            color=tile_data["color"],
            name=tile_data.get("name", ""),
            walking_sound=tile_data.get("walking_sound"),
            nearby_sound=tile_data.get("nearby_sound"),
            animation_colors=tile_data.get("animation_colors") or [],
            blocks_sight=tile_data.get("blocks_sight"),
            blocks_sound=tile_data.get("blocks_sound"),
        )

    default_data = data["default"]
    default_tile = TileDef(
        char=default_data["symbol"],
        walkable=default_data["walkable"],
        color=default_data["color"],
    )

    return tiles, default_tile


# Load tiles from JSON
TILES, DEFAULT_TILE = _load_tiles_from_json()


def get_tile(char: str) -> TileDef:
    """Get the tile definition for a character."""
    return TILES.get(char, DEFAULT_TILE)


def is_walkable(char: str) -> bool:
    """Check if a tile character is walkable."""
    return get_tile(char).walkable


def render_tile(char: str, term: "Terminal", anim_frame: int = 0) -> str:
    """Render a tile with its color using blessed Terminal."""
    tile = get_tile(char)

    # Determine color - use animation if available
    if tile.animation_colors:
        color_name = tile.animation_colors[anim_frame % len(tile.animation_colors)]
    else:
        color_name = tile.color

    # Get the color function from terminal
    color_fn = getattr(term, color_name, None)

    if color_fn:
        return str(color_fn(tile.char))
    return tile.char
