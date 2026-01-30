"""Proximity-based audio routing."""

import math

from ..common.constants import AUDIO_FULL_VOLUME_DISTANCE, AUDIO_MAX_DISTANCE
from .player import Player


def calculate_distance(pos1: tuple[int, int], pos2: tuple[int, int]) -> float:
    """Calculate Euclidean distance between two positions."""
    dx = pos1[0] - pos2[0]
    dy = pos1[1] - pos2[1]
    return math.sqrt(dx * dx + dy * dy)


def calculate_volume(distance: float) -> float:
    """Calculate volume multiplier based on distance (0.0 to 1.0)."""
    if distance <= AUDIO_FULL_VOLUME_DISTANCE:
        return 1.0
    if distance >= AUDIO_MAX_DISTANCE:
        return 0.0
    # Linear falloff
    normalized = (distance - AUDIO_FULL_VOLUME_DISTANCE) / (
        AUDIO_MAX_DISTANCE - AUDIO_FULL_VOLUME_DISTANCE
    )
    return 1.0 - normalized


def get_audio_recipients(
    source: Player, players: dict[int, Player]
) -> list[tuple[Player, float]]:
    """
    Get list of (player, volume) tuples for players who should receive
    audio from the source player.
    """
    if source.is_muted:
        return []

    recipients = []
    source_pos = (source.x, source.y)

    for player_id, player in players.items():
        if player_id == source.id:
            continue

        distance = calculate_distance(source_pos, (player.x, player.y))
        volume = calculate_volume(distance)

        if volume > 0.0:
            recipients.append((player, volume))

    return recipients
