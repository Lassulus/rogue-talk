"""Main game server handling connections and game state."""

import asyncio
import io
import tarfile
from asyncio import StreamReader, StreamWriter
from pathlib import Path

from ..common.protocol import (
    AudioFrame,
    MessageType,
    PlayerInfo,
    deserialize_audio_frame,
    deserialize_client_hello,
    deserialize_level_pack_request,
    deserialize_mute_status,
    deserialize_position_update,
    read_message,
    serialize_audio_frame,
    serialize_level_pack_data,
    serialize_player_joined,
    serialize_player_left,
    serialize_position_ack,
    serialize_server_hello,
    serialize_world_state,
    write_message,
)
from .audio_router import get_audio_recipients
from .level import Level
from .player import Player
from .world import World


class GameServer:
    def __init__(self, host: str, port: int, levels_dir: str = "./levels"):
        self.host = host
        self.port = port
        self.levels_dir = Path(levels_dir)
        self.level_packs: dict[str, bytes] = {}  # name -> tarball bytes
        self._load_level_packs()
        # Load "main" level for the world
        self.level = self._extract_level_from_pack("main")
        self.world = World(self.level)
        self.players: dict[int, Player] = {}
        self.next_player_id = 1
        self._lock = asyncio.Lock()

    def _load_level_packs(self) -> None:
        """Load all .tar level packs from the levels directory."""
        if not self.levels_dir.exists():
            raise FileNotFoundError(
                f"Levels directory not found: {self.levels_dir}"
            )

        for tar_path in self.levels_dir.glob("*.tar"):
            name = tar_path.stem  # filename without .tar extension
            with open(tar_path, "rb") as f:
                self.level_packs[name] = f.read()
            print(f"Loaded level pack: {name}")

        if "main" not in self.level_packs:
            raise FileNotFoundError(
                f"Required level pack 'main.tar' not found in {self.levels_dir}"
            )

    def _extract_level_from_pack(self, name: str) -> Level:
        """Extract and load a Level from a level pack."""
        if name not in self.level_packs:
            raise ValueError(f"Level pack '{name}' not found")

        tarball_data = self.level_packs[name]
        with tarfile.open(fileobj=io.BytesIO(tarball_data), mode="r:*") as tar:
            # Find level.txt in the tarball
            for member in tar.getmembers():
                if member.name == "level.txt" or member.name.endswith("/level.txt"):
                    level_file = tar.extractfile(member)
                    if level_file:
                        content = level_file.read().decode("utf-8")
                        return Level.from_string(content)

        raise ValueError(f"level.txt not found in level pack '{name}'")

    async def start(self) -> None:
        server = await asyncio.start_server(
            self.handle_client, self.host, self.port, reuse_address=True
        )
        addr = server.sockets[0].getsockname()
        print(f"Server listening on {addr[0]}:{addr[1]}")
        async with server:
            await server.serve_forever()

    async def handle_client(self, reader: StreamReader, writer: StreamWriter) -> None:
        player: Player | None = None
        try:
            # First message should be LEVEL_PACK_REQUEST
            msg_type, payload = await read_message(reader)
            if msg_type == MessageType.LEVEL_PACK_REQUEST:
                level_name = deserialize_level_pack_request(payload)
                await self._handle_level_pack_request(writer, level_name)
                # Now wait for CLIENT_HELLO
                msg_type, payload = await read_message(reader)

            if msg_type != MessageType.CLIENT_HELLO:
                return

            name = deserialize_client_hello(payload)

            async with self._lock:
                player_id = self.next_player_id
                self.next_player_id += 1
                spawn_x, spawn_y = self.world.get_spawn_position()
                player = Player(player_id, name, spawn_x, spawn_y, reader, writer)
                self.players[player_id] = player

            # Send SERVER_HELLO with level data
            await write_message(
                writer,
                MessageType.SERVER_HELLO,
                serialize_server_hello(
                    player_id,
                    self.world.width,
                    self.world.height,
                    spawn_x,
                    spawn_y,
                    self.level.to_bytes(),
                ),
            )

            # Notify others about new player
            await self._broadcast_player_joined(player)

            # Send initial world state
            await self._send_world_state(player)

            print(f"Player {name} (id={player_id}) joined at ({spawn_x}, {spawn_y})")

            # Main message loop
            while True:
                msg_type, payload = await read_message(reader)
                await self._handle_message(player, msg_type, payload)

        except (asyncio.IncompleteReadError, ConnectionResetError, BrokenPipeError):
            pass
        finally:
            if player:
                async with self._lock:
                    self.players.pop(player.id, None)
                await self._broadcast_player_left(player.id)
                print(f"Player {player.name} (id={player.id}) left")
                writer.close()

    async def _handle_level_pack_request(
        self, writer: StreamWriter, level_name: str
    ) -> None:
        """Handle a LEVEL_PACK_REQUEST message."""
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

    async def _handle_message(
        self, player: Player, msg_type: MessageType, payload: bytes
    ) -> None:
        if msg_type == MessageType.POSITION_UPDATE:
            seq, x, y = deserialize_position_update(payload)
            # Validate the move (should be adjacent)
            dx = x - player.x
            dy = y - player.y
            if abs(dx) <= 1 and abs(dy) <= 1:
                if self.world.is_valid_position(x, y):
                    player.x = x
                    player.y = y
            # Always send ACK with authoritative position (even if move was rejected)
            try:
                await write_message(
                    player.writer,
                    MessageType.POSITION_ACK,
                    serialize_position_ack(seq, player.x, player.y),
                )
            except (ConnectionResetError, BrokenPipeError):
                pass
            await self._broadcast_world_state()

        elif msg_type == MessageType.AUDIO_FRAME:
            frame = deserialize_audio_frame(payload)
            frame.player_id = player.id  # Ensure correct source
            await self._route_audio(player, frame)

        elif msg_type == MessageType.MUTE_STATUS:
            player.is_muted = deserialize_mute_status(payload)
            await self._broadcast_world_state()

    async def _route_audio(self, source: Player, frame: AudioFrame) -> None:
        """Route audio frame to nearby players with volume scaling."""
        recipients = get_audio_recipients(source, self.players)
        for recipient, volume in recipients:
            routed_frame = AudioFrame(
                player_id=source.id,
                timestamp_ms=frame.timestamp_ms,
                volume=volume,
                opus_data=frame.opus_data,
            )
            try:
                await write_message(
                    recipient.writer,
                    MessageType.AUDIO_FRAME,
                    serialize_audio_frame(routed_frame),
                )
            except (ConnectionResetError, BrokenPipeError):
                pass

    async def _send_world_state(self, player: Player) -> None:
        """Send current world state to a specific player."""
        players_info = [
            PlayerInfo(p.id, p.x, p.y, p.is_muted, p.name)
            for p in self.players.values()
        ]
        await write_message(
            player.writer,
            MessageType.WORLD_STATE,
            serialize_world_state(players_info),
        )

    async def _broadcast_world_state(self) -> None:
        """Broadcast world state to all players."""
        players_info = [
            PlayerInfo(p.id, p.x, p.y, p.is_muted, p.name)
            for p in self.players.values()
        ]
        payload = serialize_world_state(players_info)
        for player in list(self.players.values()):
            try:
                await write_message(player.writer, MessageType.WORLD_STATE, payload)
            except (ConnectionResetError, BrokenPipeError):
                pass

    async def _broadcast_player_joined(self, new_player: Player) -> None:
        """Notify all other players about a new player."""
        payload = serialize_player_joined(new_player.id, new_player.name)
        for player in list(self.players.values()):
            if player.id != new_player.id:
                try:
                    await write_message(
                        player.writer, MessageType.PLAYER_JOINED, payload
                    )
                except (ConnectionResetError, BrokenPipeError):
                    pass

    async def _broadcast_player_left(self, player_id: int) -> None:
        """Notify all players that someone left."""
        payload = serialize_player_left(player_id)
        for player in list(self.players.values()):
            try:
                await write_message(player.writer, MessageType.PLAYER_LEFT, payload)
            except (ConnectionResetError, BrokenPipeError):
                pass
