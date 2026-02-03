"""Shared proximity-based audio volume calculation."""

import math

from .constants import AUDIO_FULL_VOLUME_DISTANCE, AUDIO_MAX_DISTANCE

# Pre-computed squared thresholds
_MAX_DISTANCE_SQ = int(AUDIO_MAX_DISTANCE * AUDIO_MAX_DISTANCE)  # 100
_FULL_VOLUME_DISTANCE_SQ = AUDIO_FULL_VOLUME_DISTANCE * AUDIO_FULL_VOLUME_DISTANCE

# Lookup table: _VOLUME_TABLE[squared_distance] -> volume
# Precomputed at module load, no sqrt needed at runtime
_VOLUME_TABLE: tuple[float, ...] = tuple(
    1.0
    if dist_sq <= _FULL_VOLUME_DISTANCE_SQ
    else 1.0
    - (math.sqrt(dist_sq) - AUDIO_FULL_VOLUME_DISTANCE)
    / (AUDIO_MAX_DISTANCE - AUDIO_FULL_VOLUME_DISTANCE)
    for dist_sq in range(_MAX_DISTANCE_SQ + 1)
)


def get_volume(dx: int, dy: int) -> float:
    """Get volume for a position offset. Uses lookup table, no sqrt at runtime."""
    dist_sq = dx * dx + dy * dy
    if dist_sq > _MAX_DISTANCE_SQ:
        return 0.0
    return _VOLUME_TABLE[dist_sq]
