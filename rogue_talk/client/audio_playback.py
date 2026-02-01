"""Audio playback with decoding and mixing."""

from __future__ import annotations

import logging
import time
from collections import defaultdict
from typing import TYPE_CHECKING, Any

import numpy as np
import numpy.typing as npt
import sounddevice as sd

from ..audio.mixer import AudioMixer
from ..audio.opus_codec import OpusDecoder
from ..common.constants import CHANNELS, FRAME_SIZE, SAMPLE_RATE
from .jitter_buffer import AudioPacket, JitterBuffer

if TYPE_CHECKING:
    from ..audio.webrtc_tracks import AudioPlaybackTrack
    from .tile_sound_player import TileSoundPlayer

_logger = logging.getLogger(__name__)


class AudioPlayback:
    """Manages receiving, decoding, and playing back audio from multiple players."""

    # Jitter buffer settings for WebRTC
    # Wait for this many samples before starting playback (100ms = 4800 samples)
    WEBRTC_MIN_BUFFER = FRAME_SIZE * 5  # 100ms
    # Maximum buffer size before discarding old data (300ms = 14400 samples)
    WEBRTC_MAX_BUFFER = FRAME_SIZE * 15  # 300ms

    def __init__(self) -> None:
        self.jitter_buffers: dict[int, JitterBuffer] = defaultdict(JitterBuffer)
        self.decoders: dict[int, OpusDecoder] = {}
        self.mixer = AudioMixer()
        self.stream: sd.OutputStream | None = None
        self.tile_sound_player: TileSoundPlayer | None = None
        # WebRTC audio track for receiving voice
        self.webrtc_track: AudioPlaybackTrack | None = None
        # Pre-allocated ring buffer for WebRTC audio samples (avoids repeated allocations)
        self._webrtc_ring_buffer: npt.NDArray[np.float32] = np.zeros(
            self.WEBRTC_MAX_BUFFER * 2, dtype=np.float32
        )
        self._webrtc_write_pos = 0
        self._webrtc_read_pos = 0
        # Track if WebRTC playback has started (for jitter buffering)
        self._webrtc_playback_started = False
        # Diagnostics
        self._underrun_count: dict[int, int] = defaultdict(int)
        self._frame_count: dict[int, int] = defaultdict(int)
        self._last_log_time = 0.0

    def start(self) -> None:
        """Start audio output stream."""
        self.stream = sd.OutputStream(
            samplerate=SAMPLE_RATE,
            channels=CHANNELS,
            dtype=np.float32,
            blocksize=FRAME_SIZE,
            callback=self._audio_callback,
        )
        self.stream.start()

    def stop(self) -> None:
        """Stop audio output stream."""
        if self.stream:
            self.stream.stop()
            self.stream.close()
            self.stream = None

    def receive_audio_frame(
        self, player_id: int, timestamp_ms: int, opus_data: bytes, volume: float
    ) -> None:
        """Process an incoming audio frame from the network."""
        packet = AudioPacket(
            timestamp_ms=timestamp_ms,
            opus_data=opus_data,
            volume=volume,
        )
        self.jitter_buffers[player_id].add_packet(packet)

    def remove_player(self, player_id: int) -> None:
        """Clean up resources for a player who left."""
        self.jitter_buffers.pop(player_id, None)
        self.decoders.pop(player_id, None)
        self.mixer.remove_player(player_id)

    def _get_decoder(self, player_id: int) -> OpusDecoder:
        """Get or create decoder for a player."""
        if player_id not in self.decoders:
            self.decoders[player_id] = OpusDecoder()
        return self.decoders[player_id]

    def _audio_callback(
        self,
        outdata: npt.NDArray[np.float32],
        frames: int,
        time_info: Any,
        status: sd.CallbackFlags,
    ) -> None:
        """Sounddevice callback - runs in separate thread."""
        # Process WebRTC audio track (voice from server)
        # Get exactly FRAME_SIZE samples for this callback
        webrtc_frame: npt.NDArray[np.float32] | None = None

        if self.webrtc_track is not None:
            ring_buf = self._webrtc_ring_buffer
            buf_size = len(ring_buf)

            # Drain all available frames from WebRTC track into ring buffer
            while True:
                frame_data = self.webrtc_track.get_frame()
                if frame_data is None:
                    break
                pcm_data, volume = frame_data
                samples = pcm_data.flatten()
                sample_len = len(samples)

                # Check if we have space (avoid overflow)
                available = (
                    self._webrtc_read_pos - self._webrtc_write_pos - 1
                ) % buf_size
                if available == 0:
                    available = buf_size - 1
                if sample_len > available:
                    # Buffer full, discard oldest data by advancing read pointer
                    discard = sample_len - available
                    self._webrtc_read_pos = (self._webrtc_read_pos + discard) % buf_size

                # Write samples to ring buffer (handle wrap-around)
                write_pos = self._webrtc_write_pos
                end_pos = write_pos + sample_len
                if end_pos <= buf_size:
                    ring_buf[write_pos:end_pos] = samples * volume
                else:
                    first_part = buf_size - write_pos
                    ring_buf[write_pos:buf_size] = samples[:first_part] * volume
                    ring_buf[: end_pos - buf_size] = samples[first_part:] * volume
                self._webrtc_write_pos = end_pos % buf_size

            # Calculate buffered samples
            buffer_len = (self._webrtc_write_pos - self._webrtc_read_pos) % buf_size

            # Jitter buffering: wait for minimum buffer before starting playback
            if not self._webrtc_playback_started:
                if buffer_len >= self.WEBRTC_MIN_BUFFER:
                    self._webrtc_playback_started = True

            # Extract exactly FRAME_SIZE samples for this callback
            if self._webrtc_playback_started and buffer_len >= FRAME_SIZE:
                read_pos = self._webrtc_read_pos
                end_pos = read_pos + FRAME_SIZE
                if end_pos <= buf_size:
                    webrtc_frame = ring_buf[read_pos:end_pos].copy()
                else:
                    first_part = buf_size - read_pos
                    webrtc_frame = np.concatenate(
                        [ring_buf[read_pos:buf_size], ring_buf[: end_pos - buf_size]]
                    )
                self._webrtc_read_pos = end_pos % buf_size
            elif self._webrtc_playback_started and buffer_len < FRAME_SIZE:
                # Buffer underrun - reset playback state to rebuffer
                self._webrtc_playback_started = False

        if webrtc_frame is not None:
            self.mixer.add_frame(0, webrtc_frame, 1.0)

        # Process each player's jitter buffer (legacy TCP path, not used with WebRTC)
        for player_id, jitter_buffer in list(self.jitter_buffers.items()):
            packet = jitter_buffer.get_next_packet()
            if packet is not None:
                # Decode Opus to PCM
                decoder = self._get_decoder(player_id)
                pcm = decoder.decode(packet.opus_data)

                # Add to mixer with volume
                self.mixer.add_frame(player_id, pcm, packet.volume)
                self._frame_count[player_id] += 1
            elif jitter_buffer.has_started():
                # Underrun: buffer empty after playback started
                self._underrun_count[player_id] += 1

        # Mix all voice streams
        mixed = self.mixer.mix()

        # Mix in tile sounds if available
        if self.tile_sound_player:
            tile_audio = self.tile_sound_player.get_mixed_frame()
            mixed = mixed + tile_audio
            # Soft clip to prevent harsh distortion
            mixed = np.tanh(mixed)

        # Write to output (reshape for sounddevice)
        outdata[:] = mixed.reshape(-1, 1)
