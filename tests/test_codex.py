"""Codex integration regressions. All transcripts, stores and transports are isolated."""
import json
import os
import shlex
import subprocess
from datetime import datetime, timezone
from pathlib import Path

import pytest

from cagents import codex_data
from cagents.app import CagentsApp
from cagents.claude_data import ParsedSession
from cagents.codex_rpc import CodexClient, CodexRpcError
from cagents.search import search_all_sessions
from cagents.sessions import SessionRegistry, SessionState, SessionView, Snapshot, derive_state, map_tmux_sessions
from cagents.store import Store, TrackedSession
from cagents.tmuxctl import TmuxClient, TmuxSession
from conftest import FakeTmux, TranscriptBuilder

SID = "01234567-0123-4567-8901-012345678901"
KEY = "codex:" + SID
TS = "2026-09-09T10:00:00Z"
NOW = datetime.fromisoformat(TS.replace("Z", "+00:00")).timestamp()


def rec(kind, payload, ts=TS):
    return {"type": kind, "timestamp": ts, "payload": payload}


def message(role, text, **extra):
    return rec("response_item", {"type": "message", "role": role,
               "content": [{"type": "input_text" if role == "user" else "output_text", "text": text}], **extra})


def rollout(root, *extra, folder="sessions"):
    path = root / folder / "2026/09/09" / f"rollout-2026-09-09T10-00-00-{SID}.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    rows = [rec("session_meta", {"id": SID, "cwd": "/proj", "cli_version": "test",
                               "git": {"branch": "main"}}), *extra]
    path.write_text("\n".join(json.dumps(r) for r in rows) + "\n")
    return path


def test_parser_lifecycle_metadata_and_preview(tmp_path):
    path = rollout(tmp_path,
        message("developer", "hidden config"), message("user", "Fix the widget"),
        rec("turn_context", {"cwd": "/proj-worktree", "model": "configured-model"}),
        rec("event_msg", {"type": "task_started"}),
        rec("response_item", {"type": "function_call", "name": "exec_command", "call_id": "1", "arguments": '{"cmd":"pytest"}'}),
        rec("response_item", {"type": "function_call_output", "call_id": "1", "output": "passed"}),
        message("assistant", "Fixed and tested.", phase="final_answer"),
        rec("event_msg", {"type": "task_complete", "duration_ms": 1200}),
        rec("event_msg", {"type": "token_count"}, "2026-09-09T12:00:00Z"))
    with path.open("a") as f:
        f.write('null\n[]\n{"type": "response_item", "payload": []}\n{"unfinished"')
    p = codex_data.parse_session_file(path)
    assert p.session_id == KEY
    assert (p.title, p.cwd, p.last_cwd, p.git_branch, p.model) == ("Fix the widget", "/proj", "/proj-worktree", "main", "configured-model")
    assert p.turn_state == "completed" and not p.pending_tool_use
    assert p.last_timestamp.timestamp() == NOW
    assert p.last_assistant_text == "Fixed and tested."
    assert [item.kind for item in p.preview] == ["user", "tool", "assistant"]


@pytest.mark.parametrize("event_format", [False, True])
def test_setup_messages_do_not_become_titles_preview_or_activity(tmp_path, event_format):
    def user(text, ts=TS):
        if event_format:
            return rec("event_msg", {"type": "user_message", "message": text}, ts)
        row = message("user", text)
        row["timestamp"] = ts
        return row

    setup = "<recommended_plugins>\nPlugin catalog, not a task.\n</recommended_plugins>"
    path = rollout(tmp_path,
        user(setup), user("# AGENTS.md instructions for /proj\nProject setup."),
        user("<environment_context>\n<cwd>/proj</cwd>\n</environment_context>"),
        user("Fix the widget"),
        message("assistant", "Fixed.", phase="final_answer"),
        user(setup, "2026-09-09T12:00:00Z"))
    parsed = codex_data.parse_session_file(path)
    assert parsed.title == "Fix the widget"
    assert [(item.kind, item.text) for item in parsed.preview] == [
        ("user", "Fix the widget"), ("assistant", "Fixed.")]
    assert parsed.turn_state == "completed"
    assert parsed.last_timestamp.timestamp() == NOW
    assert parsed.last_record_role == "assistant"
    assert codex_data.scan_transcript(path) == ("/proj", "Fix the widget", ["Fix the widget", "Fixed."])


