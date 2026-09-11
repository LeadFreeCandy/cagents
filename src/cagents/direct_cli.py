"""Isolated cagents3 launcher. It never tears down the shared agent server."""
from __future__ import annotations

import os
from pathlib import Path
import shlex
import shutil
import sys

from .direct import DirectTmux, SESSION, server_lock, setup_commands


def state_path() -> Path:
    from .store import default_store_path
    return default_store_path().parent.parent / "cagents3" / "state.json"


def seed_state(path: Path) -> None:
    """Copy bookkeeping once. Transcripts remain in the native CLI stores."""
    from .store import default_store_path
    source = default_store_path()
    if path.exists() or not source.is_file():
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with path.open("xb") as out:
            out.write(source.read_bytes())
    except FileExistsError:
        pass  # another launch won the race


def needs_bootstrap(environ, is_tty):
    if not is_tty:
        return False
    from .direct import direct_socket
    socket_path = environ.get("TMUX", "").rsplit(",", 2)[0]
    return not (environ.get("CAGENTS_DIRECT_RAIL") == "1"
                and Path(socket_path).name == direct_socket())


def bootstrap(argv: list[str]):
    client = DirectTmux()
    with server_lock(client.create_socket):
        command = shlex.join(["env", "CAGENTS_DIRECT=1", "CAGENTS_SIDECAR=1", "CAGENTS_DIRECT_RAIL=1",
                              f"CAGENTS_DIRECT_SOCKET={client.create_socket}",
                              f"CAGENTS_LAUNCH_CWD={os.environ.get('CAGENTS_LAUNCH_CWD', os.getcwd())}",
                              f"CAGENTS_TERM_PROGRAM={os.environ.get('CAGENTS_TERM_PROGRAM', '')}",
                              f"CAGENTS_SOCKET_SUFFIX={os.environ.get('CAGENTS_SOCKET_SUFFIX', '')}",
                              sys.executable, "-m", "cagents.direct_cli", *argv])
        has = client._run(client.create_socket, "has-session", "-t", f"={SESSION}")
        if has.returncode:
            size = shutil.get_terminal_size()
            rail = client.call("new-session", "-d", "-P", "-F", "#{pane_id}", "-s", SESSION,
                               "-n", "session", "-x", str(size.columns), "-y", str(size.lines), "sleep 30")
            # Set retention before starting Textual; an early exception cannot
            # make tmux promote a live agent into the dashboard's position.
            for cmd in setup_commands():
                client.call(*cmd)
            client.call("set", "-g", "@cagents_rail", rail)
            client.call("respawn-pane", "-k", "-t", rail, command)
        else:
            rail = client.call("show-option", "-gqv", "@cagents_rail")
            if not rail:
                raise RuntimeError("cagents3's dashboard pane is missing; its agent server was left intact.")
            dead = client.call("display-message", "-p", "-t", rail, "#{pane_dead}")
            if dead == "1":
                client.call("respawn-pane", "-k", "-t", rail, command)
        client.call("select-window", "-t", f"={SESSION}:session")
        client.call("select-pane", "-t", rail)
    env = os.environ.copy()
    env.pop("TMUX", None)
    os.execvpe("tmux", ["tmux", "-L", client.create_socket, "attach-session", "-t", f"={SESSION}"], env)


def main(argv=None):
    os.environ["CAGENTS_DIRECT"] = "1"
    from .__main__ import main as app_main
    return app_main(argv)


if __name__ == "__main__":
    sys.exit(main())
