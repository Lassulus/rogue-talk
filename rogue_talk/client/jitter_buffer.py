"""Per-player jitter buffer for smooth audio playback."""

import collections
from dataclasses import dataclass


@dataclass
class AudioPacket:
    timestamp_ms: int
    opus_data: bytes
    volume: float


class JitterBuffer:
    """Buffers audio packets to smooth out network jitter."""

    def __init__(self, min_packets: int = 5, max_packets: int = 15):
        # Increased defaults for WiFi tolerance:
        # min_packets=5 (100ms) gives more buffer for jitter
        # max_packets=15 (300ms) allows more buffering before drops
        self.min_packets = min_packets
        self.max_packets = max_packets
        self.packets: collections.deque[AudioPacket] = collections.deque()
        self.playback_started = False

    def add_packet(self, packet: AudioPacket) -> None:
        """Add a packet, maintaining timestamp order."""
        # Note: We don't reset on timestamp gaps anymore - it caused stuttering
        # by requiring min_packets to refill after every speech pause.
        # The buffer handles gaps naturally by just playing what's available.

        if not self.packets or packet.timestamp_ms >= self.packets[-1].timestamp_ms:
            self.packets.append(packet)
        else:
            # Insert in sorted position for out-of-order packets
            for i, p in enumerate(self.packets):
                if packet.timestamp_ms < p.timestamp_ms:
                    self.packets.insert(i, packet)
                    break

        # Drop oldest packets if buffer is too full (prevents latency growth)
        while len(self.packets) > self.max_packets:
            self.packets.popleft()

    def get_next_packet(self) -> AudioPacket | None:
        """Get next packet for playback, or None if not ready."""
        if not self.packets:
            return None

        # Wait for a few packets before starting playback
        if not self.playback_started:
            if len(self.packets) < self.min_packets:
                return None
            self.playback_started = True

        return self.packets.popleft()

    def has_started(self) -> bool:
        """Check if playback has started for this buffer."""
        return self.playback_started

    def reset(self) -> None:
        """Reset buffer state."""
        self.packets.clear()
        self.playback_started = False
