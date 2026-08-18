"""The plugin framework: user-defined keybinds and automations.

Plugins are Python files in `<store dir>/plugins/`, hot-reloaded when they
change. Each defines a PLUGIN dict:

    PLUGIN = {
        "name": "open-pr",                    # required, unique
        "description": "open the session's PR",
        "key": "ctrl+g",                      # optional: a keybind
        "run": lambda api: ...,               # invoked on the key
        "on_snapshot": lambda api, snap: ..., # optional: every refresh (~2s)
        "every": 300,                         # optional: seconds between...
        "tick": lambda api: ...,              # ...automation runs
    }

The `api` object is the extension surface (PluginAPI below). A broken
plugin never takes the app down: its error is captured and shown, the
rest keep working. Plugins are written by the "meta" Claude session
(`+` in cagents) — or by hand.
"""

from __future__ import annotations

import importlib.util
import time
from dataclasses import dataclass, field
from pathlib import Path

# Keys plugins may NOT take (the built-in bindings).
RESERVED_KEYS = {
    "1", "2", "3", "4", "tab", "enter", "space", "escape",
    "j", "k", "g", "G", "h", "l", "up", "down", "left", "right",
    "a", "n", "r", "e", "x", "d", "p", "m", "o", "t", "q",
    "A", "D", "F", "L", "R", "V", "W", "H",
    "question_mark", "comma", "colon", "equals_sign", "plus",
}


@dataclass
class PluginRecord:
    name: str
    path: Path
    description: str = ""
    key: str = ""
    run: object = None
    on_snapshot: object = None
    tick: object = None
    every: float = 0.0
    error: str = ""
    mtime: float = 0.0
    last_tick: float = 0.0


@dataclass
class PluginManager:
    plugin_dir: Path
    plugins: dict[str, PluginRecord] = field(default_factory=dict)
    _seen: dict[str, float] = field(default_factory=dict)  # path -> mtime

    def scan(self) -> list[str]:
        """(Re)load changed plugin files. Returns new error strings."""
        errors: list[str] = []
        if not self.plugin_dir.is_dir():
            return errors
        current: set[str] = set()
        for path in sorted(self.plugin_dir.glob("*.py")):
            current.add(str(path))
            try:
                mtime = path.stat().st_mtime
            except OSError:
                continue
            if self._seen.get(str(path)) == mtime:
                continue
            self._seen[str(path)] = mtime
            record = self._load(path, mtime)
            if record.error:
                errors.append(f"{path.name}: {record.error}")
            self.plugins[record.name] = record
        # drop plugins whose file went away
        for name, record in list(self.plugins.items()):
            if str(record.path) not in current:
                del self.plugins[name]
        return errors

    def _load(self, path: Path, mtime: float) -> PluginRecord:
        record = PluginRecord(name=path.stem, path=path, mtime=mtime)
        try:
            spec = importlib.util.spec_from_file_location(
                f"cagents_plugin_{path.stem}_{int(mtime)}", path
            )
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            manifest = getattr(module, "PLUGIN", None)
            if not isinstance(manifest, dict):
                raise ValueError("no PLUGIN dict")
            record.name = str(manifest.get("name") or path.stem)
            record.description = str(manifest.get("description", ""))
            key = str(manifest.get("key", "") or "")
            if key in RESERVED_KEYS:
                raise ValueError(f"key '{key}' is reserved by cagents")
            record.key = key
            record.run = manifest.get("run")
            record.on_snapshot = manifest.get("on_snapshot")
            record.tick = manifest.get("tick")
            record.every = float(manifest.get("every", 0) or 0)
            if record.key and not callable(record.run):
                raise ValueError("'key' needs a callable 'run'")
            if record.every and not callable(record.tick):
                raise ValueError("'every' needs a callable 'tick'")
        except Exception as error:  # any plugin failure stays contained
            record.error = str(error)[:200]
        return record

    def by_key(self, key: str) -> PluginRecord | None:
        for record in self.plugins.values():
            if record.key == key and not record.error:
                return record
        return None

    def snapshot_hooks(self) -> list[PluginRecord]:
        return [r for r in self.plugins.values() if callable(r.on_snapshot) and not r.error]

    def due_automations(self, now: float | None = None) -> list[PluginRecord]:
        now = time.time() if now is None else now
        due = []
        for record in self.plugins.values():
            if record.error or not record.every or not callable(record.tick):
                continue
            if now - record.last_tick >= record.every:
                record.last_tick = now
                due.append(record)
        return due