def test_context_and_task_in_the_same_message_preserve_the_task(tmp_path):
    path = rollout(tmp_path, rec("response_item", {"type": "message", "role": "user", "content": [
        {"type": "input_text", "text": "<recommended_plugins>Plugin catalog.</recommended_plugins>"},
        {"type": "input_text", "text": "<environment_context><cwd>/proj</cwd></environment_context>"},
        {"type": "input_text", "text": "Fix the widget\nKeep the existing API."},
    ]}))
    parsed = codex_data.parse_session_file(path)
    assert parsed.title == "Fix the widget"
    assert parsed.preview[0].text == "Fix the widget\nKeep the existing API."


def test_context_only_thread_uses_id_without_inventing_activity(tmp_path):
    path = rollout(tmp_path, message("user", "<recommended_plugins>Plugin catalog.</recommended_plugins>"))
    parsed = codex_data.parse_session_file(path)
    assert parsed.title == SID[:8]
    assert not parsed.preview and not parsed.turn_state and parsed.last_timestamp is None


@pytest.mark.parametrize("text", ["<request>Fix the widget</request>",
                                 "Fix <recommended_plugins> rendering", "<div>Example HTML</div>"])
def test_user_markup_is_not_mistaken_for_setup(tmp_path, text):
    path = rollout(tmp_path, message("user", text))
    parsed = codex_data.parse_session_file(path)
    assert parsed.title == text and parsed.preview[0].text == text


def test_saved_codex_names_follow_renames_while_suspended(tmp_path, monkeypatch):
    root = tmp_path / "codex"
    rollout(root, message("user", "Original prompt"), message("assistant", "Done", phase="final_answer"))
    index = root / "session_index.jsonl"
    index.write_text(json.dumps({"id": SID, "thread_name": "Native conversation name"}) + "\n")
    store = Store(tmp_path / "state.json")
    tracked = store.track(KEY, "/proj", TS)
    tracked.suspended_at = TS
    registry = SessionRegistry(store, tmux=FakeTmux(), claude_dir=tmp_path / "claude", codex_dir=root)
    registry.codex_client.socket_path.parent.mkdir(parents=True)
    registry.codex_client.socket_path.touch()
    def no_rpc(*args):
        raise AssertionError("Reading a suspended conversation's name must not contact its runtime")
    monkeypatch.setattr(registry.codex_client, "call", no_rpc)
    assert registry.refresh(now=NOW + 1).by_id(KEY).title == "Native conversation name"
    # Only the native name index changes; the rollout and cagents store do not.
    with index.open("a") as f:
        f.write(json.dumps({"id": SID, "thread_name": "Renamed in Codex"}) + "\n")
    view = registry.refresh(now=NOW + 2).by_id(KEY)
    assert view.title == "Renamed in Codex" and view.last_activity.timestamp() == NOW
    tracked.label = "My explicit cagents label"
    assert registry.refresh(now=NOW + 3).by_id(KEY).title == tracked.label


def test_native_name_is_available_without_a_rollout(tmp_path):
    (tmp_path / "session_index.jsonl").write_text(json.dumps({"id": SID, "thread_name": "Saved name"}) + "\n")
    store = Store(tmp_path / "state.json")
    store.track(KEY, "/proj", TS)
    registry = SessionRegistry(store, tmux=FakeTmux(), claude_dir=tmp_path / "claude", codex_dir=tmp_path)
    view = registry.refresh(now=NOW + 1).by_id(KEY)
    assert view.title == "Saved name" and view.missing


def test_name_index_handles_partial_writes_and_keeps_only_tracked_names(tmp_path):
    index = tmp_path / "session_index.jsonl"
    index.write_text('\n'.join(["null", "[]", json.dumps({"id": SID, "thread_name": "Saved name"}),
                                 json.dumps({"id": "untracked", "thread_name": "Not retained"}), '{"partial"']))
    names = codex_data.SessionNames(tmp_path)
    assert names.read({KEY}) == {KEY: "Saved name"}
    assert names.read(set()) == {} and not names._names
    assert names.read({KEY}) == {KEY: "Saved name"}


