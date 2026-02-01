#!/usr/bin/env python3
"""Example bot that wanders around and greets nearby players.

The bot:
- Moves randomly at 1 tile per second
- Greets players who come within range with hello.ogg
- Only greets each player at most once every 10 seconds

Usage:
    python examples/greeter_bot.py [--host HOST] [--port PORT] [--name NAME]
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import random
import time
from pathlib import Path

from rogue_talk.bot import BotClient, BotConfig, Direction, PlayerState, WorldState

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("greeter_bot")
# Silence noisy loggers
logging.getLogger("aiortc").setLevel(logging.WARNING)
logging.getLogger("aioice").setLevel(logging.WARNING)


# Path to the greeting audio file (relative to this script)
HELLO_AUDIO = Path(__file__).parent / "hello.ogg"

# Greeting cooldown per player (seconds)
GREETING_COOLDOWN = 10.0

# Movement interval (seconds)
MOVE_INTERVAL = 1.0


async def main() -> None:
    parser = argparse.ArgumentParser(description="Greeter bot for rogue-talk")
    parser.add_argument("--host", default="localhost", help="Server host")
    parser.add_argument("--port", type=int, default=7777, help="Server port")
    parser.add_argument("--name", default="GreeterBot", help="Bot display name")
    args = parser.parse_args()

    # Track last greeting time for each player
    last_greeted: dict[int, float] = {}

    # Create the bot
    bot = BotClient(name=args.name, config=BotConfig(audio_enabled=True))

    @bot.on_player_nearby
    async def on_player_nearby(player: PlayerState) -> None:
        """Called when a player enters audio range."""
        logger.info(f"Player {player.name} entered range at ({player.x}, {player.y})")
        now = time.time()
        last_time = last_greeted.get(player.player_id, 0)

        if now - last_time >= GREETING_COOLDOWN:
            logger.info(f"Greeting player: {player.name}")
            last_greeted[player.player_id] = now

            if HELLO_AUDIO.exists():
                logger.info(f"Playing audio file: {HELLO_AUDIO}")
                await bot.speak_file(HELLO_AUDIO)
                logger.info("Audio file queued")
            else:
                logger.warning(f"Audio file not found: {HELLO_AUDIO}")
        else:
            logger.debug(
                f"Skipping greeting for {player.name} (cooldown: {GREETING_COOLDOWN - (now - last_time):.1f}s remaining)"
            )

    @bot.on_world_state
    async def on_world_state(world: WorldState) -> None:
        """Called on world state updates."""
        pass  # Can be used to track world state if needed

    @bot.on_player_joined
    async def on_player_joined(player_id: int, name: str) -> None:
        """Called when a player joins the server."""
        logger.info(f"Player joined: {name} (ID: {player_id})")

    @bot.on_player_left
    async def on_player_left(player_id: int) -> None:
        """Called when a player leaves the server."""
        logger.info(f"Player left: ID {player_id}")
        # Clean up greeting cooldown for this player
        last_greeted.pop(player_id, None)

    # Connect to server
    logger.info(f"Connecting to {args.host}:{args.port} as {args.name}...")
    if not await bot.connect(args.host, args.port):
        logger.error("Failed to connect to server")
        return

    logger.info(f"Connected! Position: ({bot.x}, {bot.y})")

    # Start the movement task
    movement_task = asyncio.create_task(wander(bot))

    try:
        await bot.run()
    finally:
        movement_task.cancel()
        try:
            await movement_task
        except asyncio.CancelledError:
            pass


async def wander(bot: BotClient) -> None:
    """Wander around randomly, moving 1 tile per second."""
    directions = list(Direction)

    while True:
        await asyncio.sleep(MOVE_INTERVAL)

        # Pick a random direction
        direction = random.choice(directions)

        # Try to move
        if not await bot.move(direction):
            # If blocked, try other directions
            random.shuffle(directions)
            for alt_direction in directions:
                if await bot.move(alt_direction):
                    break


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Bot stopped by user")
