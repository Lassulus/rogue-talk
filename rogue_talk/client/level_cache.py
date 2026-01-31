"""Content-addressed level file caching."""

from pathlib import Path

CACHE_DIR = Path.home() / ".rogue-talk" / "level_cache"


def get_cached_file(level: str, expected_hash: str) -> bytes | None:
    """Return cached file content if hash matches, else None."""
    path = CACHE_DIR / level / expected_hash
    if path.exists():
        return path.read_bytes()
    return None


def cache_file(level: str, file_hash: str, content: bytes) -> None:
    """Store file in cache, named by hash."""
    path = CACHE_DIR / level / file_hash
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)


def get_cached_files(
    level: str, manifest: dict[str, tuple[str, int]]
) -> tuple[dict[str, bytes], list[str]]:
    """Check cache for all files in manifest.

    Returns:
        Tuple of (cached_files dict, list of missing filenames)
    """
    cached: dict[str, bytes] = {}
    missing: list[str] = []

    for filename, (file_hash, _size) in manifest.items():
        content = get_cached_file(level, file_hash)
        if content is not None:
            cached[filename] = content
        else:
            missing.append(filename)

    return cached, missing


def cache_received_files(
    level: str,
    manifest: dict[str, tuple[str, int]],
    files: dict[str, bytes],
) -> None:
    """Cache received files using their hashes from the manifest."""
    for filename, content in files.items():
        if filename in manifest:
            file_hash, _ = manifest[filename]
            cache_file(level, file_hash, content)
