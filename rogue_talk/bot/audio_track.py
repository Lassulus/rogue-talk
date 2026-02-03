"""Custom WebRTC audio track for bot audio output."""

from __future__ import annotations

import asyncio
import fractions
import logging
import time
from typing import TYPE_CHECKING, Any

import numpy as np
import numpy.typing as npt
from aiortc import MediaStreamTrack

from ..audio.pcm import float32_to_int16
from ..common.constants import FRAME_SIZE, SAMPLE_RATE
from .audio import AudioSource

logger = logging.getLogger(__name__)

# Import av for AudioFrame creation
try:
    import av
except ImportError:
    av = None  # type: ignore[assignment]


class BotAudioCaptureTrack(MediaStreamTrack):
    """Custom audio track for bots that pulls from audio sources.

    This track:
    - Pulls audio from a queue of audio sources
    - Sends silence when no audio is queued
    - Handles frame timing (20ms frames at 48kHz)
    """

    kind = "audio"

    def __init__(self) -> None:
        super().__init__()
        self._source_queue: asyncio.Queue[AudioSource] = asyncio.Queue(maxsize=10)
        self._current_source: AudioSource | None = None
        self._start_time: float | None = None
        self._timestamp = 0
        self._sample_rate = SAMPLE_RATE
        self._frame_size = FRAME_SIZE
        self._frame_duration = FRAME_SIZE / SAMPLE_RATE  # 20ms per frame
        self._is_muted = False

    def queue_source(self, source: AudioSource) -> bool:
        """Queue an audio source to be played.

        Args:
            source: Audio source to play.

        Returns:
            True if queued successfully, False if queue is full.
        """
        try:
            self._source_queue.put_nowait(source)
            return True
        except asyncio.QueueFull:
            return False

    def set_muted(self, muted: bool) -> None:
        """Set mute state."""
        self._is_muted = muted

    def is_playing(self) -> bool:
        """Check if currently playing audio."""
        return (
            self._current_source is not None and not self._current_source.is_finished()
        )

    async def recv(self) -> Any:
        """Called by aiortc to get the next audio frame."""
        try:
            if av is None:
                raise RuntimeError("PyAV not available")

            if self._start_time is None:
                self._start_time = time.time()

            # Pace frames to real-time: wait until it's time for this frame
            frames_sent = self._timestamp // self._frame_size
            target_time = self._start_time + (frames_sent * self._frame_duration)
            now = time.time()
            wait_time = target_time - now
            if wait_time > 0:
                await asyncio.sleep(wait_time)

            # Get samples from current source or queue
            pcm_data: npt.NDArray[np.float32] | None = None

            if not self._is_muted:
                # Try current source
                if self._current_source is not None:
                    if self._current_source.is_finished():
                        self._current_source = None
                    else:
                        pcm_data = await self._current_source.get_samples()
                        if pcm_data is None:
                            self._current_source = None

                # Try to get next source from queue
                if self._current_source is None:
                    try:
                        self._current_source = self._source_queue.get_nowait()
                        pcm_data = await self._current_source.get_samples()
                    except asyncio.QueueEmpty:
                        pass

            # Generate silence if no audio data
            if pcm_data is None:
                pcm_data = np.zeros(self._frame_size, dtype=np.float32)

            # Ensure correct size
            if len(pcm_data) < self._frame_size:
                pcm_data = np.pad(
                    pcm_data, (0, self._frame_size - len(pcm_data)), mode="constant"
                )
            elif len(pcm_data) > self._frame_size:
                pcm_data = pcm_data[: self._frame_size]

            # Convert to int16 for av.AudioFrame (mono packed format)
            pcm_int16 = float32_to_int16(pcm_data)

            # Create AudioFrame with manual plane update
            frame = av.AudioFrame(format="s16", layout="mono", samples=len(pcm_int16))
            frame.sample_rate = self._sample_rate
            frame.pts = self._timestamp
            frame.time_base = fractions.Fraction(1, self._sample_rate)
            frame.planes[0].update(pcm_int16.tobytes())

            self._timestamp += len(pcm_int16)
            return frame

        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.error(f"BotAudioCaptureTrack.recv() error: {type(e).__name__}: {e}")
            raise