class PluginAPI:
    """What a plugin gets to touch. Deliberately the useful surface, not
    the whole app: cagents' own state, the selected session, tmux
    delivery, notifications, subprocesses."""

    def __init__(self, app):
        self._app = app
        self.store = app.store

    @property
    def snapshot(self):
        return self._app.snapshot

    def selected(self):
        return self._app.selected_view()

    def notify(self, message: str, severity: str = "information") -> None:
        self._app.call_from_thread(self._app.notify, message, severity=severity) \
            if self._threaded() else self._app.notify(message, severity=severity)

    def _threaded(self) -> bool:
        import threading

        return threading.current_thread() is not threading.main_thread()

    def send_text(self, session_id: str, text: str) -> bool:
        view = self._app.snapshot.by_id(session_id)
        if view is None or not view.live:
            return False
        self._app.tmux.send_text(view.tmux_name, text)
        return True

    def run(self, argv: list[str], cwd: str | None = None, timeout: float = 30.0):
        """Run a command; returns (returncode, stdout)."""
        import subprocess

        proc = subprocess.run(argv, cwd=cwd, capture_output=True, text=True, timeout=timeout)
        return proc.returncode, proc.stdout

    def open_url(self, url: str) -> None:
        import subprocess

        subprocess.run(["open", url], capture_output=True, timeout=10)

    def refresh(self) -> None:
        self._app.call_from_thread(self._app.refresh_data) \
            if self._threaded() else self._app.refresh_data()


PLUGIN_GUIDE = '''You are the cagents "meta" session: you extend the cagents TUI itself by
writing plugin files. Write them into THIS directory (your cwd): {plugin_dir}

A plugin is a single .py file defining a PLUGIN dict:

    PLUGIN = {{
        "name": "my-plugin",                # unique
        "description": "what it does",
        "key": "ctrl+g",                    # optional keybind (avoid cagents' own keys)
        "run": run,                         # def run(api): called on the key
        "on_snapshot": on_snapshot,         # optional: def on_snapshot(api, snapshot),
                                            #   called every ~2s refresh
        "every": 300, "tick": tick,         # optional: def tick(api), periodic automation
    }}

The api object:
    api.snapshot          # .views: list of sessions (see below); .by_id(sid)
    api.selected()        # the currently selected session view or None
    api.store             # cagents persistent state: .set_note(sid, txt),
                          # .set_label(sid, txt), .mark_reviewed(sid, iso),
                          # .todos, .sessions, .get_setting(key)
    api.notify(msg, severity="information"|"warning"|"error")
    api.send_text(sid, text) -> bool   # paste text into a live session's Claude prompt
    api.run(argv, cwd=None) -> (rc, stdout)   # subprocess, 30s timeout
    api.open_url(url)
    api.refresh()

A session view has: .session_id .title .state (.value is "working" / "needs input" /
"needs review" / "waiting on review" / "done" / "stopped") .live .project_dir .did_line
.needs_line .last_activity .tracked (the store record).

Rules: plugins hot-reload on save — just write the file, no restart. Never block:
keybind handlers run on the UI thread, keep them under ~100ms (use "every"/"tick"
for slow work — those run on worker threads). Errors are caught and shown, so
iterate freely. Reserved keys you must not take: the letters/keys cagents already
binds (1-4, j/k/g/G/h/l, enter/space/tab, a n r e x d p m o t q, A D F H L R V W,
comma, colon, ?, =, +).

The user's request follows. Write the plugin now, then briefly confirm what key or
automation it added.

REQUEST: {request}'''