def test_bad_thread_does_not_block_other_threads_names_or_renames(tmp_path, monkeypatch):
    rollout(tmp_path, message("user", "Fallback prompt"))
    store = Store(tmp_path / "state.json")
    bad_key = "codex:missing-thread"
    store.track(bad_key, "/proj", TS)
    store.track(KEY, "/proj", TS)
    registry = SessionRegistry(store, tmux=FakeTmux(), claude_dir=tmp_path / "claude", codex_dir=tmp_path)
    registry.codex_client.socket_path.parent.mkdir()
    registry.codex_client.socket_path.touch()
    calls, name = [], "Native name"
    def read(method, params):
        calls.append(params["threadId"])
        if params["threadId"] == "missing-thread":
            raise CodexRpcError("thread not found")
        return {"thread": {"name": name, "status": {"type": "idle"}}}
    monkeypatch.setattr(registry.codex_client, "call", read)
    assert registry.refresh(now=NOW + 1).by_id(KEY).title == "Native name"
    name = "Renamed live"
    assert registry.refresh(now=NOW + 2).by_id(KEY).title == "Renamed live"
    assert calls == ["missing-thread", SID, SID]


@pytest.mark.parametrize("event, expected", [("task_complete", SessionState.NEEDS_REVIEW),
                                            ("turn_aborted", SessionState.STOPPED),
                                            ("task_started", SessionState.WORKING)])
def test_codex_turn_states_are_not_claude_heuristics(tmp_path, event, expected):
    path = rollout(tmp_path, message("user", "Work"), message("assistant", "On it", phase="commentary"),
                   rec("event_msg", {"type": event}))
    p = codex_data.parse_session_file(path)
    tracked = TrackedSession(KEY, "/proj", TS)
    assert derive_state(p, tracked, True, now=NOW + 1)[0] == expected
    if event == "task_complete":
        tracked.reviewed_at = TS
        assert derive_state(p, tracked, True, now=NOW + 1)[0] == SessionState.DONE
        path = rollout(tmp_path, message("user", "Next"), rec("event_msg", {"type": "task_started"}, "2026-09-09T10:00:10Z"))
        assert derive_state(codex_data.parse_session_file(path), tracked, True, now=NOW + 11)[0] == SessionState.WORKING


def test_codex_approval_and_external_runtime_status(tmp_path):
    p = codex_data.parse_session_file(rollout(tmp_path, message("assistant", "Finished", phase="final_answer")))
    t = TrackedSession(KEY, "/proj", TS)
    assert derive_state(p, t, False, now=NOW + 100, agent_state={"type": "active", "activeFlags": ["waitingOnApproval"]})[0] == SessionState.NEEDS_INPUT
    assert derive_state(p, t, False, now=NOW + 100, agent_state={"type": "active", "activeFlags": []})[0] == SessionState.WORKING
    assert derive_state(p, t, True, "Would you like to run the following command?", now=NOW + 100)[0] == SessionState.NEEDS_INPUT


def test_remote_disconnect_interrupts_only_its_own_turn(tmp_path, monkeypatch):
    client = CodexClient(tmp_path)
    calls = []
    pages = iter([{"data": [{"id": "turn-1", "status": "inProgress"}]},
                  {"data": [{"id": "turn-1", "status": "interrupted"}]}])
    def call(method, params):
        calls.append((method, params))
        return next(pages) if method == "thread/turns/list" else {}
    monkeypatch.setattr(client, "call", call)
    client.stop_thread_activity(SID)
    assert ("turn/interrupt", {"threadId": SID, "turnId": "turn-1"}) in calls
    assert calls[-1] == ("thread/backgroundTerminals/clean", {"threadId": SID})
    assert all(params["threadId"] == SID for _, params in calls)


def test_discovery_search_and_archive_move(tmp_path):
    root = tmp_path / "codex"
    path = rollout(root, message("user", "Widget regression"), message("assistant", "The special needle is here.", phase="final_answer"))
    found = codex_data.discover_sessions(root)
    assert [(s.session_id, s.provider) for s in found] == [(KEY, "codex")]
    results = search_all_sessions(tmp_path / "claude", "special needle", codex_dir=root)
    assert results[0].session_id == KEY
    store = Store(tmp_path / "state.json")
    store.track(KEY, "/proj", TS)
    registry = SessionRegistry(store, tmux=FakeTmux(), codex_dir=root)
    assert registry.refresh(now=NOW + 100).by_id(KEY).state == SessionState.NEEDS_REVIEW
    dest = root / "archived_sessions" / path.name
    dest.parent.mkdir()
    path.rename(dest)
    assert registry.refresh(now=NOW + 100).by_id(KEY).parsed.path == dest


