"""Audio source management for bots."""

from __future__ import annotations

import asyncio
import io
import wave
from abc import ABC, abstractmethod
from pathlib import Path
from typing import TYPE_CHECKING

import numpy as np
import numpy.typing as npt

from ..common.constants import FRAME_SIZE, SAMPLE_RATE

import logging

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    pass


class AudioSource(ABC):
    """Base class for audio sources."""

    @abstractmethod
    async def get_samples(self) -> npt.NDArray[np.float32] | None:
        """Get the next frame of audio samples.

        Returns:
            Array of float32 samples at 48kHz mono, or None if no more audio.
        """
        pass

    @abstractmethod
    def is_finished(self) -> bool:
        """Check if the audio source has finished playing."""
        pass


class FileAudioSource(AudioSource):
    """Audio source that loads and streams from an audio file."""

    def __init__(self, path: str | Path) -> None:
        """Load an audio file.

        Args:
            path: Path to audio file (WAV, OGG, MP3, etc.).
        """
        self._path = Path(path)
        self._samples: npt.NDArray[np.float32] | None = None
        self._position = 0
        self._frame_count = 0
        self._load_file()

    def _load_file(self) -> None:
        """Load and convert the audio file to 48kHz mono float32."""
        path = self._path
        suffix = path.suffix.lower()

        if suffix == ".wav":
            self._load_wav()
        elif suffix in (".ogg", ".mp3", ".flac", ".m4a", ".aac"):
            self._load_with_av()
        else:
            # Try av as fallback for unknown formats
            try:
                self._load_with_av()
            except Exception:
                raise ValueError(f"Unsupported audio format: {path.suffix}")

    def _load_wav(self) -> None:
        """Load a WAV file."""
        with wave.open(str(self._path), "rb") as wav:
            n_channels = wav.getnchannels()
            sample_width = wav.getsampwidth()
            framerate = wav.getframerate()
            n_frames = wav.getnframes()

            raw_data = wav.readframes(n_frames)

        # Convert to numpy array based on sample width
        if sample_width == 1:
            # 8-bit unsigned
            audio = np.frombuffer(raw_data, dtype=np.uint8).astype(np.float32)
            audio = (audio - 128) / 128.0
        elif sample_width == 2:
            # 16-bit signed
            audio = np.frombuffer(raw_data, dtype=np.int16).astype(np.float32)
            audio = audio / 32768.0
        elif sample_width == 4:
            # 32-bit signed
            audio = np.frombuffer(raw_data, dtype=np.int32).astype(np.float32)
            audio = audio / 2147483648.0
        else:
            raise ValueError(f"Unsupported sample width: {sample_width}")

        # Convert stereo to mono
        if n_channels == 2:
            audio = audio.reshape(-1, 2).mean(axis=1)
        elif n_channels > 2:
            audio = audio.reshape(-1, n_channels).mean(axis=1)

        # Resample if needed
        if framerate != SAMPLE_RATE:
            audio = self._resample(audio, framerate, SAMPLE_RATE)

        self._samples = audio.astype(np.float32)

    def _load_with_av(self) -> None:
        """Load an audio file using PyAV (supports OGG, MP3, FLAC, etc.)."""
        try:
            import av
            from av.audio.frame import AudioFrame
            from av.audio.stream import AudioStream
        except ImportError:
            raise ImportError("PyAV is required for non-WAV audio formats")

        container = av.open(str(self._path))

        # Find audio stream
        audio_stream: AudioStream | None = None
        for s in container.streams:
            if isinstance(s, AudioStream):
                audio_stream = s
                break

        if audio_stream is None:
            container.close()
            raise ValueError(f"No audio stream in file: {self._path}")

        # Collect all audio frames
        frames: list[npt.NDArray[np.float32]] = []
        for packet in container.demux(audio_stream):
            for frame in packet.decode():
                if isinstance(frame, AudioFrame):
                    # Convert to numpy array
                    arr = frame.to_ndarray()
                    frames.append(arr)

        container.close()

        if not frames:
            raise ValueError(f"No audio data in file: {self._path}")

        # Concatenate all frames
        audio = np.concatenate(frames, axis=1 if frames[0].ndim > 1 else 0)

        # Handle different layouts
        if audio.ndim > 1:
            # Channels are in first dimension, samples in second
            # Convert to mono by averaging channels
            audio = audio.mean(axis=0)

        # Normalize based on dtype
        if audio.dtype == np.int16:
            audio = audio.astype(np.float32) / 32768.0
        elif audio.dtype == np.int32:
            audio = audio.astype(np.float32) / 2147483648.0
        elif audio.dtype == np.float64:
            audio = audio.astype(np.float32)
        elif audio.dtype != np.float32:
            # Assume it's already normalized or convert as float
            audio = audio.astype(np.float32)

        # Resample if needed
        framerate = audio_stream.sample_rate
        if framerate != SAMPLE_RATE:
            audio = self._resample(audio, framerate, SAMPLE_RATE)

        self._samples = audio.astype(np.float32)
        # Find where non-zero audio starts
        nonzero_indices = np.where(np.abs(self._samples) > 0.001)[0]
        first_nonzero = nonzero_indices[0] if len(nonzero_indices) > 0 else -1
        logger.info(
            f"Loaded audio: {len(self._samples)} samples ({len(self._samples) / SAMPLE_RATE:.2f}s), "
            f"max amplitude: {np.abs(self._samples).max():.4f}, "
            f"first nonzero sample at index {first_nonzero}"
        )

    def _resample(
        self,
        audio: npt.NDArray[np.float32],
        src_rate: int,
        dst_rate: int,
    ) -> npt.NDArray[np.float32]:
        """Simple linear resampling."""
        if src_rate == dst_rate:
            return audio

        # Calculate the ratio and new length
        ratio = dst_rate / src_rate
        new_length = int(len(audio) * ratio)

        # Create new time indices
        old_indices = np.arange(len(audio))
        new_indices = np.linspace(0, len(audio) - 1, new_length)

        # Linear interpolation
        resampled = np.interp(new_indices, old_indices, audio)
        return resampled.astype(np.float32)

    async def get_samples(self) -> npt.NDArray[np.float32] | None:
        """Get the next frame of audio samples."""
        if self._samples is None:
            logger.info("get_samples: _samples is None")
            return None
        if self._position >= len(self._samples):
            logger.info(
                f"get_samples: position {self._position} >= len {len(self._samples)}, total frames: {self._frame_count}"
            )
            return None

        end = min(self._position + FRAME_SIZE, len(self._samples))
        frame = self._samples[self._position : end]
        self._position = end
        self._frame_count += 1

        # Log every 10th frame to track progress
        if self._frame_count % 10 == 0:
            logger.info(
                f"Audio frame {self._frame_count}: pos={self._position}/{len(self._samples)}, max={np.abs(frame).max():.4f}"
            )

        # Pad with zeros if needed
        if len(frame) < FRAME_SIZE:
            frame = np.pad(frame, (0, FRAME_SIZE - len(frame)), mode="constant")

        return frame

    def is_finished(self) -> bool:
        """Check if all samples have been read."""
        if self._samples is None:
            logger.info("is_finished: _samples is None")
            return True
        finished = self._position >= len(self._samples)
        if finished:
            logger.info(
                f"is_finished: position {self._position} >= len {len(self._samples)}, frames: {self._frame_count}"
            )
        return finished

    def reset(self) -> None:
        """Reset playback to the beginning."""
        self._position = 0


