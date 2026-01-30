"""Per-player jitter buffer for smooth audio playback."""

import collections
from dataclasses import dataclass

from ..common.constants import JITTER_BUFFER_MS


@dataclass
class AudioPacket:
    timestamp_ms: int
    opus_data: bytes
    volume: float


class JitterBuffer:
    """Buffers audio packets to smooth out network jitter."""

    def __init__(self, buffer_size_ms: int = JITTER_BUFFER_MS):
        self.buffer_size_ms = buffer_size_ms
        self.packets: collections.deque[AudioPacket] = collections.deque()
        self.playback_started = False

    def add_packet(self, packet: AudioPacket) -> None:
        """Add a packet, maintaining timestamp order."""
        if not self.packets or packet.timestamp_ms >= self.packets[-1].timestamp_ms:
            self.packets.append(packet)
        else:
            # Insert in sorted position for out-of-order packets
            for i, p in enumerate(self.packets):
                if packet.timestamp_ms < p.timestamp_ms:
                    self.packets.insert(i, packet)
                    break

    def get_next_packet(self) -> AudioPacket | None:
        """Get next packet for playback, or None if not ready."""
        if not self.packets:
            return None

        # Wait for buffer to fill before starting
        if not self.playback_started:
            if len(self.packets) < 2:
                return None
            total_buffered = self.packets[-1].timestamp_ms - self.packets[0].timestamp_ms
            if total_buffered < self.buffer_size_ms:
                return None
            self.playback_started = True

        return self.packets.popleft()

    def reset(self) -> None:
        """Reset buffer state."""
        self.packets.clear()
        self.playback_started = False
