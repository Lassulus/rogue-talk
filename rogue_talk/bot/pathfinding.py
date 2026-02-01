"""A* pathfinding for bot navigation."""

from __future__ import annotations

import heapq
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Callable

if TYPE_CHECKING:
    from ..client.level import Level


@dataclass(order=True)
class _Node:
    """A node in the pathfinding priority queue."""

    f_score: float
    position: tuple[int, int] = field(compare=False)
    g_score: float = field(compare=False)


def _heuristic(a: tuple[int, int], b: tuple[int, int]) -> float:
    """Calculate heuristic (Chebyshev distance for 8-directional movement)."""
    dx = abs(a[0] - b[0])
    dy = abs(a[1] - b[1])
    # Chebyshev distance: max of dx, dy (since diagonal moves cost same as cardinal)
    return max(dx, dy)


def _get_neighbors(x: int, y: int) -> list[tuple[int, int]]:
    """Get all 8 neighboring positions."""
    return [
        (x + 1, y),
        (x - 1, y),
        (x, y + 1),
        (x, y - 1),
        (x + 1, y + 1),
        (x + 1, y - 1),
        (x - 1, y + 1),
        (x - 1, y - 1),
    ]


def find_path(
    start: tuple[int, int],
    goal: tuple[int, int],
    level: Level,
    max_iterations: int = 10000,
) -> list[tuple[int, int]] | None:
    """Find a path from start to goal using A* algorithm.

    Args:
        start: Starting position (x, y).
        goal: Target position (x, y).
        level: Level data for walkability checks.
        max_iterations: Maximum iterations before giving up.

    Returns:
        List of positions from start to goal (inclusive), or None if no path found.
    """
    if start == goal:
        return [start]

    # Check if goal is walkable
    if not level.is_walkable(goal[0], goal[1]):
        return None

    # Priority queue: (f_score, position, g_score)
    open_set: list[_Node] = []
    heapq.heappush(open_set, _Node(_heuristic(start, goal), start, 0))

    # Track came_from for path reconstruction
    came_from: dict[tuple[int, int], tuple[int, int]] = {}

    # Track best g_score for each position
    g_scores: dict[tuple[int, int], float] = {start: 0}

    # Track positions in open set
    open_positions: set[tuple[int, int]] = {start}

    iterations = 0
    while open_set and iterations < max_iterations:
        iterations += 1

        current_node = heapq.heappop(open_set)
        current = current_node.position
        open_positions.discard(current)

        if current == goal:
            # Reconstruct path
            path = [current]
            while current in came_from:
                current = came_from[current]
                path.append(current)
            path.reverse()
            return path

        current_g = g_scores[current]

        for neighbor in _get_neighbors(current[0], current[1]):
            nx, ny = neighbor

            # Check walkability
            if not level.is_walkable(nx, ny):
                continue

            # For diagonal moves, check that we can actually pass through
            dx = nx - current[0]
            dy = ny - current[1]
            if dx != 0 and dy != 0:
                # Diagonal move - check both adjacent tiles are walkable
                if not level.is_walkable(current[0] + dx, current[1]):
                    continue
                if not level.is_walkable(current[0], current[1] + dy):
                    continue

            # All moves cost 1 (Chebyshev distance)
            tentative_g = current_g + 1

            if neighbor not in g_scores or tentative_g < g_scores[neighbor]:
                came_from[neighbor] = current
                g_scores[neighbor] = tentative_g
                f_score = tentative_g + _heuristic(neighbor, goal)

                if neighbor not in open_positions:
                    heapq.heappush(open_set, _Node(f_score, neighbor, tentative_g))
                    open_positions.add(neighbor)

    return None  # No path found


def find_path_with_custom_walkable(
    start: tuple[int, int],
    goal: tuple[int, int],
    is_walkable: Callable[[int, int], bool],
    max_iterations: int = 10000,
) -> list[tuple[int, int]] | None:
    """Find a path using a custom walkability function.

    Args:
        start: Starting position (x, y).
        goal: Target position (x, y).
        is_walkable: Function that returns True if (x, y) is walkable.
        max_iterations: Maximum iterations before giving up.

    Returns:
        List of positions from start to goal (inclusive), or None if no path found.
    """
    if start == goal:
        return [start]

    if not is_walkable(goal[0], goal[1]):
        return None

    open_set: list[_Node] = []
    heapq.heappush(open_set, _Node(_heuristic(start, goal), start, 0))

    came_from: dict[tuple[int, int], tuple[int, int]] = {}
    g_scores: dict[tuple[int, int], float] = {start: 0}
    open_positions: set[tuple[int, int]] = {start}

    iterations = 0
    while open_set and iterations < max_iterations:
        iterations += 1

        current_node = heapq.heappop(open_set)
        current = current_node.position
        open_positions.discard(current)

        if current == goal:
            path = [current]
            while current in came_from:
                current = came_from[current]
                path.append(current)
            path.reverse()
            return path

        current_g = g_scores[current]

        for neighbor in _get_neighbors(current[0], current[1]):
            nx, ny = neighbor

            if not is_walkable(nx, ny):
                continue

            dx = nx - current[0]
            dy = ny - current[1]
            if dx != 0 and dy != 0:
                if not is_walkable(current[0] + dx, current[1]):
                    continue
                if not is_walkable(current[0], current[1] + dy):
                    continue

            tentative_g = current_g + 1

            if neighbor not in g_scores or tentative_g < g_scores[neighbor]:
                came_from[neighbor] = current
                g_scores[neighbor] = tentative_g
                f_score = tentative_g + _heuristic(neighbor, goal)

                if neighbor not in open_positions:
                    heapq.heappush(open_set, _Node(f_score, neighbor, tentative_g))
                    open_positions.add(neighbor)

    return None
