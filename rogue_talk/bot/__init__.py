"""Bot SDK for creating automated clients.

Example usage:

    from rogue_talk.bot import BotClient, Direction

    async def main():
        bot = BotClient(name="GuardBot")
        await bot.connect("localhost", 7777)

        greeted = set()

        @bot.on_player_nearby
        async def on_nearby(player):
            if player.player_id not in greeted:
                greeted.add(player.player_id)
                await bot.speak_file("sounds/hello.wav")

        await bot.move(Direction.NORTH)
        await bot.move_to(10, 5)

        await bot.run()

    asyncio.run(main())
"""

from .audio import AudioSource, FileAudioSource, PCMAudioSource, SilenceSource
from .client import BotClient
from .pathfinding import find_path, find_path_with_custom_walkable
from .types import BotConfig, Direction, PlayerState, WorldState

__all__ = [
    "BotClient",
    "BotConfig",
    "Direction",
    "PlayerState",
    "WorldState",
    "AudioSource",
    "FileAudioSource",
    "PCMAudioSource",
    "SilenceSource",
    "find_path",
    "find_path_with_custom_walkable",
]
