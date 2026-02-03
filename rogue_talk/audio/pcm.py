"""Centralized PCM audio format conversion and resampling utilities."""

import numpy as np
import numpy.typing as npt


def float32_to_int16(data: npt.NDArray[np.float32]) -> npt.NDArray[np.int16]:
    """Convert float32 [-1.0, 1.0] audio to little-endian int16.

    Clips to prevent overflow and uses standard 32768 scaling.
    """
    return np.clip(data * 32768, -32768, 32767).astype("<i2")


def to_float32(
    data: npt.NDArray[np.int16]
    | npt.NDArray[np.int32]
    | npt.NDArray[np.float32]
    | npt.NDArray[np.float64],
) -> npt.NDArray[np.float32]:
    """Normalize any common PCM dtype to float32 [-1.0, 1.0].

    Handles int16, int32, float64, and float32 passthrough.
    """
    if data.dtype == np.int16:
        return data.astype(np.float32) / 32768.0
    elif data.dtype == np.int32:
        return data.astype(np.float32) / 2147483648.0
    else:
        return data.astype(np.float32)


def resample(
    audio: npt.NDArray[np.float32],
    src_rate: int,
    dst_rate: int,
) -> npt.NDArray[np.float32]:
    """Resample audio using linear interpolation.

    Args:
        audio: Source audio data (float32).
        src_rate: Source sample rate.
        dst_rate: Destination sample rate.

    Returns:
        Resampled audio data as float32.
    """
    if src_rate == dst_rate:
        return audio

    new_length = int(len(audio) * dst_rate / src_rate)
    old_indices = np.arange(len(audio))
    new_indices = np.linspace(0, len(audio) - 1, new_length)
    resampled = np.interp(new_indices, old_indices, audio)
    return resampled.astype(np.float32)
