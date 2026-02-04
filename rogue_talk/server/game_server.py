"""Main game server handling connections and game state."""

from __future__ import annotations

import asyncio
import hashlib
import io
import json
import logging
import os
import struct
import tarfile
import time
from asyncio import StreamReader, StreamWriter
from pathlib import Path
from typing import Any

from livekit import api as livekit_api

logger = logging.getLogger(__name__)
logging.basicConfig(
    format="%(asctime)s %(name)s %(message)s",
    level=logging.DEBUG,
)

from ..common import tiles as tile_defs
from ..common.crypto import verify_signature
from ..common.protocol import (
    AuthResult,
    MessageType,
    PlayerInfo,
    deserialize_auth_response,
    deserialize_level_files_request,
    deserialize_level_manifest_request,
    deserialize_level_pack_request,
    deserialize_mute_status,
    deserialize_position_update,
    read_message,
    serialize_auth_challenge,
    serialize_auth_result,
    serialize_door_transition,
    serialize_level_files_data,
    serialize_level_manifest,
    serialize_level_pack_data,
    serialize_livekit_token,
    serialize_player_joined,
    serialize_player_left,
    serialize_position_ack,
    serialize_server_hello,
    serialize_world_state,
    write_message,
)
from .audio_router import clear_recipient_cache
from .level import DoorInfo, Level, StreamInfo
from .player import Player
from .storage import PlayerStorage
from .world import World

# Ping/keepalive settings
PING_INTERVAL = 10.0  # Send ping every 10 seconds
PING_TIMEOUT = 30.0  # Disconnect if no pong within 30 seconds

# Subscription management interval (how often to update LiveKit subscriptions)
SUBSCRIPTION_INTERVAL = 0.5  # 500ms

# LiveKit configuration (can be overridden via environment variables)
LIVEKIT_URL = os.environ.get("LIVEKIT_URL", "ws://localhost:7880")
LIVEKIT_ROOM_NAME = os.environ.get("LIVEKIT_ROOM_NAME", "rogue-talk")


def _read_secret_file(env_var: str, default: str) -> str:
    """Read a secret from a file path given by an environment variable."""
    path = os.environ.get(env_var)
    if path is None:
        return default
    return Path(path).read_text().strip()


LIVEKIT_API_KEY = _read_secret_file("LIVEKIT_API_KEYFILE", "devkey")
LIVEKIT_API_SECRET = _read_secret_file("LIVEKIT_API_SECRETFILE", "secret")


