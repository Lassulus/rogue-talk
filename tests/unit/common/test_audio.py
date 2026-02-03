"""Tests for shared proximity audio volume calculation."""

from __future__ import annotations

from rogue_talk.common.audio import (
    _MAX_DISTANCE_SQ,
    _VOLUME_TABLE,
    get_volume,
)
from rogue_talk.common.constants import AUDIO_FULL_VOLUME_DISTANCE, AUDIO_MAX_DISTANCE


class TestGetVolume:
    """Tests for get_volume function in the shared module."""

    def test_same_position(self) -> None:
        assert get_volume(0, 0) == 1.0

    def test_within_full_volume_distance(self) -> None:
        assert get_volume(2, 0) == 1.0
        assert get_volume(0, 2) == 1.0
        assert get_volume(1, 1) == 1.0  # sqrt(2) ≈ 1.41

    def test_beyond_max_distance(self) -> None:
        assert get_volume(11, 0) == 0.0
        assert get_volume(0, 11) == 0.0
        assert get_volume(8, 8) == 0.0  # sqrt(128) ≈ 11.3

    def test_at_max_distance(self) -> None:
        vol = get_volume(10, 0)
        assert vol >= 0.0
        assert vol < 0.01

    def test_linear_falloff(self) -> None:
        vol_near = get_volume(3, 0)
        vol_mid = get_volume(5, 0)
        vol_far = get_volume(8, 0)
        assert vol_near > vol_mid > vol_far
        assert vol_near > 0.0
        assert vol_far > 0.0

    def test_symmetric(self) -> None:
        assert get_volume(5, 0) == get_volume(0, 5)
        assert get_volume(3, 4) == get_volume(4, 3)
        assert get_volume(-5, 0) == get_volume(5, 0)


class TestVolumeTable:
    """Tests for the pre-computed volume table."""

    def test_table_length(self) -> None:
        assert len(_VOLUME_TABLE) == _MAX_DISTANCE_SQ + 1

    def test_table_starts_at_one(self) -> None:
        assert _VOLUME_TABLE[0] == 1.0

    def test_table_monotonic_decrease(self) -> None:
        full_vol_sq = int(AUDIO_FULL_VOLUME_DISTANCE**2)
        for i in range(full_vol_sq + 1, len(_VOLUME_TABLE)):
            assert _VOLUME_TABLE[i] <= _VOLUME_TABLE[i - 1]

    def test_constants(self) -> None:
        assert _MAX_DISTANCE_SQ == int(AUDIO_MAX_DISTANCE * AUDIO_MAX_DISTANCE)