@pytest.mark.parametrize("status", ["active", "idle"])
def test_open_active_codex_outside_tmux_attaches_to_existing_server(tmp_path, monkeypatch, status):
    # Live regression: "Analyze strategies for profitability" was active in
    # the shared server but had no cagents pane. The Claude duplicate-process
    # guard rejected Enter/preview before Codex could attach its native TUI.
    app = CagentsApp(store=Store(tmp_path / "state.json"), tmux=FakeTmux(),
                     codex_dir=tmp_path / "codex home")
    t = app.store.track(KEY, str(tmp_path), TS)
    p = ParsedSession(KEY, tmp_path / "rollout", cwd=str(tmp_path), turn_state="running")
    view = SessionView(KEY, t, p, SessionState.WORKING, live=False)
    client = app.registry.codex_client
    client.socket_path.parent.mkdir(parents=True)
    client.socket_path.touch()
    calls, launches = [], []

    def read(method, params):
        calls.append((method, params))
        return {"thread": {"id": SID, "status": {"type": status}}}

    monkeypatch.setattr(client, "call", read)
    monkeypatch.setattr(app, "_agent_bin", lambda _: "/bin/codex")
    monkeypatch.setattr(app, "_spawn_session", lambda directory, args, sid:
                        launches.append((directory, args, sid)) or "codex-attached")
    assert app._resume_target(view) == ("codex-attached", "", "")
    assert calls == [("thread/read", {"threadId": SID})]
    assert launches == [(str(tmp_path), ["resume", SID, "-C", str(tmp_path),
                         "--remote", f"unix://{client.socket_path}"], KEY)]


@pytest.mark.parametrize("failure", ["missing_socket", "rpc_error", "notLoaded"])
def test_open_active_codex_never_falls_back_to_a_second_engine(tmp_path, monkeypatch, failure):
    app = CagentsApp(store=Store(tmp_path / "state.json"), tmux=FakeTmux(), codex_dir=tmp_path)
    t = app.store.track(KEY, str(tmp_path), TS)
    view = SessionView(KEY, t, ParsedSession(KEY, tmp_path / "rollout"), SessionState.WORKING, live=False)
    client = app.registry.codex_client
    if failure != "missing_socket":
        client.socket_path.parent.mkdir(parents=True)
        client.socket_path.touch()

    def read(method, params):
        if failure == "rpc_error":
            raise CodexRpcError("unavailable")
        assert failure == "notLoaded"
        return {"thread": {"id": SID, "status": {"type": "notLoaded"}}}

    monkeypatch.setattr(client, "call", read)
    monkeypatch.setattr(app, "_spawn_session", lambda *args: pytest.fail("must not launch a second engine"))
    name, reason, severity = app._resume_target(view)
    assert name is None and reason and severity == "warning"


def test_open_active_claude_outside_tmux_still_refuses_duplicate_process(tmp_path, monkeypatch):
    app = CagentsApp(store=Store(tmp_path / "state.json"), tmux=FakeTmux())
    t = app.store.track(SID, str(tmp_path), TS)
    view = SessionView(SID, t, ParsedSession(SID, tmp_path / "transcript"), SessionState.WORKING, live=False)
    monkeypatch.setattr(app, "_spawn_session", lambda *args: pytest.fail("must not duplicate Claude"))
    name, reason, severity = app._resume_target(view)
    assert name is None and "running outside" in reason and severity == "warning"


def test_namespaces_and_sharing_preserve_codex(tmp_path, monkeypatch):
    monkeypatch.setenv("CAGENTS_SHARED_DB", str(tmp_path / "shared.sqlite3"))
    store = Store(tmp_path / "state.json")
    store.track(SID, "/claude", TS)
    store.track(KEY, "/codex", TS)
    store.sync_shared()
    loaded = Store.load(store.path)
    assert loaded.sessions[KEY].provider == "codex"
    assert loaded.sessions[KEY].native_session_id == SID
    from cagents.shared import sync
    observed = []
    sync(tmp_path / "shared.sqlite3", "other", {}, lambda shared: observed.append(shared))
    assert observed == [{SID: "/claude"}]
    store.sync_shared()
    assert KEY in store.sessions


