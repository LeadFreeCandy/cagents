"""Native tab commands. This module does not handle terminal input or output."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
import time

from . import ctx
from .direct import DirectSidecar, DirectTmux, SESSION, _placeholder, server_lock


def shell(context, sidecar, select=True):
    client = sidecar.client
    current = client.call("display-message", "-p", "#{window_name}")
    if select and current.startswith("term-"):
        sidecar.focus_session()
        return
    directory, kind, _ = ctx.resolve_terminal_directory(str(context.get("dir", "")))
    if not kind:
        command = _placeholder("No git worktree found for this conversation")
    elif context.get("tmux_name"):
        name = str(context["tmux_name"])
        socket = str(context.get("tmux_socket") or client.create_socket)
        client.ensure_session_window(name, "term", directory, socket)
        target = client.ensure_window_view(name, "term", socket, force_select=select)
        command = sidecar.attach_command(socket, target)
    else:
        # A transcript without a running agent still gets its own persistent
        # terminal. Its name is keyed by conversation, never by directory.
        name = "terminal-" + str(context.get("session_id", "untracked"))
        try:
            command = client.pane(name)
        except RuntimeError:
            client._new_home(name, directory, role="terminal", env=sidecar.term_env)
            command = client.pane(name)
    sidecar.sync_terminal_tab(command)
    if select:
        sidecar.select_tab("term-1")
        sidecar.focus_pane()


def diff(context, sidecar, select=True):
    client = sidecar.client
    directory = str(context.get("dir", ""))
    target = f"={SESSION}:diff.1"
    mode = str(context.get("diff_mode", "branch"))
    if not Path(directory).is_dir() or subprocess.run(
        ["git", "-C", directory, "rev-parse", "--is-inside-work-tree"],
        capture_output=True, timeout=10,
    ).returncode:
        sidecar._show("diff", _placeholder("No git repository for this conversation"))
    else:
        key = json.dumps([directory, mode])
        old = client.call("display-message", "-p", "-t", target, "#{@cagents_diff_key}")
        built = client.call("display-message", "-p", "-t", target, "#{@cagents_diff_built}")
        dead = client.call("display-message", "-p", "-t", target, "#{pane_dead}")
        # Selecting the tab after C-d fires the native click hook too. Build
        # once, without a second respawn and flash from the hook.
        if old != key or dead == "1" or time.time() - float(built or 0) > 1.5:
            lazygit = shutil.which("lazygit") is not None
            command = ctx.lazygit_command(directory) if lazygit else "sh -c " + shlex.quote(ctx.diff_popup_command(directory, mode))
            client.call("respawn-pane", "-k", "-t", target, command)
            client.call("set", "-p", "-t", target, "@cagents_diff_key", key)
            client.call("set", "-p", "-t", target, "@cagents_diff_built", str(time.time()))
            if select:
                sidecar.select_tab("diff")
                sidecar.focus_pane()
            if lazygit and mode == "branch":
                ref = ctx._merge_base_ref(directory)
                if ref:
                    # Same native lazygit behavior as the original diff tab.
                    time.sleep(1.2)
                    for keys, delay in ((["W"], .4), (["Enter"], .4), (["-l", ref], .2), (["Enter"], 0)):
                        client.call("send-keys", "-t", target, *keys)
                        time.sleep(delay)
    if select:
        sidecar.select_tab("diff")
        sidecar.focus_pane()


def new_terminal(context, sidecar):
    client = sidecar.client
    directory = str(context.get("dir", ""))
    if not Path(directory).is_dir():
        directory = os.environ.get("CAGENTS_LAUNCH_CWD") or os.getcwd()
    names = client.call("list-windows", "-t", f"={SESSION}", "-F", "#W").splitlines()
    number = 2
    while f"term-{number}" in names:
        number += 1
    name = f"term-{number}"
    sidecar._ensure_tab(name, cwd=directory, command=os.environ.get("SHELL", "/bin/zsh"))
    sidecar.select_tab(name)
    sidecar.focus_pane()


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] in ("event", "wlog"):
        return ctx.main(argv)  # provider hooks and logging do not involve layout
    parser = argparse.ArgumentParser(prog="cagents3-ctx")
    parser.add_argument("command", choices=("shell", "diff", "tab", "new-term"))
    parser.add_argument("--context", type=Path, required=True)
    parser.add_argument("--no-select", action="store_true")
    args = parser.parse_args(argv)
    ctx.init_log(args.context.parent)
    ctx.LAZYGIT_CONFIG_DIR = args.context.parent
    client = DirectTmux()
    rail = client.call("show-option", "-gqv", "@cagents_rail")
    sidecar = DirectSidecar(client=client, own_pane=rail)
    sidecar.term_env = json.loads(client.call("show-option", "-gqv", "@cagents_term_env") or "[]")
    with server_lock(client.create_socket):
        # Read after acquiring the layout lock: a queued hook must use the
        # latest selection, rather than its caller's stale conversation.
        context = ctx.read_context(args.context)
        command, select = args.command, not args.no_select
        if command == "tab":
            tab = client.call("display-message", "-p", "#{window_name}")
            command = {"term-1": "shell", "diff": "diff", "new-term": "new-term"}.get(tab)
            select = False
        if command == "shell":
            shell(context, sidecar, select)
        elif command == "diff":
            diff(context, sidecar, select)
        elif command == "new-term":
            new_terminal(context, sidecar)
    return 0


def tmux_entry(argv=None):
    # A nonzero run-shell result paints tmux's error screen over the native
    # agent. Match the existing entry point: report errors through the rail.
    try:
        main(argv)
    except SystemExit as error:
        ctx._log(f"cagents3-ctx: {error}")
    except Exception as error:
        ctx._log(f"cagents3-ctx: {error}")
        args = list(sys.argv[1:] if argv is None else argv)
        if "--context" in args and args.index("--context") + 1 < len(args):
            ctx.queue_toast(Path(args[args.index("--context") + 1]).parent, str(error), "error")
    return 0


if __name__ == "__main__":
    sys.exit(tmux_entry())