class PCMAudioSource(AudioSource):
    """Audio source that streams raw PCM samples."""

    def __init__(
        self,
        samples: npt.NDArray[np.float32] | None = None,
        sample_rate: int = SAMPLE_RATE,
    ) -> None:
        """Create a PCM audio source.

        Args:
            samples: Optional initial samples (float32, mono).
            sample_rate: Sample rate of the input (will be resampled to 48kHz).
        """
        self._queue: asyncio.Queue[npt.NDArray[np.float32]] = asyncio.Queue(maxsize=100)
        self._finished = False
        self._sample_rate = sample_rate
        self._buffer: npt.NDArray[np.float32] = np.array([], dtype=np.float32)

        if samples is not None:
            self.add_samples(samples)

    def add_samples(self, samples: npt.NDArray[np.float32]) -> None:
        """Add samples to the queue.

        Args:
            samples: Float32 mono samples at the configured sample rate.
        """
        # Resample if needed
        if self._sample_rate != SAMPLE_RATE:
            samples = self._resample(samples, self._sample_rate, SAMPLE_RATE)

        try:
            self._queue.put_nowait(samples.astype(np.float32))
        except asyncio.QueueFull:
            pass  # Drop if queue is full

    def _resample(
        self,
        audio: npt.NDArray[np.float32],
        src_rate: int,
        dst_rate: int,
    ) -> npt.NDArray[np.float32]:
        """Simple linear resampling."""
        if src_rate == dst_rate:
            return audio

        ratio = dst_rate / src_rate
        new_length = int(len(audio) * ratio)
        old_indices = np.arange(len(audio))
        new_indices = np.linspace(0, len(audio) - 1, new_length)
        resampled = np.interp(new_indices, old_indices, audio)
        return resampled.astype(np.float32)

    def finish(self) -> None:
        """Mark the source as finished (no more samples will be added)."""
        self._finished = True

    async def get_samples(self) -> npt.NDArray[np.float32] | None:
        """Get the next frame of audio samples."""
        # First, try to fill buffer from queue
        while len(self._buffer) < FRAME_SIZE:
            try:
                chunk = self._queue.get_nowait()
                self._buffer = np.concatenate([self._buffer, chunk])
            except asyncio.QueueEmpty:
                break

        # If we have enough, return a frame
        if len(self._buffer) >= FRAME_SIZE:
            frame = self._buffer[:FRAME_SIZE]
            self._buffer = self._buffer[FRAME_SIZE:]
            return frame

        # Not enough samples - if finished, return what we have padded
        if self._finished:
            if len(self._buffer) > 0:
                frame = np.pad(
                    self._buffer, (0, FRAME_SIZE - len(self._buffer)), mode="constant"
                )
                self._buffer = np.array([], dtype=np.float32)
                return frame
            return None

        # Not finished, but no data available yet
        return None

    def is_finished(self) -> bool:
        """Check if the source is finished and buffer is empty."""
        return self._finished and len(self._buffer) == 0 and self._queue.empty()


class SilenceSource(AudioSource):
    """Audio source that generates silence."""

    def __init__(self, duration_ms: int | None = None) -> None:
        """Create a silence source.

        Args:
            duration_ms: Duration in milliseconds, or None for infinite silence.
        """
        self._duration_ms = duration_ms
        self._frames_played = 0
        self._total_frames: int | None
        if duration_ms is not None:
            self._total_frames = (duration_ms * SAMPLE_RATE) // (1000 * FRAME_SIZE)
        else:
            self._total_frames = None

    async def get_samples(self) -> npt.NDArray[np.float32] | None:
        """Get a frame of silence."""
        if self._total_frames is not None and self._frames_played >= self._total_frames:
            return None
        self._frames_played += 1
        return np.zeros(FRAME_SIZE, dtype=np.float32)

    def is_finished(self) -> bool:
        """Check if silence duration has elapsed."""
        if self._total_frames is None:
            return False
        return self._frames_played >= self._total_frames