def test_codex_mapping_never_claims_unverified_claude_pane(tmp_path):
    t = TrackedSession(KEY, "/proj", TS)
    p = ParsedSession(KEY, tmp_path / "x", cwd="/proj", mtime=100, title="Codex fixes the widget")
    tmux = TmuxSession("claude", 90, 100, False, 1, "/proj")
    assert map_tmux_sessions([(t, p)], [tmux]) == {}
    assert map_tmux_sessions([(t, p)], [tmux], lambda _: p.title)[KEY] == tmux
    tmux.cagents_session_id = KEY
    assert map_tmux_sessions([(t, p)], [tmux])[KEY] == tmux


def test_codex_tmux_command_preserves_args_home_and_identity(tmp_path, monkeypatch):
    tmux = TmuxClient(create_socket="test-codex")
    calls = []
    monkeypatch.setattr(tmux, "_unique_name", lambda _: "codex-test")
    def run(socket, *args, **kwargs):
        calls.append(args)
        return subprocess.CompletedProcess([], 0, "", "")
    monkeypatch.setattr(tmux, "_run", run)
    root = tmp_path / "codex home"
    tmux.new_codex_session("/project path", ["resume", SID, "hello $(no)"], KEY, root, "/bin/codex")
    launch = calls[0]
    assert f"CAGENTS_SESSION_ID={KEY}" in launch and f"CODEX_HOME={root}" in launch
    assert shlex.split(launch[-1]) == ["/bin/codex", "resume", SID, "hello $(no)"]
    assert "--settings" not in launch[-1] and "--session-id" not in launch[-1]


@pytest.mark.asyncio
async def test_app_import_resume_and_shell_spawn(tmp_path, monkeypatch):
    root = tmp_path / "codex"
    rollout(root, message("user", "Fix widget"), message("assistant", "Finished", phase="final_answer"))
    (root / "session_index.jsonl").write_text(json.dumps({"id": SID, "thread_name": "Renamed widget task"}) + "\n")
    # Use a real existing cwd for resume validation.
    path = next((root / "sessions").rglob("*.jsonl"))
    path.write_text(path.read_text().replace('"/proj"', json.dumps(str(tmp_path))))
    store = Store(tmp_path / "state.json")
    tmux = FakeTmux()
    registry = SessionRegistry(store, tmux=tmux, claude_dir=tmp_path / "claude", codex_dir=root)
    app = CagentsApp(store=store, tmux=tmux, registry=registry)
    launches = []
    monkeypatch.setattr(app, "_agent_bin", lambda provider: "/bin/" + provider)
    monkeypatch.setattr(tmux, "new_codex_session", lambda directory, args, sid, home, binary: launches.append((directory, args, sid, home)) or "codex-native", raising=False)
    monkeypatch.setattr(app, "_show_new_session", lambda name: None)
    async with app.run_test(size=(120, 35)) as pilot:
        await pilot.pause()
        await app._load_track_candidates().wait()
        await pilot.pause()
        assert app.screen.candidates[0][1] == "Renamed widget task"
        await pilot.press("down")
        await pilot.press("enter")
        await pilot.pause()
        assert KEY in store.sessions
        view = registry.refresh(now=NOW + 100).by_id(KEY)
        assert app._resume_target(view)[0] == "codex-native"
        assert launches[-1][1][:2] == ["resume", SID]
        assert launches[-1][2:] == (KEY, root)
        pending = "pending-shell"
        app._pending_new_terminals.add(pending)
        store.track(pending, str(tmp_path), TS)
        monkeypatch.setattr(CodexClient, "create", lambda self, directory, parent: SID)
        app.snapshot = Snapshot()
        worker = app._start_codex_worker(str(tmp_path), ["--model", "user-model"], pending)
        await worker.wait()
        assert pending not in store.sessions
        assert app.selected_session_id == KEY
        assert launches[-1][1][-2:] == ["--model", "user-model"]
        app._write_claude_shim()
        assert '"provider": "codex"' in (app._shim_dir() / "codex").read_text()
        assert "codex()" in (store.path.parent / "zdot/.zshrc").read_text()
        await pilot.pause()


