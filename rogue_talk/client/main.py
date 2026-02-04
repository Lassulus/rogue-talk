"""Client entry point."""

import argparse
import asyncio
import logging
import os

from ..common.constants import DEFAULT_HOST, DEFAULT_PORT
from .game_client import GameClient
from .log_buffer import LogBuffer


def setup_logging(log_file: str | None, log_buffer: LogBuffer) -> None:
    """Configure logging with in-memory buffer and optional file output."""
    root = logging.getLogger()
    root.setLevel(logging.DEBUG)

    # Always add the in-memory buffer for TUI display
    log_buffer.setLevel(logging.DEBUG)
    root.addHandler(log_buffer)

    # Optionally add file handler
    if log_file:
        file_handler = logging.FileHandler(log_file)
        file_handler.setFormatter(
            logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s")
        )
        root.addHandler(file_handler)

    # Suppress noisy LiveKit debug logs
    logging.getLogger("livekit").setLevel(logging.WARNING)


def main() -> None:
    parser = argparse.ArgumentParser(description="Rogue-Talk Client")
    parser.add_argument("--host", default=DEFAULT_HOST, help="Server host")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT, help="Server port")
    parser.add_argument(
        "--name", default=os.environ.get("USER", "player"), help="Player name"
    )
    parser.add_argument(
        "--log", help="Log file path (in addition to in-memory log buffer)"
    )
    args = parser.parse_args()

    # Create log buffer for TUI display
    log_buffer = LogBuffer(maxlen=200)
    setup_logging(args.log, log_buffer)

    client = GameClient(args.host, args.port, args.name)
    client.log_buffer = log_buffer

    async def run_client() -> None:
        if await client.connect():
            await client.run()
        else:
            print("Failed to connect to server")

    try:
        asyncio.run(run_client())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
