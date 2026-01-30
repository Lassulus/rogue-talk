"""Terminal UI rendering with blessed."""

from blessed import Terminal

from ..common.protocol import PlayerInfo


class TerminalUI:
    def __init__(self, terminal: Terminal):
        self.term = terminal

    def render(
        self,
        room_width: int,
        room_height: int,
        players: list[PlayerInfo],
        local_player_id: int,
        is_muted: bool,
    ) -> None:
        """Render the game state to the terminal."""
        output = []

        # Clear screen and move to top
        output.append(self.term.home + self.term.clear)

        # Draw the room
        for y in range(room_height):
            row = ""
            for x in range(room_width):
                char = self._get_cell_char(x, y, room_width, room_height, players, local_player_id)
                row += char
            output.append(row)

        # Status bar
        output.append("")
        local_player = next((p for p in players if p.player_id == local_player_id), None)
        mute_status = self.term.red("MUTED") if is_muted else self.term.green("LIVE")
        player_count = len(players)

        status = f"[{mute_status}] Players: {player_count}"
        if local_player:
            status += f" | Position: ({local_player.x}, {local_player.y})"
        output.append(status)

        # Player list
        output.append("")
        output.append("Players:")
        for p in players:
            marker = ">" if p.player_id == local_player_id else " "
            muted = " (muted)" if p.is_muted else ""
            output.append(f"  {marker} {p.name} at ({p.x}, {p.y}){muted}")

        # Controls
        output.append("")
        output.append("Controls: WASD/HJKL/Arrows=Move, M=Mute, Q=Quit")

        print("\n".join(output), end="", flush=True)

    def _get_cell_char(
        self,
        x: int,
        y: int,
        room_width: int,
        room_height: int,
        players: list[PlayerInfo],
        local_player_id: int,
    ) -> str:
        """Get the character to display at a cell."""
        # Check for players at this position
        for p in players:
            if p.x == x and p.y == y:
                if p.player_id == local_player_id:
                    return self.term.bold_green("@")
                else:
                    return self.term.bold_yellow("@")

        # Walls
        if x == 0 or x == room_width - 1 or y == 0 or y == room_height - 1:
            return "#"

        # Empty floor
        return "."

    def cleanup(self) -> None:
        """Restore terminal state."""
        print(self.term.normal + self.term.clear, end="")