def test_rpc_handshake_and_read_never_resume(tmp_path, monkeypatch):
    sent = []
    replies = iter([json.dumps({"id": 1, "result": {}}),
                    json.dumps({"method": "notification"}),
                    json.dumps({"id": 2, "result": {"thread": {"id": SID}}})])
    class Connection:
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def send(self, data): sent.append(json.loads(data))
        def recv(self, **kwargs): return next(replies)
    monkeypatch.setattr("websockets.sync.client.unix_connect", lambda *args, **kwargs: Connection())
    client = CodexClient(tmp_path)
    assert client.call("thread/read", {"threadId": SID})["thread"]["id"] == SID
    assert [msg["method"] for msg in sent] == ["initialize", "initialized", "thread/read"]


def test_codex_runner_uses_stdin_and_final_output(tmp_path, monkeypatch):
    from cagents.codex_rpc import CliCodexRunner
    calls = []
    def run(args, **kwargs):
        calls.append((args, kwargs))
        Path(args[args.index("--output-last-message") + 1]).write_text("handoff spec")
        return subprocess.CompletedProcess(args, 0, "progress is not the answer", "")
    monkeypatch.setattr(subprocess, "run", run)
    assert CliCodexRunner(tmp_path, str(tmp_path), context="source").run("summarize") == "handoff spec"
    args, kwargs = calls[0]
    assert args[1] == "exec" and "resume" not in args
    assert args[args.index("--sandbox") + 1] == "read-only"
    assert kwargs["input"] == "source\n\nsummarize"
    assert kwargs["env"]["CODEX_HOME"] == str(tmp_path)


