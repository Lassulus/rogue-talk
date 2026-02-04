"""Bot audio track that feeds audio sources into LiveKit."""

from __future__ import annotations

import asyncio
import logging
import time

import numpy as np
import numpy.typing as npt
from livekit import rtc as livekit_rtc

from ..audio.pcm import float32_to_int16
from ..common.constants import FRAME_SIZE, SAMPLE_RATE
from .audio import AudioSource

logger = logging.getLogger(__name__)


class BotAudioTrack:
    """Audio track for bots that pulls from audio sources and feeds LiveKit.

    This track:
    - Pulls audio from a queue of audio sources
    - Sends silence when no audio is queued
    - Handles frame timing (20ms frames at 48kHz)
    - Feeds frames into a LiveKit AudioSource via capture_frame()
    """

    def __init__(self, audio_source: livekit_rtc.AudioSource) -> None:
        self._audio_source = audio_source
        self._source_queue: asyncio.Queue[AudioSource] = asyncio.Queue(maxsize=10)
        self._current_source: AudioSource | None = None
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

    async def run(self) -> None:
        """Run the frame generation loop, feeding audio into LiveKit.

        This replaces aiortc's recv() pull model with an async push loop.
        Should be run as an asyncio task.
        """
        start_time = time.time()
        frames_sent = 0

        try:
            while True:
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
                        pcm_data,
                        (0, self._frame_size - len(pcm_data)),
                        mode="constant",
                    )
                elif len(pcm_data) > self._frame_size:
                    pcm_data = pcm_data[: self._frame_size]

                # Convert float32 to int16 for LiveKit
                pcm_int16 = float32_to_int16(pcm_data)

                # Create LiveKit AudioFrame and send
                frame = livekit_rtc.AudioFrame(
                    data=pcm_int16.tobytes(),
                    sample_rate=SAMPLE_RATE,
                    num_channels=1,
                    samples_per_channel=self._frame_size,
                )
                await self._audio_source.capture_frame(frame)

                # Pace frames to real-time using absolute timing
                frames_sent += 1
                target_time = start_time + (frames_sent * self._frame_duration)
                now = time.time()
                wait_time = target_time - now
                if wait_time > 0:
                    await asyncio.sleep(wait_time)
                elif wait_time < -0.1:
                    # We're way behind - reset timing
                    start_time = time.time()
                    frames_sent = 0

        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.error(f"BotAudioTrack.run() error: {type(e).__name__}: {e}")
