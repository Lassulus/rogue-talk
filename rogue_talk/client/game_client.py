"""Main game client handling network and UI."""

from __future__ import annotations

import asyncio
import logging
import struct
import tempfile
import time
import urllib.parse
import warnings
from asyncio import StreamReader, StreamWriter
from pathlib import Path
from typing import TYPE_CHECKING, Any

from blessed import Terminal
from blessed.keyboard import Keystroke
from livekit import rtc as livekit_rtc

_logger = logging.getLogger(__name__)


def _asyncio_exception_handler(
    loop: asyncio.AbstractEventLoop, context: dict[str, Any]
) -> None:
    """Custom asyncio exception handler that logs to file instead of stderr.

    By default, asyncio prints unhandled task exceptions to stderr, which
    interferes with the terminal UI. This redirects them to the log file.
    """
    exception = context.get("exception")
    message = context.get("message", "Unhandled exception in asyncio task")
    if exception:
        _logger.error(f"{message}: {exception}", exc_info=exception)
    else:
        _logger.error(message)


from ..audio.sound_loader import SoundCache
from ..common import tiles as tile_defs
from ..common.constants import FRAME_SIZE, MOVEMENT_TICK_INTERVAL, SAMPLE_RATE
from ..common.crypto import sign_challenge
from ..common.protocol import (
    AuthResult,
    MessageType,
    PlayerInfo,
    deserialize_auth_challenge,
    deserialize_auth_result,
    deserialize_door_transition,
    deserialize_level_files_data,
    deserialize_level_manifest,
    deserialize_level_pack_data,
    deserialize_livekit_token,
    deserialize_player_joined,
    deserialize_player_left,
    deserialize_position_ack,
    deserialize_server_hello,
    deserialize_world_state,
    read_message,
    serialize_auth_response,
    serialize_level_files_request,
    serialize_level_manifest_request,
    serialize_level_pack_request,
    serialize_mute_status,
    serialize_position_update,
    write_message,
)
from .identity import Identity, load_or_create_identity
from .input_handler import (
    get_movement,
    is_help_key,
    is_interact_key,
    is_log_key,
    is_mute_key,
    is_player_table_key,
    is_quit_key,
    is_show_names_key,
)
from .log_buffer import LogBuffer
from .level import Level
from .level_cache import cache_received_files, get_cached_files
from .level_pack import (
    LevelPack,
    create_level_pack_from_dir,
    extract_level_pack,
    parse_doors,
    parse_interactions,
    parse_streams,
    write_files_to_dir,
)
from .stream_player import StreamPlayer
from .terminal_ui import TerminalUI
from .tile_sound_player import TileSoundPlayer

if TYPE_CHECKING:
    from .audio_capture import AudioCapture
    from .audio_playback import AudioPlayback