def test_create_and_fork_use_returned_thread_id(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(subprocess, "run", lambda *args, **kwargs: subprocess.CompletedProcess(args, 0, "", ""))
    client = CodexClient(tmp_path)
    monkeypatch.setattr(client, "call", lambda method, params: calls.append((method, params)) or {"thread": {"id": SID}})
    assert client.create("/proj") == SID
    assert calls[-2] == ("thread/start", {"cwd": "/proj"})
    assert calls[-1][0] == "thread/inject_items"
    assert client.create("/worktree", "parent-id") == SID
    assert calls[-2] == ("thread/fork", {"cwd": "/worktree", "threadId": "parent-id", "excludeTurns": True})


def test_fork_with_persisted_history_needs_no_startup_note(tmp_path, monkeypatch):
    path = tmp_path / "rollout.jsonl"
    path.write_text("original fork history\n")
    monkeypatch.setattr(subprocess, "run", lambda *args, **kwargs: subprocess.CompletedProcess(args, 0, "", ""))
    client, calls = CodexClient(tmp_path), []
    monkeypatch.setattr(client, "call", lambda method, params: calls.append(method) or {"thread": {"id": SID, "path": str(path)}})
    assert client.create("/proj", "parent") == SID
    assert calls == ["thread/fork"]
    assert path.read_text() == "original fork history\n"


def test_failed_startup_persistence_does_not_return_a_broken_thread(tmp_path, monkeypatch):
    monkeypatch.setattr(subprocess, "run", lambda *args, **kwargs: subprocess.CompletedProcess(args, 0, "", ""))
    client = CodexClient(tmp_path)
    def call(method, params):
        if method == "thread/inject_items":
            raise RuntimeError("could not persist startup context")
        return {"thread": {"id": SID}}
    monkeypatch.setattr(client, "call", call)
    with pytest.raises(RuntimeError, match="persist startup context"):
        client.create("/proj")


@pytest.mark.asyncio
async def test_codex_cd_and_fork_worker(tmp_path, monkeypatch):
    project = tmp_path / "project"
    project.mkdir()
    store = Store(tmp_path / "state.json")
    app = CagentsApp(store=store, tmux=FakeTmux(), claude_dir=tmp_path / "claude", codex_dir=tmp_path / "codex")
    creates, launches = [], []
    monkeypatch.setattr(app, "_agent_bin", lambda _: "codex")
    monkeypatch.setattr(CodexClient, "create", lambda self, cwd, parent: creates.append((cwd, parent)) or SID)
    monkeypatch.setattr(app, "_spawn_session", lambda cwd, args, sid: launches.append((cwd, args, sid)) or "native")
    monkeypatch.setattr(app, "_show_new_session", lambda _: None)
    async with app.run_test() as pilot:
        await pilot.pause()
        worker = app._start_codex_worker(str(tmp_path), ["--cd", "project", "--model", "chosen"])
        await worker.wait()
        await pilot.pause()
        assert creates[-1] == (str(project.resolve()), "")
        assert launches[-1][1].count("-C") == 1 and "--cd" not in launches[-1][1]
        assert launches[-1][1][-2:] == ["--model", "chosen"]
        worker = app._start_codex_worker(str(project), ["fork", SID])
        await worker.wait()
        await pilot.pause()
        assert creates[-1] == (str(project), SID)


def test_malformed_deleted_search_file_is_skipped(tmp_path):
    path = rollout(tmp_path, message("user", "needle"))
    entries = codex_data.discover_sessions(tmp_path)
    path.unlink()
    assert search_all_sessions(tmp_path, "needle", sessions=entries) == []


@pytest.mark.parametrize("payload", [
    {"type": "reasoning", "summary": []},
    {"type": "custom_tool_call_output", "call_id": "outside-tail", "output": "still going"},
    {"type": "message", "role": "assistant", "phase": "commentary", "content": [{"text": "Checking next"}]},
])
def test_tail_activity_supersedes_completed_head(tmp_path, payload):
    # A bounded read may retain an older completed turn in the head while
    # omitting the new turn's start event in the middle.
    path = rollout(tmp_path, message("assistant", "Old final", phase="final_answer"),
                   rec("response_item", payload))
    parsed = codex_data.parse_session_file(path)
    assert derive_state(parsed, TrackedSession(KEY, "/proj", TS), True, now=NOW + 100)[0] == SessionState.WORKING


def test_claude_cannot_claim_a_codex_process(tmp_path):
    tracked = TrackedSession(SID, "/proj", TS)
    parsed = ParsedSession(SID, tmp_path / "x", cwd="/proj", mtime=100)
    pane = TmuxSession("native", 90, 100, False, 1, "/proj", pane_command="codex")
    assert map_tmux_sessions([(tracked, parsed)], [pane]) == {}


@pytest.mark.asyncio
async def test_codex_handoff_keeps_provider_and_lineage(tmp_path, monkeypatch):
    root = tmp_path / "codex"
    path = rollout(root, message("user", "Original task"), message("assistant", "Finished", phase="final_answer"))
    store = Store(tmp_path / "state.json")
    tracked = store.track(KEY, str(tmp_path), TS)
    view = SessionView(KEY, tracked, codex_data.parse_session_file(path), SessionState.NEEDS_REVIEW, False)
    app = CagentsApp(store=store, tmux=FakeTmux(), claude_dir=tmp_path / "claude", codex_dir=root)
    child_id = "11234567-0123-4567-8901-012345678901"
    launches, sends = [], []
    monkeypatch.setattr(app, "_agent_bin", lambda _: "codex")
    monkeypatch.setattr(CodexClient, "create", lambda self, cwd: child_id)
    monkeypatch.setattr(app, "_spawn_session", lambda cwd, args, sid: launches.append((args, sid)) or "native")
    monkeypatch.setattr(app, "_show_new_session", lambda _: None)
    monkeypatch.setattr(app, "_send_prompt_later", lambda *args: sends.append(args))
    async with app.run_test() as pilot:
        await pilot.pause()
        app.snapshot = Snapshot([view])
        runner = app._handoff_runner(KEY)
        assert "Original task" in runner.context and "Finished" in runner.context
        worker = app._codex_handoff_worker(view, "Next task", "The handoff spec")
        await worker.wait()
        await pilot.pause()
        child = store.sessions["codex:" + child_id]
        assert (child.provider, child.parent_id, child.relation) == ("codex", KEY, "handoff")
        assert launches[-1][0][:2] == ["resume", child_id]
        assert "The handoff spec" in sends[-1][1] and "Next task" in sends[-1][1]
        assert store.sessions[KEY].reviewed_at


def test_codex_notifications_do_not_replace_other_threads(tmp_path, monkeypatch):
    from cagents.notifier import notify_desktop
    calls = []
    monkeypatch.setattr(subprocess, "run", lambda args, **kwargs: calls.append(args))
    for sid in (KEY, KEY[:-1] + "2"):
        notify_desktop("cagents", "Ready for review", sid, tmp_path, tn_bin="terminal-notifier")
    assert calls[0][calls[0].index("-group") + 1] != calls[1][calls[1].index("-group") + 1]