class GameServer:
    def __init__(
        self,
        host: str,
        port: int,
        levels_dir: str = "./levels",
        data_dir: str = "./data",
    ):
        self.host = host
        self.port = port
        self.levels_dir = Path(levels_dir)
        self.data_dir = Path(data_dir)
        self.storage = PlayerStorage(self.data_dir)
        self.level_packs: dict[str, bytes] = {}  # name -> tarball bytes
        self.levels: dict[str, Level] = {}  # name -> parsed Level object
        self.level_tiles: dict[
            str, dict[str, tile_defs.TileDef]
        ] = {}  # name -> tile definitions
        # Content-addressed caching: manifest and raw file contents per level
        self.level_manifests: dict[str, dict[str, tuple[str, int]]] = {}
        self.level_file_contents: dict[str, dict[str, bytes]] = {}
        self._load_level_packs()
        # Load "main" level for the world (for backwards compatibility)
        self.level = self.levels["main"]
        self.world = World(self.level)
        self.players: dict[int, Player] = {}
        self.next_player_id = 1
        self._lock = asyncio.Lock()

        # LiveKit API client for managing subscriptions
        self._livekit_api: livekit_api.LiveKitAPI | None = None

    def _load_level_packs(self) -> None:
        """Load all level packs from subdirectories in the levels directory."""
        if not self.levels_dir.exists():
            raise FileNotFoundError(f"Levels directory not found: {self.levels_dir}")

        for folder_path in self.levels_dir.iterdir():
            if not folder_path.is_dir():
                continue
            name = folder_path.name
            self.level_packs[name] = self._create_tarball_from_folder(folder_path)

            # Compute manifest and store file contents for caching
            manifest, contents = self._compute_level_manifest(name, folder_path)
            self.level_manifests[name] = manifest
            self.level_file_contents[name] = contents

            # Parse the level and its tiles
            level, tiles = self._parse_level_pack(name)
            self.levels[name] = level
            self.level_tiles[name] = tiles
            # Count door tiles
            door_count = sum(1 for t in tiles.values() if t.is_door)
            total_size = sum(size for _, size in manifest.values())
            print(
                f"Loaded level pack: {name} ({level.width}x{level.height}, {door_count} door tiles, {len(manifest)} files, {total_size // 1024}KB)"
            )

        if "main" not in self.level_packs:
            raise FileNotFoundError(
                f"Required level folder 'main/' not found in {self.levels_dir}"
            )

    def _create_tarball_from_folder(self, folder_path: Path) -> bytes:
        """Create a tarball in memory from a level folder."""
        buffer = io.BytesIO()
        with tarfile.open(fileobj=buffer, mode="w") as tar:
            for file_path in folder_path.rglob("*"):
                if file_path.is_file():
                    arcname = file_path.relative_to(folder_path)
                    tar.add(file_path, arcname=str(arcname))
        return buffer.getvalue()

    def _compute_level_manifest(
        self, level_name: str, folder_path: Path
    ) -> tuple[dict[str, tuple[str, int]], dict[str, bytes]]:
        """Compute SHA256 hash and size for each file in level, return manifest and contents."""
        manifest: dict[str, tuple[str, int]] = {}
        contents: dict[str, bytes] = {}
        for file_path in folder_path.rglob("*"):
            if file_path.is_file():
                content = file_path.read_bytes()
                hash_hex = hashlib.sha256(content).hexdigest()
                rel_path = str(file_path.relative_to(folder_path))
                manifest[rel_path] = (hash_hex, len(content))
                contents[rel_path] = content
        return manifest, contents

    def _parse_level_pack(
        self, name: str
    ) -> tuple[Level, dict[str, tile_defs.TileDef]]:
        """Parse a level pack and return Level and tile definitions."""
        if name not in self.level_packs:
            raise ValueError(f"Level pack '{name}' not found")

        tarball_data = self.level_packs[name]
        level_content: str | None = None
        tiles_data: dict[str, object] | None = None
        level_json_data: dict[str, object] | None = None

        with tarfile.open(fileobj=io.BytesIO(tarball_data), mode="r:*") as tar:
            for member in tar.getmembers():
                if member.name == "level.txt" or member.name.endswith("/level.txt"):
                    level_file = tar.extractfile(member)
                    if level_file:
                        level_content = level_file.read().decode("utf-8")
                elif member.name == "tiles.json" or member.name.endswith("/tiles.json"):
                    tiles_file = tar.extractfile(member)
                    if tiles_file:
                        tiles_data = json.load(tiles_file)
                elif member.name == "level.json" or member.name.endswith("/level.json"):
                    level_json_file = tar.extractfile(member)
                    if level_json_file:
                        level_json_data = json.load(level_json_file)

        if level_content is None:
            raise ValueError(f"level.txt not found in level pack '{name}'")

        # Parse tile definitions (use defaults if not in pack)
        if tiles_data:
            tiles = self._parse_tiles_json(tiles_data)
        else:
            tiles = dict(tile_defs.TILES)

        # Parse level (pass tiles to detect spawn points)
        level = Level.from_string(level_content, tiles)

        # Parse doors from level.json
        if level_json_data:
            self._parse_level_json(level, level_json_data)

        # Validate level consistency
        self._validate_level(name, level, tiles)

        return level, tiles

    def _validate_level(
        self, level_name: str, level: Level, tiles: dict[str, tile_defs.TileDef]
    ) -> None:
        """Validate level consistency.

        Logs warnings for:
        - Tiles used in level.txt that aren't defined in tiles.json
        - Doors/teleporters in level.json at positions without is_door tiles
        - Tiles with is_door=true that have no level.json entry
        - Same-level teleporters with invalid target positions
        """
        # Check for undefined tiles
        undefined_tiles: dict[str, list[tuple[int, int]]] = {}
        for y in range(level.height):
            for x in range(level.width):
                tile_char = level.get_tile(x, y)
                if tile_char not in tiles:
                    if tile_char not in undefined_tiles:
                        undefined_tiles[tile_char] = []
                    if len(undefined_tiles[tile_char]) < 3:  # Limit examples
                        undefined_tiles[tile_char].append((x, y))

        for tile_char, positions in undefined_tiles.items():
            pos_str = ", ".join(f"({x},{y})" for x, y in positions)
            print(
                f"WARNING: {level_name}: Tile '{tile_char}' (ord={ord(tile_char)}) "
                f"not defined in tiles.json (e.g. at {pos_str})"
            )
        # Check doors defined in level.json
        for pos, door in level.doors.items():
            x, y = pos
            tile_char = level.get_tile(x, y)
            tile_def = tiles.get(tile_char, tile_defs.DEFAULT_TILE)
            if not tile_def.is_door:
                target = door.target_level or "same level"
                print(
                    f"WARNING: {level_name}: Door at ({x}, {y}) -> {target} "
                    f"has tile '{tile_char}' without is_door=true (teleporter won't work!)"
                )

            # For same-level teleporters, validate target position
            if door.target_level is None:
                tx, ty = door.target_x, door.target_y
                if tx < 0 or tx >= level.width or ty < 0 or ty >= level.height:
                    print(
                        f"WARNING: {level_name}: Teleporter at ({x}, {y}) "
                        f"has target ({tx}, {ty}) outside level bounds!"
                    )
                else:
                    # Use level-specific tiles for walkability check
                    target_tile = level.get_tile(tx, ty)
                    target_tile_def = tiles.get(target_tile, tile_defs.DEFAULT_TILE)
                    if not target_tile_def.walkable:
                        print(
                            f"WARNING: {level_name}: Teleporter at ({x}, {y}) "
                            f"has non-walkable target ({tx}, {ty}) tile '{target_tile}'!"
                        )

        # Check for orphaned door tiles
        for y in range(level.height):
            for x in range(level.width):
                tile_char = level.get_tile(x, y)
                tile_def = tiles.get(tile_char, tile_defs.DEFAULT_TILE)
                if tile_def.is_door and (x, y) not in level.doors:
                    print(
                        f"WARNING: {level_name}: Door tile '{tile_char}' at ({x}, {y}) "
                        f"has no entry in level.json (no destination!)"
                    )

    def _parse_tiles_json(
        self, data: dict[str, object]
    ) -> dict[str, tile_defs.TileDef]:
        """Parse tiles.json data into TileDef objects."""
        tiles: dict[str, tile_defs.TileDef] = {}
        tiles_data = data.get("tiles", {})
        if not isinstance(tiles_data, dict):
            return tiles
        for char, tile_data in tiles_data.items():
            if not isinstance(tile_data, dict):
                continue
            tiles[str(char)] = tile_defs.TileDef(
                char=str(char),
                walkable=bool(tile_data["walkable"]),
                color=str(tile_data["color"]),
                name=str(tile_data.get("name", "")),
                walking_sound=str(tile_data["walking_sound"])
                if tile_data.get("walking_sound")
                else None,
                nearby_sound=str(tile_data["nearby_sound"])
                if tile_data.get("nearby_sound")
                else None,
                animation_colors=list(tile_data.get("animation_colors") or []),
                blocks_sight=bool(tile_data["blocks_sight"])
                if tile_data.get("blocks_sight") is not None
                else None,
                blocks_sound=bool(tile_data["blocks_sound"])
                if tile_data.get("blocks_sound") is not None
                else None,
                is_door=bool(tile_data.get("is_door", False)),
                is_spawn=bool(tile_data.get("is_spawn", False)),
                render_char=str(tile_data["render_char"])
                if tile_data.get("render_char")
                else None,
            )
        return tiles

    def _parse_level_json(self, level: Level, data: dict[str, object]) -> None:
        """Parse level.json data and populate Level with door and stream metadata."""
        # Parse doors from level.json
        doors_data = data.get("doors", [])
        if isinstance(doors_data, list):
            for door_data in doors_data:
                if not isinstance(door_data, dict):
                    continue
                x = int(door_data["x"])
                y = int(door_data["y"])
                target_level = door_data.get("target_level")
                door_info = DoorInfo(
                    x=x,
                    y=y,
                    target_level=str(target_level) if target_level else None,
                    target_x=int(door_data["target_x"]),
                    target_y=int(door_data["target_y"]),
                )
                level.doors[(x, y)] = door_info

        # Parse streams from level.json
        streams_data = data.get("streams", [])
        if isinstance(streams_data, list):
            for stream_data in streams_data:
                if not isinstance(stream_data, dict):
                    continue
                x = int(stream_data["x"])
                y = int(stream_data["y"])
                url = str(stream_data["url"])
                radius = int(stream_data.get("radius", 5))
                stream_info = StreamInfo(
                    x=x,
                    y=y,
                    url=url,
                    radius=radius,
                )
                level.streams[(x, y)] = stream_info

    def _generate_livekit_token(self, player: Player) -> str:
        """Generate a LiveKit access token for a player."""
        token = (
            livekit_api.AccessToken(LIVEKIT_API_KEY, LIVEKIT_API_SECRET)
            .with_identity(player.livekit_identity)
            .with_name(player.name)
            .with_grants(
                livekit_api.VideoGrants(
                    room_join=True,
                    room=LIVEKIT_ROOM_NAME,
                    can_publish=True,
                    can_subscribe=True,
                )
            )
        )
        result: str = token.to_jwt()
        return result

    async def start(self) -> None:
        # Initialize LiveKit API client
        self._livekit_api = livekit_api.LiveKitAPI(
            LIVEKIT_URL,
            api_key=LIVEKIT_API_KEY,
            api_secret=LIVEKIT_API_SECRET,
        )

        server = await asyncio.start_server(
            self.handle_client, self.host, self.port, reuse_address=True
        )
        addr = server.sockets[0].getsockname()
        print(f"Server listening on {addr[0]}:{addr[1]}")
        print(f"LiveKit URL: {LIVEKIT_URL}, Room: {LIVEKIT_ROOM_NAME}")

        # Start subscription management task
        subscription_task = asyncio.create_task(self._subscription_management_loop())

        try:
            async with server:
                await server.serve_forever()
        finally:
            subscription_task.cancel()
            try:
                await subscription_task
            except asyncio.CancelledError:
                pass
            if self._livekit_api:
                await self._livekit_api.aclose()

    async def _subscription_management_loop(self) -> None:
        """Placeholder for future server-side subscription management.

        Currently, LiveKit auto-subscribe handles all track subscriptions.
        The client applies proximity-based volume scaling locally.
        If bandwidth becomes an issue with many players, this loop can be
        extended to use LiveKit's update_subscriptions API with actual
        track SIDs to limit audio streams to nearby players only.
        """
        while True:
            await asyncio.sleep(SUBSCRIPTION_INTERVAL)

    async def handle_client(self, reader: StreamReader, writer: StreamWriter) -> None:
        player: Player | None = None
        connection_closed = asyncio.Event()
        try:
            # Send AUTH_CHALLENGE with random nonce
            nonce = os.urandom(32)
            await write_message(
                writer, MessageType.AUTH_CHALLENGE, serialize_auth_challenge(nonce)
            )

            # Wait for AUTH_RESPONSE
            msg_type, payload = await read_message(reader)
            if msg_type != MessageType.AUTH_RESPONSE:
                return

            public_key, name, signature = deserialize_auth_response(payload)

            # Validate name
            if not name or len(name) > 32 or not name.isprintable():
                await write_message(
                    writer,
                    MessageType.AUTH_RESULT,
                    serialize_auth_result(AuthResult.INVALID_NAME),
                )
                return

            # Verify signature
            if not verify_signature(public_key, nonce, name, signature):
                await write_message(
                    writer,
                    MessageType.AUTH_RESULT,
                    serialize_auth_result(AuthResult.INVALID_SIGNATURE),
                )
                return

            # Check registration status
            existing_key = self.storage.get_public_key(name)
            existing_name = self.storage.get_name_by_key(public_key)

            if existing_key is not None:
                # Name is registered
                if existing_key != public_key:
                    # Name taken by different key
                    await write_message(
                        writer,
                        MessageType.AUTH_RESULT,
                        serialize_auth_result(AuthResult.NAME_TAKEN),
                    )
                    return
                # Key matches, returning player
            elif existing_name is not None:
                # Key is registered with different name
                await write_message(
                    writer,
                    MessageType.AUTH_RESULT,
                    serialize_auth_result(AuthResult.KEY_MISMATCH),
                )
                return
            else:
                # New player, register them
                if not self.storage.register_player(name, public_key):
                    await write_message(
                        writer,
                        MessageType.AUTH_RESULT,
                        serialize_auth_result(AuthResult.NAME_TAKEN),
                    )
                    return

            # Check if player is already connected
            async with self._lock:
                for p in self.players.values():
                    if p.public_key == public_key:
                        await write_message(
                            writer,
                            MessageType.AUTH_RESULT,
                            serialize_auth_result(AuthResult.ALREADY_CONNECTED),
                        )
                        return

            # Auth successful
            await write_message(
                writer,
                MessageType.AUTH_RESULT,
                serialize_auth_result(AuthResult.SUCCESS),
            )

            # Get spawn position (use saved state if returning player)
            saved_state = self.storage.get_player_state(name)
            if saved_state:
                spawn_x = saved_state.x
                spawn_y = saved_state.y
                current_level = saved_state.level
            else:
                spawn_x, spawn_y = self.world.get_spawn_position()
                current_level = "main"

            async with self._lock:
                player_id = self.next_player_id
                self.next_player_id += 1
                player = Player(
                    player_id,
                    name,
                    spawn_x,
                    spawn_y,
                    reader,
                    writer,
                    livekit_identity=name,
                    current_level=current_level,
                    public_key=public_key,
                )
                self.players[player_id] = player

            # Get the level for the player
            player_level = self.levels.get(current_level, self.level)

            # Send SERVER_HELLO with level data
            await write_message(
                writer,
                MessageType.SERVER_HELLO,
                serialize_server_hello(
                    player_id,
                    player_level.width,
                    player_level.height,
                    spawn_x,
                    spawn_y,
                    player_level.to_bytes(),
                    current_level,
                ),
            )

            returning = " (returning)" if saved_state else ""
            print(
                f"Player {name} (id={player_id}) joined at ({spawn_x}, {spawn_y}){returning}"
            )

            # Generate and send LiveKit token immediately after SERVER_HELLO
            # (client requests level files concurrently and ignores non-level messages)
            livekit_token = self._generate_livekit_token(player)
            await write_message(
                writer,
                MessageType.LIVEKIT_TOKEN,
                serialize_livekit_token(LIVEKIT_URL, livekit_token),
            )

            # Notify others about new player
            await self._broadcast_player_joined(player)

            # Broadcast world state to all players
            await self._broadcast_world_state()

            # Clear audio recipient cache so new player is included
            clear_recipient_cache()

            # Start ping loop
            ping_task = asyncio.create_task(self._ping_loop(player, connection_closed))

            # Main TCP message loop (handles level requests, position updates, etc.)
            try:
                await self._message_loop(player, reader, connection_closed)
            finally:
                ping_task.cancel()
                try:
                    await ping_task
                except asyncio.CancelledError:
                    pass

        except (
            asyncio.IncompleteReadError,
            ConnectionResetError,
            BrokenPipeError,
            TimeoutError,
            OSError,
        ):
            pass  # Client disconnected
        except Exception as e:
            # Log unexpected exceptions
            logger.error(
                f"Unexpected error for {player.name if player else 'unknown'}: "
                f"{type(e).__name__}: {e}",
                exc_info=True,
            )
        finally:
            if player:
                # Save player state before removing
                self.storage.save_player_state(
                    player.name, player.x, player.y, player.current_level
                )

                async with self._lock:
                    self.players.pop(player.id, None)
                clear_recipient_cache()  # Invalidate audio routing cache
                await self._broadcast_player_left(player.id)
                print(f"Player {player.name} (id={player.id}) left")

                # Close TCP if still open
                if player.writer:
                    player.writer.close()
                    try:
                        await player.writer.wait_closed()
                    except Exception:
                        pass

    async def _send_to_player(
        self, player: Player, msg_type: MessageType, payload: bytes
    ) -> None:
        """Send a message to a player via TCP."""
        if player.writer is None:
            return
        try:
            await write_message(player.writer, msg_type, payload)
        except (ConnectionResetError, BrokenPipeError, OSError):
            pass

    async def _message_loop(
        self,
        player: Player,
        reader: StreamReader,
        connection_closed: asyncio.Event,
    ) -> None:
        """Main message loop for a player over TCP."""
        try:
            while not connection_closed.is_set():
                msg_type, payload = await read_message(reader)
                await self._handle_message(player, msg_type, payload)
        except (
            asyncio.IncompleteReadError,
            ConnectionResetError,
            BrokenPipeError,
            OSError,
        ):
            pass  # Client disconnected

    async def _handle_level_pack_request(
        self, writer: StreamWriter, level_name: str
    ) -> None:
        """Handle a LEVEL_PACK_REQUEST message over TCP."""
        if level_name in self.level_packs:
            tarball = self.level_packs[level_name]
            print(f"Sending level pack: {level_name} ({len(tarball)} bytes)")
        else:
            # Level not found - send empty response
            tarball = b""
            print(f"Level pack not found: {level_name}")

        await write_message(
            writer,
            MessageType.LEVEL_PACK_DATA,
            serialize_level_pack_data(tarball),
        )

    async def _handle_level_manifest_request(
        self, writer: StreamWriter, level_name: str
    ) -> None:
        """Handle a LEVEL_MANIFEST_REQUEST message over TCP."""
        if level_name in self.level_manifests:
            manifest = self.level_manifests[level_name]
            print(f"Sending manifest: {level_name} ({len(manifest)} files)")
        else:
            manifest = {}
            print(f"Level manifest not found: {level_name}")

        await write_message(
            writer,
            MessageType.LEVEL_MANIFEST,
            serialize_level_manifest(manifest),
        )

    async def _handle_level_files_request(
        self, writer: StreamWriter, level_name: str, filenames: list[str]
    ) -> None:
        """Handle a LEVEL_FILES_REQUEST message over TCP."""
        files: dict[str, bytes] = {}
        if level_name in self.level_file_contents:
            level_contents = self.level_file_contents[level_name]
            for filename in filenames:
                if filename in level_contents:
                    files[filename] = level_contents[filename]
        total_size = sum(len(c) for c in files.values())
        print(f"Sending {len(files)} files for {level_name} ({total_size} bytes)")

        await write_message(
            writer,
            MessageType.LEVEL_FILES_DATA,
            serialize_level_files_data(files),
        )

    async def _ping_loop(
        self, player: Player, connection_closed: asyncio.Event
    ) -> None:
        """Send periodic pings to check if client is alive and measure RTT."""
        while True:
            await asyncio.sleep(PING_INTERVAL)

            # Check if client responded to recent pings
            time_since_pong = time.monotonic() - player.last_pong_time
            if time_since_pong > PING_TIMEOUT:
                print(
                    f"Player {player.name} timed out (no pong for {time_since_pong:.1f}s)"
                )
                connection_closed.set()
                return

            # Record time before sending ping (for RTT measurement)
            player.last_ping_sent_time = time.monotonic()

            # Send ping via TCP
            await self._send_to_player(player, MessageType.PING, b"")

    async def _handle_door_transition(
        self, player: Player, door_info: DoorInfo, seq: int
    ) -> None:
        """Handle a player stepping on a door/teleporter tile."""
        target_x = door_info.target_x
        target_y = door_info.target_y

        # Target level (None means same level = teleporter)
        target_level_name = door_info.target_level or player.current_level
        is_same_level = target_level_name == player.current_level

        # Check if target level exists (if switching levels)
        if not is_same_level and target_level_name not in self.levels:
            print(f"Door transition failed: level '{target_level_name}' not found")
            # Send ACK at current position (transition failed)
            await self._send_to_player(
                player,
                MessageType.POSITION_ACK,
                serialize_position_ack(seq, player.x, player.y),
            )
            return

        if is_same_level:
            # Teleporter within same level - just update position
            print(f"Player {player.name} teleporting to ({target_x}, {target_y})")
            player.x = target_x
            player.y = target_y

            # Send position ACK with new position
            await self._send_to_player(
                player,
                MessageType.POSITION_ACK,
                serialize_position_ack(seq, player.x, player.y),
            )
        else:
            # Door to different level
            print(
                f"Player {player.name} entering door -> level '{target_level_name}' at ({target_x}, {target_y})"
            )

            # Send DOOR_TRANSITION message to client
            await self._send_to_player(
                player,
                MessageType.DOOR_TRANSITION,
                serialize_door_transition(target_level_name, target_x, target_y),
            )

            # Update player's level and position
            player.current_level = target_level_name
            player.x = target_x
            player.y = target_y

            # Send position ACK with new position
            await self._send_to_player(
                player,
                MessageType.POSITION_ACK,
                serialize_position_ack(seq, player.x, player.y),
            )

        await self._broadcast_world_state()

    async def _handle_message(
        self, player: Player, msg_type: MessageType, payload: bytes
    ) -> None:
        if msg_type == MessageType.POSITION_UPDATE:
            seq, x, y = deserialize_position_update(payload)
            # Validate the move (should be adjacent)
            dx = x - player.x
            dy = y - player.y

            # Get player's current level
            current_level = self.levels.get(player.current_level, self.level)
            current_tiles = self.level_tiles.get(player.current_level, tile_defs.TILES)

            # Movement speed is rate-limited client-side; server only validates adjacency
            if abs(dx) <= 1 and abs(dy) <= 1:
                # Check if position is valid and walkable using level-specific tiles
                if 0 <= x < current_level.width and 0 <= y < current_level.height:
                    tile_char = current_level.get_tile(x, y)
                    tile_def = current_tiles.get(tile_char, tile_defs.DEFAULT_TILE)
                    if tile_def.walkable:
                        player.x = x
                        player.y = y

                        # Check if player stepped on a door/teleporter
                        if tile_def.is_door:
                            door_info = current_level.get_door_at(x, y)
                            if door_info:
                                await self._handle_door_transition(
                                    player, door_info, seq
                                )
                                return  # Door transition handles ACK differently

            # Always send ACK with authoritative position (even if move was rejected)
            await self._send_to_player(
                player,
                MessageType.POSITION_ACK,
                serialize_position_ack(seq, player.x, player.y),
            )
            await self._broadcast_world_state()

        elif msg_type == MessageType.LEVEL_PACK_REQUEST:
            level_name = deserialize_level_pack_request(payload)
            await self._send_to_player(
                player,
                MessageType.LEVEL_PACK_DATA,
                serialize_level_pack_data(self.level_packs.get(level_name, b"")),
            )

        elif msg_type == MessageType.LEVEL_MANIFEST_REQUEST:
            level_name = deserialize_level_manifest_request(payload)
            manifest = self.level_manifests.get(level_name, {})
            await self._send_to_player(
                player,
                MessageType.LEVEL_MANIFEST,
                serialize_level_manifest(manifest),
            )

        elif msg_type == MessageType.LEVEL_FILES_REQUEST:
            level_name, filenames = deserialize_level_files_request(payload)
            files: dict[str, bytes] = {}
            if level_name in self.level_file_contents:
                level_contents = self.level_file_contents[level_name]
                for filename in filenames:
                    if filename in level_contents:
                        files[filename] = level_contents[filename]
            await self._send_to_player(
                player,
                MessageType.LEVEL_FILES_DATA,
                serialize_level_files_data(files),
            )

        elif msg_type == MessageType.MUTE_STATUS:
            player.is_muted = deserialize_mute_status(payload)
            await self._broadcast_world_state()

        elif msg_type == MessageType.PONG:
            now = time.monotonic()
            player.last_pong_time = now
            # Calculate RTT if we have a valid ping send time
            if player.last_ping_sent_time > 0:
                rtt_seconds = now - player.last_ping_sent_time
                player.ping_ms = int(rtt_seconds * 1000)

    async def _send_world_state(self, player: Player) -> None:
        """Send current world state to a specific player."""
        players_info = [
            PlayerInfo(p.id, p.x, p.y, p.is_muted, p.name, p.current_level, p.ping_ms)
            for p in self.players.values()
        ]
        await self._send_to_player(
            player,
            MessageType.WORLD_STATE,
            serialize_world_state(players_info),
        )

    async def _broadcast_world_state(self) -> None:
        """Broadcast world state to all players."""
        players_info = [
            PlayerInfo(p.id, p.x, p.y, p.is_muted, p.name, p.current_level, p.ping_ms)
            for p in self.players.values()
        ]
        payload = serialize_world_state(players_info)
        for player in list(self.players.values()):
            await self._send_to_player(player, MessageType.WORLD_STATE, payload)

    async def _broadcast_player_joined(self, new_player: Player) -> None:
        """Notify all other players about a new player."""
        payload = serialize_player_joined(new_player.id, new_player.name)
        for player in list(self.players.values()):
            if player.id != new_player.id:
                await self._send_to_player(player, MessageType.PLAYER_JOINED, payload)

    async def _broadcast_player_left(self, player_id: int) -> None:
        """Notify all players that someone left."""
        payload = serialize_player_left(player_id)
        for player in list(self.players.values()):
            await self._send_to_player(player, MessageType.PLAYER_LEFT, payload)
