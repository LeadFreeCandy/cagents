"""Carry terminal defaults to an agent that starts in a detached tmux pane.

Codex queries OSC 10/11 once at startup. A detached session has no client's
colors to report, so Codex omits its user-message and composer backgrounds.
Query in a temporary window of the dashboard's attached session instead.
The helper owns its input stream; it never reads the user's keystrokes.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import shlex
import subprocess
import sys
import tempfile
import time

_REPLY = re.compile(rb"\x1b\](10|11);rgb:([\da-fA-F]{1,4})/([\da-fA-F]{1,4})/([\da-fA-F]{1,4})(?:\x07|\x1b\\)")


def parse_colors(data: bytes) -> dict[str, str]:
    colors = {}
    for match in _REPLY.finditer(data):
        rgb = [round(int(part, 16) * 255 / (16 ** len(part) - 1)) for part in match.groups()[1:]]
        colors[match[1].decode()] = "#" + "".join(f"{value:02x}" for value in rgb)
    return colors


def _query_tty(timeout: float = 0.4) -> dict[str, str]:
    """Run only in our dedicated helper pane, never the dashboard's tty."""
    import select
    import termios
    import tty

    previous = termios.tcgetattr(0)
    try:
        tty.setraw(0, termios.TCSANOW)
        os.write(1, b"\x1b]10;?\x1b\\\x1b]11;?\x1b\\")
        deadline = time.monotonic() + timeout
        data = b""
        while (remaining := deadline - time.monotonic()) > 0:
            if not select.select([0], [], [], remaining)[0]:
                break
            chunk = os.read(0, 1024)
            if not chunk:
                break
            data += chunk
            if len(parse_colors(data)) == 2 or len(data) > 4096:
                break
        return parse_colors(data)
    finally:
        termios.tcsetattr(0, termios.TCSANOW, previous)


def terminal_colors(tmux_bin: str = "tmux") -> dict[str, str]:
    parts = os.environ.get("TMUX", "").rsplit(",", 2)
    if len(parts) != 3 or not parts[2].isdigit():
        return {}
    tmux = [tmux_bin, "-S", parts[0]]
    window = ""
    try:
        with tempfile.TemporaryDirectory(prefix="cagents-colors-") as folder:
            result = Path(folder) / "colors.json"
            command = shlex.join([sys.executable, str(Path(__file__).resolve()), str(result)])
            proc = subprocess.run(
                [*tmux, "new-window", "-d", "-P", "-F", "#{window_id}",
                 "-t", f"${parts[2]}:", "-n", "cagents-colors", command],
                capture_output=True, text=True, timeout=2,
            )
            if proc.returncode:
                return {}
            window = proc.stdout.strip()
            deadline = time.monotonic() + 1
            while time.monotonic() < deadline:
                try:
                    colors = json.loads(result.read_text())
                    return {key: value for key, value in colors.items()
                            if key in ("10", "11") and isinstance(value, str)
                            and re.fullmatch(r"#[\da-fA-F]{6}", value)}
                except (OSError, ValueError):
                    time.sleep(0.01)
    except (OSError, subprocess.TimeoutExpired):
        pass  # lack of terminal color support must never prevent a launch
    finally:
        if re.fullmatch(r"@\d+", window):
            try:
                subprocess.run([*tmux, "kill-window", "-t", window],
                               capture_output=True, timeout=2)
            except (OSError, subprocess.TimeoutExpired):
                pass
    return {}


def prepare_command(command: str, tmux_bin: str = "tmux") -> str:
    colors = terminal_colors(tmux_bin)
    if not colors:
        return command
    # These OSC setters change only the new native pane's palette, not the
    # outer terminal or global tmux styling. Hex values above are validated.
    sequences = "".join(f"\\033]{key};{value}\\033\\\\" for key, value in colors.items())
    launch = command if command.startswith("exec ") else f"exec {command}"
    return f"printf '%b' {shlex.quote(sequences)}; {launch}"


if __name__ == "__main__":
    try:
        colors = _query_tty()
    except (OSError, ValueError):
        colors = {}
    Path(sys.argv[1]).write_text(json.dumps(colors))