class GameClient:
    def __init__(self, host: str, port: int, name: str) -> None:
        self.host = host
        self.port = port
        self.name = name
        self.identity: Identity | None = None
        self.player_id: int = 0
        self.x: int = 0
        self.y: int = 0
        self.room_width: int = 0
        self.room_height: int = 0
        self.level: Level | None = None
        self.current_level: str = "main"
        self.is_muted: bool = False
        self.show_player_names: bool = True
        self.show_player_table: bool = False
        self.show_help: bool = False
        self.show_logs: bool = False
        self.log_buffer: LogBuffer | None = None
        self._log_scroll_offset: int = 0  # Vertical: 0 = showing most recent
        self._log_scroll_x: int = 0  # Horizontal scroll offset
        # Interaction system
        self._interact_pending_time: float | None = None
        self._interact_lines: list[str] | None = None
        self._interact_line_index: int = 0
        self._interact_anim_start: float = 0.0
        self._interact_chars_per_sec: float = 60.0  # Typewriter speed
        self.players: list[PlayerInfo] = []
        # TCP connection (stays open for entire session)
        self.reader: StreamReader | None = None
        self.writer: StreamWriter | None = None
        # LiveKit room connection
        self._livekit_room: livekit_rtc.Room | None = None
        self._livekit_audio_source: livekit_rtc.AudioSource | None = None
        self._livekit_connected: bool = False
        # Track async tasks for receiving audio from remote participants
        self._audio_receive_tasks: dict[str, asyncio.Task[None]] = {}
        self.running = False
        self._needs_render = True  # Flag to track when re-render is needed
        self._last_render_time = 0.0  # For periodic updates (mic level, animations)
        self.term: Any = Terminal()
        self.ui = TerminalUI(self.term)
        self.audio_capture: AudioCapture | None = None
        self.audio_playback: AudioPlayback | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        # Queue for outgoing position updates (non-blocking sends)
        self._position_queue: asyncio.Queue[tuple[int, int, int]] | None = None
        # Client-side prediction: track pending (unacked) moves
        self._move_seq: int = 0
        # seq -> (dx, dy, expected_x, expected_y)
        self._pending_moves: dict[int, tuple[int, int, int, int]] = {}
        # Movement rate limiting (matches server's MOVEMENT_TICK_INTERVAL)
        self._last_move_time: float = 0.0
        # Temporary directory for level pack extraction
        self._temp_dir: tempfile.TemporaryDirectory[str] | None = None
        # Tile sound system
        self._sound_cache: SoundCache = SoundCache()
        self._tile_sound_player: TileSoundPlayer = TileSoundPlayer(self._sound_cache)
        # Audio stream player (for radio streams at map locations)
        self._stream_player: StreamPlayer = StreamPlayer()
        # Other levels loaded for see-through portals
        self.other_levels: dict[str, Level] = {}
        # Pending futures for level caching protocol
        self._pending_manifest_future: asyncio.Future[bytes] | None = None
        self._pending_files_future: asyncio.Future[bytes] | None = None

    async def connect(self) -> bool:
        """Connect to the server and complete handshake."""
        # Load or create identity
        self.identity = load_or_create_identity()
        print(f"Connecting to {self.host}:{self.port}...")

        try:
            self.reader, self.writer = await asyncio.wait_for(
                asyncio.open_connection(
                    self.host, self.port, happy_eyeballs_delay=0.25
                ),
                timeout=10.0,
            )
            print("TCP connection established")
        except asyncio.TimeoutError:
            print(
                f"Connection timed out after 10s (is {self.host}:{self.port} reachable?)"
            )
            return False
        except ConnectionRefusedError:
            print(f"Connection refused by {self.host}:{self.port}")
            return False
        except OSError as e:
            print(f"Failed to connect: {e}")
            return False

        # Type narrowing for mypy (reader/writer are set after successful connection)
        assert self.reader is not None and self.writer is not None
        reader = self.reader
        writer = self.writer

        # Wait for AUTH_CHALLENGE
        print("Waiting for AUTH_CHALLENGE...")
        try:
            msg_type, payload = await asyncio.wait_for(
                read_message(reader), timeout=10.0
            )
        except asyncio.TimeoutError:
            print(
                "Timeout waiting for AUTH_CHALLENGE (server may not be running rogue-talk)"
            )
            return False
        except asyncio.IncompleteReadError as e:
            print(f"Connection closed during handshake: {e}")
            return False

        if msg_type != MessageType.AUTH_CHALLENGE:
            print(
                f"Unexpected response from server: got {msg_type.name}, expected AUTH_CHALLENGE"
            )
            return False

        nonce = deserialize_auth_challenge(payload)
        print("Received AUTH_CHALLENGE, sending AUTH_RESPONSE...")

        # Sign the challenge
        signature = sign_challenge(self.identity.private_key, nonce, self.name)

        # Send AUTH_RESPONSE
        await write_message(
            writer,
            MessageType.AUTH_RESPONSE,
            serialize_auth_response(self.identity.public_key, self.name, signature),
        )

        # Wait for AUTH_RESULT
        print("Waiting for AUTH_RESULT...")
        try:
            msg_type, payload = await asyncio.wait_for(
                read_message(reader), timeout=10.0
            )
        except asyncio.TimeoutError:
            print("Timeout waiting for AUTH_RESULT")
            return False
        except asyncio.IncompleteReadError as e:
            print(f"Connection closed during authentication: {e}")
            return False

        if msg_type != MessageType.AUTH_RESULT:
            print(
                f"Unexpected response from server: got {msg_type.name}, expected AUTH_RESULT"
            )
            return False

        auth_result = deserialize_auth_result(payload)
        if auth_result != AuthResult.SUCCESS:
            error_messages = {
                AuthResult.NAME_TAKEN: "Name is already taken by another player",
                AuthResult.KEY_MISMATCH: "Your key is registered with a different name",
                AuthResult.INVALID_SIGNATURE: "Authentication failed (invalid signature)",
                AuthResult.INVALID_NAME: "Invalid name",
                AuthResult.ALREADY_CONNECTED: "You are already connected to this server",
            }
            print(
                f"Authentication failed: {error_messages.get(auth_result, 'Unknown error')}"
            )
            return False

        print("Authentication successful, waiting for SERVER_HELLO...")

        # Wait for SERVER_HELLO to learn which level we're in
        try:
            msg_type, payload = await asyncio.wait_for(
                read_message(reader), timeout=10.0
            )
        except asyncio.TimeoutError:
            print("Timeout waiting for SERVER_HELLO")
            return False
        except asyncio.IncompleteReadError as e:
            print(f"Connection closed while waiting for SERVER_HELLO: {e}")
            return False

        if msg_type != MessageType.SERVER_HELLO:
            print(
                f"Unexpected response from server: got {msg_type.name}, expected SERVER_HELLO"
            )
            return False

        (
            self.player_id,
            self.room_width,
            self.room_height,
            self.x,
            self.y,
            level_data,
            level_name,
        ) = deserialize_server_hello(payload)
        self.level = Level.from_bytes(level_data)
        self.current_level = level_name
        print(f"Received SERVER_HELLO: player_id={self.player_id}, level={level_name}")

        # Request level files using content-addressed caching
        # The server sends LIVEKIT_TOKEN right after SERVER_HELLO, so it may
        # arrive while we're exchanging level data. Buffer it if we see it.
        print("Requesting level files...")
        self._buffered_livekit_token: tuple[str, str] | None = None
        level_pack = await self._request_level_cached_tcp(level_name)
        if level_pack is None:
            print("Failed to load level pack")
            return False

        # Load custom tiles if present
        if level_pack.tiles_path:
            tile_defs.reload_tiles(level_pack.tiles_path)

        # Set up sound assets directory
        self._sound_cache.set_assets_dir(level_pack.assets_dir)

        # Parse and set doors from level.json
        doors = parse_doors(level_pack.level_json_path)
        self.level.doors = doors

        # Parse and set streams from level.json
        streams = parse_streams(level_pack.level_json_path)
        self.level.streams = streams

        # Parse and set interactions from level.json
        interactions = parse_interactions(level_pack.level_json_path)
        self.level.interactions = interactions

        # Get LIVEKIT_TOKEN (may have been buffered during level loading)
        if self._buffered_livekit_token is not None:
            livekit_url, livekit_token = self._buffered_livekit_token
        else:
            print("Waiting for LiveKit token...")
            try:
                msg_type, payload = await asyncio.wait_for(
                    read_message(reader), timeout=10.0
                )
            except asyncio.TimeoutError:
                print("Timeout waiting for LIVEKIT_TOKEN")
                return False
            except asyncio.IncompleteReadError as e:
                print(f"Connection closed while waiting for LiveKit token: {e}")
                return False

            if msg_type != MessageType.LIVEKIT_TOKEN:
                print(
                    f"Unexpected response from server: got {msg_type.name}, expected LIVEKIT_TOKEN"
                )
                return False

            livekit_url, livekit_token = deserialize_livekit_token(payload)

        # Replace the host in the LiveKit URL with the game server host,
        # since LiveKit runs on the same machine as the game server.
        livekit_url = self._rewrite_livekit_url(livekit_url)
        print(f"Received LiveKit token, connecting to {livekit_url}...")

        # Connect to LiveKit SFU
        if not await self._connect_livekit(livekit_url, livekit_token):
            print("Failed to connect to LiveKit")
            return False

        print("LiveKit connection established")

        return True

    async def _request_level_cached_tcp(self, level_name: str) -> LevelPack | None:
        """Request level files via TCP using content-addressed caching."""
        if not self.writer or not self.reader:
            return None

        # Request manifest
        await write_message(
            self.writer,
            MessageType.LEVEL_MANIFEST_REQUEST,
            serialize_level_manifest_request(level_name),
        )

        # Wait for LEVEL_MANIFEST
        try:
            while True:
                msg_type, payload = await asyncio.wait_for(
                    read_message(self.reader), timeout=10.0
                )
                if msg_type == MessageType.LEVEL_MANIFEST:
                    break
                if msg_type == MessageType.LIVEKIT_TOKEN:
                    self._buffered_livekit_token = deserialize_livekit_token(payload)
                # Ignore other messages during initial connection
        except asyncio.TimeoutError:
            print("Timeout waiting for LEVEL_MANIFEST")
            return None
        except asyncio.IncompleteReadError as e:
            print(f"Connection closed while requesting level manifest: {e}")
            return None

        manifest = deserialize_level_manifest(payload)
        if not manifest:
            print("Server returned empty manifest")
            return None

        # Check local cache
        cached_files, missing_files = get_cached_files(level_name, manifest)
        cached_count = len(cached_files)
        total_count = len(manifest)

        if missing_files:
            # Request missing files from server
            await write_message(
                self.writer,
                MessageType.LEVEL_FILES_REQUEST,
                serialize_level_files_request(level_name, missing_files),
            )

            # Wait for LEVEL_FILES_DATA
            try:
                while True:
                    msg_type, payload = await asyncio.wait_for(
                        read_message(self.reader), timeout=30.0
                    )
                    if msg_type == MessageType.LEVEL_FILES_DATA:
                        break
                    if msg_type == MessageType.LIVEKIT_TOKEN:
                        self._buffered_livekit_token = deserialize_livekit_token(
                            payload
                        )
                    # Ignore other messages
            except asyncio.TimeoutError:
                print("Timeout waiting for LEVEL_FILES_DATA")
                return None
            except asyncio.IncompleteReadError as e:
                print(f"Connection closed while downloading level files: {e}")
                return None

            new_files = deserialize_level_files_data(payload)

            # Cache the new files
            cache_received_files(level_name, manifest, new_files)

            # Combine with cached files
            all_files = {**cached_files, **new_files}
            print(
                f"Level {level_name}: {cached_count}/{total_count} cached, "
                f"downloaded {len(new_files)} files"
            )
        else:
            all_files = cached_files
            print(f"Level {level_name}: {cached_count}/{total_count} files from cache")

        # Write all files to temp directory
        self._temp_dir = tempfile.TemporaryDirectory(prefix="rogue_talk_")
        extract_dir = Path(self._temp_dir.name)
        write_files_to_dir(all_files, extract_dir)

        try:
            return create_level_pack_from_dir(extract_dir)
        except ValueError as e:
            print(f"Failed to create level pack: {e}")
            return None

    async def _connect_livekit(self, url: str, token: str) -> bool:
        """Connect to LiveKit SFU and set up audio publishing."""
        try:
            self._livekit_room = livekit_rtc.Room()
            room = self._livekit_room

            # Handle incoming audio tracks (when server subscribes us to someone)
            @room.on("track_subscribed")  # type: ignore[misc]
            def on_track_subscribed(
                track: livekit_rtc.Track,
                publication: livekit_rtc.RemoteTrackPublication,
                participant: livekit_rtc.RemoteParticipant,
            ) -> None:
                if track.kind == livekit_rtc.TrackKind.KIND_AUDIO:
                    player_name = participant.identity
                    _logger.debug(f"Track subscribed from {player_name}")
                    # Start receiving audio from this participant
                    task = asyncio.create_task(
                        self._receive_audio_from_participant(
                            player_name,
                            track,
                        )
                    )
                    self._audio_receive_tasks[player_name] = task

            @room.on("track_unsubscribed")  # type: ignore[misc]
            def on_track_unsubscribed(
                track: livekit_rtc.Track,
                publication: livekit_rtc.RemoteTrackPublication,
                participant: livekit_rtc.RemoteParticipant,
            ) -> None:
                player_name = participant.identity
                _logger.debug(f"Track unsubscribed from {player_name}")
                # Cancel the receive task
                task = self._audio_receive_tasks.pop(player_name, None)
                if task:
                    task.cancel()
                # Clean up playback
                if self.audio_playback:
                    self.audio_playback.remove_player(player_name)

            # Connect to the room
            await room.connect(url, token)

            # Create audio source and publish a local audio track
            self._livekit_audio_source = livekit_rtc.AudioSource(
                SAMPLE_RATE,
                1,  # mono
            )
            local_track = livekit_rtc.LocalAudioTrack.create_audio_track(
                "microphone", self._livekit_audio_source
            )
            await room.local_participant.publish_track(local_track)

            self._livekit_connected = True
            return True
        except Exception as e:
            print(f"LiveKit connection failed: {type(e).__name__}: {e}")
            _logger.error(f"LiveKit connection failed: {e}")
            return False

    async def _receive_audio_from_participant(
        self,
        player_name: str,
        track: livekit_rtc.RemoteAudioTrack,
    ) -> None:
        """Receive audio frames from a LiveKit participant and feed to playback."""
        import numpy as np

        try:
            audio_stream = livekit_rtc.AudioStream(track)
            async for frame_event in audio_stream:
                frame = frame_event.frame
                # Convert int16 PCM from LiveKit to float32 for playback pipeline
                samples = np.frombuffer(frame.data, dtype=np.int16)
                float_samples = samples.astype(np.float32) / 32768.0

                if self.audio_playback:
                    self.audio_playback.feed_audio(player_name, float_samples)
        except asyncio.CancelledError:
            pass
        except Exception as e:
            _logger.error(f"Error receiving audio from {player_name}: {e}")

    def _rewrite_livekit_url(self, livekit_url: str) -> str:
        """Replace the host in the LiveKit URL with the game server host.

        LiveKit runs on the same machine as the game server, so the client
        should connect to LiveKit using the same host it used for TCP.
        """
        parsed = urllib.parse.urlparse(livekit_url)
        return urllib.parse.urlunparse(
            parsed._replace(netloc=f"{self.host}:{parsed.port or 7880}")
        )

    def _get_player_name_by_id(self, player_id: int) -> str | None:
        """Look up a player's name by their ID."""
        for p in self.players:
            if p.player_id == player_id:
                return p.name
        return None

    async def run(self) -> None:
        """Main client loop."""
        self.running = True
        self._loop = asyncio.get_running_loop()
        self._position_queue = asyncio.Queue()

        # Redirect asyncio exceptions to log file instead of stderr (avoids TUI flicker)
        self._loop.set_exception_handler(_asyncio_exception_handler)

        # Redirect Python warnings to logging (avoids TUI flicker under load)
        logging.captureWarnings(True)
        warnings.filterwarnings("always")  # Let logging handle all warnings

        # Start audio capture (feeds into LiveKit audio source)
        await self._start_audio()

        # Initialize audio playback with our known position
        if self.audio_playback:
            self.audio_playback.update_positions(self.x, self.y, {})

        # Start position sender task (uses TCP)
        position_sender_task = asyncio.create_task(self._send_position_updates())

        # Start TCP message receiver task (must start before loading portal levels,
        # since _request_level_cached uses futures fulfilled by the receiver)
        message_receiver_task = asyncio.create_task(self._receive_messages())

        # Load other levels for see-through portals (via TCP)
        # This needs the message receiver task running to handle responses.
        await self._load_see_through_portal_levels()

        try:
            with self.term.fullscreen(), self.term.cbreak(), self.term.hidden_cursor():
                self._render()
                while self.running:
                    # Drain all pending input (process buffered keys immediately)
                    while True:
                        key = self.term.inkey(timeout=0)
                        if not key:
                            break
                        await self._handle_input(key)

                    # Check for interact timeout (space pressed without direction)
                    now = time.monotonic()
                    if self._interact_pending_time is not None:
                        if now - self._interact_pending_time > 0.2:  # 200ms timeout
                            self._interact_with_tile(self.x, self.y)
                            self._interact_pending_time = None

                    # Check if interaction popup is active (keep fast updates while visible)
                    has_interact_popup = self._interact_lines is not None

                    # Render when something changed, periodically, or during popup display
                    render_interval = 0.016 if has_interact_popup else 0.25
                    if (
                        self._needs_render
                        or (now - self._last_render_time) > render_interval
                    ):
                        self._render()
                        self._needs_render = False
                        self._last_render_time = now

                    # Sleep shorter during popup for smooth typewriter effect
                    sleep_time = 0.016 if has_interact_popup else 0.05
                    await asyncio.sleep(sleep_time)
        finally:
            self.running = False
            position_sender_task.cancel()
            message_receiver_task.cancel()
            try:
                await position_sender_task
            except asyncio.CancelledError:
                pass
            try:
                await message_receiver_task
            except asyncio.CancelledError:
                pass
            await self._stop_audio()

            # Cancel all audio receive tasks
            for task in self._audio_receive_tasks.values():
                task.cancel()
            for task in self._audio_receive_tasks.values():
                try:
                    await task
                except asyncio.CancelledError:
                    pass
            self._audio_receive_tasks.clear()

            # Disconnect from LiveKit
            if self._livekit_room:
                await self._livekit_room.disconnect()

            # Close TCP connection
            if self.writer:
                self.writer.close()
                try:
                    await self.writer.wait_closed()
                except Exception:
                    pass  # Connection may already be gone
            if self._temp_dir:
                self._temp_dir.cleanup()
            self.ui.cleanup()

    async def _receive_messages(self) -> None:
        """Receive and handle messages from server over TCP."""
        try:
            while self.running and self.reader:
                msg_type, payload = await read_message(self.reader)
                await self._handle_server_message(msg_type, payload)
        except (
            asyncio.IncompleteReadError,
            ConnectionResetError,
            BrokenPipeError,
            OSError,
        ):
            self.running = False

    async def _handle_server_message(
        self, msg_type: MessageType, payload: bytes
    ) -> None:
        """Handle a message from the server."""
        if msg_type == MessageType.WORLD_STATE:
            world_state = deserialize_world_state(payload)
            self.players = world_state.players

            if self.audio_playback:
                # Update positions using player names as keys
                positions = {p.name: (p.x, p.y) for p in self.players}
                self.audio_playback.update_positions(self.x, self.y, positions)
            # Only update our position from server if no pending moves
            # (otherwise we'd rubber-band while moves are in-flight)
            if not self._pending_moves:
                for p in self.players:
                    if p.player_id == self.player_id:
                        self.x = p.x
                        self.y = p.y
                        break
            self._needs_render = True

        elif msg_type == MessageType.POSITION_ACK:
            seq, server_x, server_y = deserialize_position_ack(payload)
            # Check if this move was rejected (position doesn't match expected)
            acked_move = self._pending_moves.get(seq)
            move_rejected = False
            if acked_move:
                _, _, expected_x, expected_y = acked_move
                if server_x != expected_x or server_y != expected_y:
                    move_rejected = True
            # Remove this move and all older moves from pending
            seqs_to_remove = [s for s in self._pending_moves if s <= seq]
            for s in seqs_to_remove:
                del self._pending_moves[s]
            # If move was rejected, clear all pending moves - they were sent with
            # wrong absolute positions and will also be rejected
            if move_rejected:
                self._pending_moves.clear()
            # Set position from server
            self.x = server_x
            self.y = server_y
            # Replay remaining pending moves (only if not rejected)
            if self._pending_moves and self.level and not move_rejected:
                for move_seq in sorted(self._pending_moves.keys()):
                    dx, dy, _, _ = self._pending_moves[move_seq]
                    new_x = self.x + dx
                    new_y = self.y + dy
                    if self.level.is_walkable(new_x, new_y):
                        self.x = new_x
                        self.y = new_y
            self._needs_render = True

        elif msg_type == MessageType.PLAYER_JOINED:
            player_id, name = deserialize_player_joined(payload)
            # Will be updated in next WORLD_STATE

        elif msg_type == MessageType.PLAYER_LEFT:
            player_id = deserialize_player_left(payload)
            # Look up player name before removing from list
            left_player_name = self._get_player_name_by_id(player_id)
            self.players = [p for p in self.players if p.player_id != player_id]
            if left_player_name:
                if self.audio_playback:
                    self.audio_playback.remove_player(left_player_name)
                # Cancel audio receive task for this player
                task = self._audio_receive_tasks.pop(left_player_name, None)
                if task:
                    task.cancel()
            self._needs_render = True

        elif msg_type == MessageType.DOOR_TRANSITION:
            await self._handle_door_transition(payload)

        elif msg_type == MessageType.PING:
            # Respond with PONG to keep connection alive
            await self._send_message(MessageType.PONG, b"")

        elif msg_type == MessageType.LEVEL_PACK_DATA:
            # Handle level pack data (for door transitions)
            if (
                hasattr(self, "_pending_level_pack_future")
                and self._pending_level_pack_future
            ):
                self._pending_level_pack_future.set_result(payload)

        elif msg_type == MessageType.LEVEL_MANIFEST:
            # Handle level manifest data (for cached level loading)
            if self._pending_manifest_future:
                self._pending_manifest_future.set_result(payload)

        elif msg_type == MessageType.LEVEL_FILES_DATA:
            # Handle level files data (for cached level loading)
            if self._pending_files_future:
                self._pending_files_future.set_result(payload)

    async def _send_message(self, msg_type: MessageType, payload: bytes) -> None:
        """Send a message to the server via TCP."""
        if self.writer is None:
            return
        try:
            await write_message(self.writer, msg_type, payload)
        except (ConnectionResetError, BrokenPipeError, OSError):
            self.running = False

    async def _request_level_cached(
        self, level_name: str, extract_dir: Path
    ) -> LevelPack | None:
        """Request level files via TCP using content-addressed caching.

        This is used during gameplay for door transitions and portal level loading.
        """
        if self.writer is None or self.reader is None:
            return None

        # Request manifest
        self._pending_manifest_future = asyncio.Future()
        await self._send_message(
            MessageType.LEVEL_MANIFEST_REQUEST,
            serialize_level_manifest_request(level_name),
        )

        # Wait for manifest
        try:
            payload = await asyncio.wait_for(
                self._pending_manifest_future, timeout=10.0
            )
        except asyncio.TimeoutError:
            return None
        finally:
            self._pending_manifest_future = None

        manifest = deserialize_level_manifest(payload)
        if not manifest:
            return None

        # Check local cache
        cached_files, missing_files = get_cached_files(level_name, manifest)
        cached_count = len(cached_files)
        total_count = len(manifest)

        if missing_files:
            # Request missing files from server
            self._pending_files_future = asyncio.Future()
            await self._send_message(
                MessageType.LEVEL_FILES_REQUEST,
                serialize_level_files_request(level_name, missing_files),
            )

            # Wait for files
            try:
                payload = await asyncio.wait_for(
                    self._pending_files_future, timeout=10.0
                )
            except asyncio.TimeoutError:
                return None
            finally:
                self._pending_files_future = None

            new_files = deserialize_level_files_data(payload)

            # Cache the new files
            cache_received_files(level_name, manifest, new_files)

            # Combine with cached files
            all_files = {**cached_files, **new_files}
            _logger.info(
                f"Level {level_name}: {cached_count}/{total_count} cached, "
                f"downloaded {len(new_files)} files"
            )
        else:
            all_files = cached_files
            _logger.info(
                f"Level {level_name}: {cached_count}/{total_count} files from cache"
            )

        # Write all files to directory
        write_files_to_dir(all_files, extract_dir)

        try:
            return create_level_pack_from_dir(extract_dir)
        except ValueError:
            return None

    async def _handle_door_transition(self, payload: bytes) -> None:
        """Handle a door transition to a new level."""
        target_level, spawn_x, spawn_y = deserialize_door_transition(payload)

        # Clear pending moves immediately to prevent rubber banding
        # (POSITION_ACK may arrive while we're loading the new level)
        self._pending_moves.clear()

        # Clean up old temp directory and create new one
        if self._temp_dir:
            self._temp_dir.cleanup()
        self._temp_dir = tempfile.TemporaryDirectory(prefix="rogue_talk_")
        extract_dir = Path(self._temp_dir.name)

        # Request level files using content-addressed caching
        level_pack = await self._request_level_cached(target_level, extract_dir)
        if level_pack is None:
            return

        # Load custom tiles if present
        if level_pack.tiles_path:
            tile_defs.reload_tiles(level_pack.tiles_path)
        else:
            # Reset to default tiles
            tile_defs.reload_tiles()

        # Update sound assets directory for new level pack
        self._sound_cache.set_assets_dir(level_pack.assets_dir)
        self._tile_sound_player.clear()
        self._stream_player.clear()

        # Load the new level from the pack
        with open(level_pack.level_path, encoding="utf-8") as f:
            level_content = f.read()

        # Parse level dimensions from content
        lines = level_content.rstrip("\n").split("\n")
        height = len(lines)
        width = max(len(line) for line in lines) if lines else 0

        # Create tiles list
        tiles: list[list[str]] = []
        for line in lines:
            row: list[str] = []
            for x in range(width):
                if x < len(line):
                    char = line[x]
                    # Convert spawn markers to floor
                    if char == "S":
                        char = "."
                else:
                    char = " "
                row.append(char)
            tiles.append(row)

        self.level = Level(width=width, height=height, tiles=tiles)
        self.room_width = width
        self.room_height = height
        self.current_level = target_level

        # Update position to spawn point (server will also send POSITION_ACK)
        self.x = spawn_x
        self.y = spawn_y

        # Clear pending moves since we're in a new level
        self._pending_moves.clear()
        self._needs_render = True

        # Parse and set doors for new level
        doors = parse_doors(level_pack.level_json_path)
        self.level.doors = doors

        # Parse and set streams for new level
        streams = parse_streams(level_pack.level_json_path)
        self.level.streams = streams

        # Parse and set interactions for new level
        interactions = parse_interactions(level_pack.level_json_path)
        self.level.interactions = interactions

        # Load other levels for see-through portals
        await self._load_see_through_portal_levels()

    async def _load_see_through_portal_levels(self) -> None:
        """Load other levels needed for see-through portals."""
        if not self.level or not self.level.doors:
            return

        # Collect unique cross-level targets from see-through portals
        target_levels: set[str] = set()
        for door in self.level.doors:
            if door.see_through and door.target_level:
                target_levels.add(door.target_level)

        # Load each target level
        for target_level_name in target_levels:
            if target_level_name in self.other_levels:
                continue  # Already loaded

            if not self._temp_dir:
                continue
            extract_dir = Path(self._temp_dir.name) / f"other_{target_level_name}"

            # Request level files using content-addressed caching
            level_pack = await self._request_level_cached(
                target_level_name, extract_dir
            )
            if level_pack is None:
                continue

            # Load the level from the pack
            with open(level_pack.level_path, encoding="utf-8") as f:
                level_content = f.read()

            lines = level_content.rstrip("\n").split("\n")
            height = len(lines)
            width = max(len(line) for line in lines) if lines else 0

            tiles: list[list[str]] = []
            for line in lines:
                row: list[str] = []
                for x in range(width):
                    if x < len(line):
                        char = line[x]
                        if char == "S":
                            char = "."
                    else:
                        char = " "
                    row.append(char)
                tiles.append(row)

            other_level = Level(width=width, height=height, tiles=tiles)

            # Parse doors for the other level too (for rendering tile chars)
            other_doors = parse_doors(level_pack.level_json_path)
            other_level.doors = other_doors

            self.other_levels[target_level_name] = other_level

    async def _handle_input(self, key: Keystroke) -> None:
        """Handle keyboard input."""
        if is_quit_key(key):
            self.running = False
            return

        if is_mute_key(key):
            await self._toggle_mute()
            return

        if is_show_names_key(key):
            self.show_player_names = not self.show_player_names
            self._needs_render = True
            return

        if is_player_table_key(key):
            self.show_player_table = not self.show_player_table
            self._needs_render = True
            return

        if is_help_key(key):
            self.show_help = not self.show_help
            self._needs_render = True
            return

        if is_log_key(key):
            self.show_logs = not self.show_logs
            if not self.show_logs:
                self._log_scroll_offset = 0  # Reset scroll when closing
                self._log_scroll_x = 0
            self._needs_render = True
            return

        # Handle scrolling in log view
        if self.show_logs and self.log_buffer:
            if key.name == "KEY_UP":
                max_offset = max(0, len(self.log_buffer.get_entries()) - 5)
                self._log_scroll_offset = min(self._log_scroll_offset + 1, max_offset)
                self._needs_render = True
                return
            elif key.name == "KEY_DOWN":
                self._log_scroll_offset = max(0, self._log_scroll_offset - 1)
                self._needs_render = True
                return
            elif key.name == "KEY_RIGHT":
                self._log_scroll_x += 20
                self._needs_render = True
                return
            elif key.name == "KEY_LEFT":
                self._log_scroll_x = max(0, self._log_scroll_x - 20)
                self._needs_render = True
                return

        # Handle interact key (space)
        if is_interact_key(key):
            # If popup is showing, skip animation or advance to next line
            if self._interact_lines is not None:
                current_text = self._interact_lines[self._interact_line_index]
                elapsed = time.monotonic() - self._interact_anim_start
                chars_shown = int(elapsed * self._interact_chars_per_sec)

                # If animation still running, skip to end
                if chars_shown < len(current_text):
                    self._interact_anim_start = 0.0  # Show full text
                    self._needs_render = True
                    return

                # Animation done - advance to next line or close
                if self._interact_line_index < len(self._interact_lines) - 1:
                    self._interact_line_index += 1
                    self._interact_anim_start = time.monotonic()
                else:
                    self._interact_lines = None
                    self._interact_line_index = 0
                self._needs_render = True
                return
            # Otherwise, start interact pending
            self._interact_pending_time = time.monotonic()
            return

        movement = get_movement(key)
        if movement and self.level:
            dx, dy = movement

            # If interact is pending, interact with tile in that direction
            if self._interact_pending_time is not None:
                self._interact_with_tile(self.x + dx, self.y + dy)
                self._interact_pending_time = None
                return

            # Normal movement
            if self._position_queue:
                # Rate limit movement (max 1 tile per tick interval)
                now = time.monotonic()
                if now - self._last_move_time < MOVEMENT_TICK_INTERVAL:
                    return  # Too fast, ignore this input

                new_x = self.x + dx
                new_y = self.y + dy
                # Client-side prediction: apply locally and track for reconciliation
                if self.level.is_walkable(new_x, new_y):
                    self._last_move_time = now
                    self._move_seq += 1
                    seq = self._move_seq
                    self._pending_moves[seq] = (dx, dy, new_x, new_y)
                    self.x = new_x
                    self.y = new_y
                    self._needs_render = True
                    # Queue position update (non-blocking)
                    self._position_queue.put_nowait((seq, new_x, new_y))
                    # Play walking sound for the new tile
                    self._tile_sound_player.on_player_move(new_x, new_y, self.level)

    def _interact_with_tile(self, x: int, y: int) -> None:
        """Interact with the tile at the given position."""
        if not self.level:
            return

        # Check for custom interaction at this position
        interaction = self.level.get_interaction_at(x, y)
        if interaction:
            self._interact_lines = interaction.text
        else:
            # Default interaction message
            tile_char = self.level.get_tile(x, y)
            tile_def = tile_defs.get_tile(tile_char)
            tile_name = tile_def.name or "unknown"
            self._interact_lines = [f"Just some {tile_name}. Nothing to see here."]

        self._interact_line_index = 0
        self._interact_anim_start = time.monotonic()
        self._needs_render = True

    async def _toggle_mute(self) -> None:
        """Toggle mute state."""
        self.is_muted = not self.is_muted
        self._needs_render = True
        # Send mute status via TCP
        await self._send_message(
            MessageType.MUTE_STATUS, serialize_mute_status(self.is_muted)
        )
        if self.audio_capture:
            self.audio_capture.set_muted(self.is_muted)

    def _render(self) -> None:
        """Render the current game state."""
        if not self.level:
            return

        # Update ambient tile sounds based on nearby tiles
        self._tile_sound_player.update_nearby_sounds(
            self.x, self.y, self.level, self.ui.has_line_of_sound
        )

        # Update audio streams based on nearby stream sources
        self._stream_player.update_streams(self.x, self.y, self.level)

        # Get mic level from audio capture
        if self.audio_capture:
            mic_level = self.audio_capture.last_level
        else:
            mic_level = 0.0

        # Calculate interaction text with typewriter animation
        interact_text = None
        interact_has_more = False
        if self._interact_lines:
            full_text = self._interact_lines[self._interact_line_index]
            elapsed = time.monotonic() - self._interact_anim_start
            chars_to_show = int(elapsed * self._interact_chars_per_sec)
            if chars_to_show >= len(full_text):
                interact_text = full_text
                # Only show triangle when animation is complete and more lines exist
                interact_has_more = (
                    self._interact_line_index < len(self._interact_lines) - 1
                )
            else:
                interact_text = full_text[:chars_to_show]

        self.ui.render(
            self.level,
            self.players,
            self.player_id,
            self.x,
            self.y,
            self.is_muted,
            mic_level,
            self.show_player_names,
            self.other_levels,
            self.current_level,
            self.show_player_table,
            self.show_help,
            interact_text,
            interact_has_more,
            self.show_logs,
            self.log_buffer,
            self._log_scroll_offset,
            self._log_scroll_x,
        )

    async def _start_audio(self) -> None:
        """Start audio capture and playback if available."""
        try:
            from .audio_capture import AudioCapture
            from .audio_playback import AudioPlayback

            # Start tile sounds (has its own "environment" output stream)
            self._tile_sound_player.start()

            # Start audio stream player (has its own "radio" output stream)
            self._stream_player.start()

            # Start voice playback (per-player streams)
            self.audio_playback = AudioPlayback()
            self.audio_playback.start()

            # Start capture - feed audio to LiveKit audio source
            self.audio_capture = AudioCapture(self._on_audio_frame)
            self.audio_capture.start()
        except ImportError:
            # Audio modules not available
            pass
        except Exception as e:
            _logger.error(f"Audio init failed: {e}")

    async def _stop_audio(self) -> None:
        """Stop audio capture and playback."""
        if self.audio_capture:
            self.audio_capture.stop()
        if self.audio_playback:
            self.audio_playback.stop()
        self._tile_sound_player.stop()
        self._stream_player.stop()

    async def _send_position_updates(self) -> None:
        """Send position updates from the queue to the server via TCP."""
        while self.running:
            try:
                if self._position_queue is None:
                    await asyncio.sleep(0.1)
                    continue
                seq, x, y = await asyncio.wait_for(
                    self._position_queue.get(), timeout=0.1
                )
                payload = serialize_position_update(seq, x, y)
                await self._send_message(MessageType.POSITION_UPDATE, payload)
            except asyncio.TimeoutError:
                continue

    def _on_audio_frame(self, pcm_data: Any, timestamp_ms: int) -> None:
        """Callback when audio frame is captured (called from audio thread).

        With LiveKit, we feed the raw PCM data to the LiveKit AudioSource
        which handles encoding and transmission.
        """
        if self.is_muted or not self._loop or not self.running:
            return

        # Feed audio to LiveKit audio source (thread-safe bridge)
        if self._livekit_audio_source:
            import numpy as np

            # Convert float32 PCM to int16 (LiveKit expects int16)
            if isinstance(pcm_data, np.ndarray):
                int16_data = (pcm_data * 32767).clip(-32768, 32767).astype(np.int16)
            else:
                int16_data = pcm_data

            frame = livekit_rtc.AudioFrame(
                data=int16_data.tobytes(),
                sample_rate=SAMPLE_RATE,
                num_channels=1,
                samples_per_channel=len(int16_data),
            )
            # capture_frame is async; bridge from audio thread to event loop
            audio_source = self._livekit_audio_source
            if audio_source is not None:
                self._loop.call_soon_threadsafe(
                    lambda f=frame: asyncio.ensure_future(  # type: ignore[misc]
                        audio_source.capture_frame(f)
                    )
                )
