"""Server entry point."""

import argparse
import asyncio
import logging

from ..common.constants import DEFAULT_HOST, DEFAULT_PORT
from .game_server import GameServer


def setup_logging(log_file: str) -> None:
    """Configure logging to file and console."""
    logging.basicConfig(
        level=logging.DEBUG,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        handlers=[
            logging.FileHandler(log_file),
            logging.StreamHandler(),
        ],
    )
    # Suppress noisy livekit debug logs
    logging.getLogger("livekit").setLevel(logging.WARNING)


def main() -> None:
    parser = argparse.ArgumentParser(description="Rogue-Talk Server")
    parser.add_argument("--host", default=DEFAULT_HOST, help="Host to bind to")
    parser.add_argument(
        "--port", type=int, default=DEFAULT_PORT, help="Port to bind to"
    )
    parser.add_argument(
        "--levels-dir",
        default="./levels",
        help="Directory containing level pack .tar files (default: ./levels)",
    )
    parser.add_argument(
        "--data-dir",
        default="./data",
        help="Directory for player data storage (default: ./data)",
    )
    parser.add_argument(
        "--log-file", default="rogue_talk_server.log", help="Log file path"
    )
    args = parser.parse_args()

    setup_logging(args.log_file)

    server = GameServer(
        args.host, args.port, levels_dir=args.levels_dir, data_dir=args.data_dir
    )
    try:
        asyncio.run(server.start())
    except KeyboardInterrupt:
        print("\nServer stopped")


if __name__ == "__main__":
    main()
