"""Proximity-based audio routing."""

from ..common.audio import _MAX_DISTANCE_SQ as _MAX_DISTANCE_SQ
from ..common.audio import _VOLUME_TABLE as _VOLUME_TABLE
from ..common.audio import get_volume as get_volume
from .player import Player


# Cache for recipient lists: source_id -> (source_pos, [(player, volume), ...])
_recipient_cache: dict[int, tuple[tuple[int, int], list[tuple[Player, float]]]] = {}


def get_audio_recipients(
    source: Player, players: dict[int, Player]
) -> list[tuple[Player, float]]:
    """
    Get list of (player, volume) tuples for players who should receive
    audio from the source player. Results are cached until positions change.
    """
    if source.is_muted:
        return []

    source_x, source_y = source.x, source.y

    # Check cache - valid if source hasn't moved
    cached = _recipient_cache.get(source.id)
    if cached is not None:
        cached_pos, cached_recipients = cached
        if cached_pos == (source_x, source_y):
            # Verify recipients haven't moved AND no new players in range
            still_valid = True
            cached_ids = {p.id for p, _ in cached_recipients}

            # Check if any cached recipient moved or left
            for player, old_volume in cached_recipients:
                if player.id not in players:
                    still_valid = False
                    break
                new_volume = get_volume(player.x - source_x, player.y - source_y)
                if abs(new_volume - old_volume) > 0.01:
                    still_valid = False
                    break

            # Check if any NEW player has entered range
            if still_valid:
                for player_id, player in players.items():
                    if player_id == source.id or player_id in cached_ids:
                        continue
                    volume = get_volume(player.x - source_x, player.y - source_y)
                    if volume > 0.0:
                        # New player in range - cache invalid
                        still_valid = False
                        break

            if still_valid:
                return cached_recipients

    # Rebuild recipient list
    recipients = []
    for player_id, player in players.items():
        if player_id == source.id:
            continue

        volume = get_volume(player.x - source_x, player.y - source_y)
        if volume > 0.0:
            recipients.append((player, volume))

    _recipient_cache[source.id] = ((source_x, source_y), recipients)
    return recipients


def clear_recipient_cache(player_id: int | None = None) -> None:
    """Clear cached recipients for a player, or all if player_id is None."""
    if player_id is None:
        _recipient_cache.clear()
    else:
        _recipient_cache.pop(player_id, None)
