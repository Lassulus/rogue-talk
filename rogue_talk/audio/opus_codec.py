"""Opus encoder/decoder wrapper."""

import numpy as np
import opuslib

from ..common.constants import CHANNELS, FRAME_SIZE, OPUS_BITRATE, SAMPLE_RATE


class OpusEncoder:
    def __init__(self):
        self.encoder = opuslib.Encoder(SAMPLE_RATE, CHANNELS, opuslib.APPLICATION_VOIP)
        self.encoder.bitrate = OPUS_BITRATE

    def encode(self, pcm_data: np.ndarray) -> bytes:
        """Encode PCM float32 data to Opus."""
        # Convert float32 [-1.0, 1.0] to int16
        pcm_int16 = (pcm_data * 32767).astype(np.int16)
        return self.encoder.encode(pcm_int16.tobytes(), FRAME_SIZE)


class OpusDecoder:
    def __init__(self):
        self.decoder = opuslib.Decoder(SAMPLE_RATE, CHANNELS)

    def decode(self, opus_data: bytes) -> np.ndarray:
        """Decode Opus data to PCM float32."""
        pcm_bytes = self.decoder.decode(opus_data, FRAME_SIZE)
        pcm_int16 = np.frombuffer(pcm_bytes, dtype=np.int16)
        return pcm_int16.astype(np.float32) / 32767.0
