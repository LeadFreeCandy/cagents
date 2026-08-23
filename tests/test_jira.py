"""Optional Jira integration: key extraction, the HTTP layer (injected,
never hits a real network), the store roundtrip, list-row rendering, and
the end-to-end poller wired through the app."""

from __future__ import annotations

from pathlib import Path

import pytest

from cagents import gitops, jira
from cagents.format import JIRA_ASSIGNEE_WIDTH, JIRA_KEY_WIDTH, JIRA_STATUS_WIDTH, jira_header, session_row
from cagents.sessions import SessionState, SessionView
from cagents.store import Store, TrackedSession

from conftest import SID1, TranscriptBuilder


# --------------------------------------------------------------- jira.py ---


def test_extract_jira_key_prefers_title_then_body_then_branch():
    assert jira.extract_jira_key("Fix OWNER-721 login bug", "", "") == "OWNER-721"
    assert jira.extract_jira_key("no key here", "see OWNER-42 for context", "") == "OWNER-42"
    assert jira.extract_jira_key("", "", "owner-99-fix-login") == ""  # lowercase doesn't match
    assert jira.extract_jira_key("", "", "OWNER-99-fix-login") == "OWNER-99"
    assert jira.extract_jira_key("nothing", "nothing", "nothing") == ""


def test_credentials_configured_requires_all_three(monkeypatch):
    monkeypatch.delenv("JIRA_SITE", raising=False)
    monkeypatch.delenv("JIRA_EMAIL", raising=False)
    monkeypatch.delenv("JIRA_API_TOKEN", raising=False)
    assert jira.credentials_configured() is False
    monkeypatch.setenv("JIRA_SITE", "team.atlassian.net")
    monkeypatch.setenv("JIRA_EMAIL", "me@team.com")
    assert jira.credentials_configured() is False  # token still missing
    monkeypatch.setenv("JIRA_API_TOKEN", "tok")
    assert jira.credentials_configured() is True


def test_browse_url(monkeypatch):
    monkeypatch.setenv("JIRA_SITE", "team.atlassian.net")
    assert jira.browse_url("OWNER-721") == "https://team.atlassian.net/browse/OWNER-721"
    assert jira.browse_url("") == ""
    monkeypatch.delenv("JIRA_SITE", raising=False)
    assert jira.browse_url("OWNER-721") == ""


def test_fetch_issue_without_credentials_raises(monkeypatch):
    monkeypatch.delenv("JIRA_SITE", raising=False)
    with pytest.raises(jira.JiraError):
        jira.fetch_issue("OWNER-721")


def test_fetch_issue_parses_status_and_assignee(monkeypatch):
    monkeypatch.setenv("JIRA_SITE", "team.atlassian.net")
    monkeypatch.setenv("JIRA_EMAIL", "me@team.com")
    monkeypatch.setenv("JIRA_API_TOKEN", "tok")

    def fake_fetch(url, email, token):
        assert "OWNER-721" in url
        assert email == "me@team.com" and token == "tok"
        return {
            "fields": {
                "status": {"name": "In Review"},
                "assignee": {"displayName": "Jamie Rivera"},
            }
        }

    issue = jira.fetch_issue("OWNER-721", fetch=fake_fetch)
    assert issue.key == "OWNER-721"
    assert issue.status == "In Review"
    assert issue.assignee == "Jamie Rivera"
    assert issue.url == "https://team.atlassian.net/browse/OWNER-721"


def test_fetch_issue_unassigned_and_transport_failure(monkeypatch):
    monkeypatch.setenv("JIRA_SITE", "team.atlassian.net")
    monkeypatch.setenv("JIRA_EMAIL", "me@team.com")
    monkeypatch.setenv("JIRA_API_TOKEN", "tok")

    unassigned = jira.fetch_issue(
        "OWNER-1", fetch=lambda *a: {"fields": {"status": {"name": "Ready for dev"}, "assignee": None}}
    )
    assert unassigned.assignee == ""

    def boom(*a):
        raise TimeoutError("network unreachable")

    with pytest.raises(jira.JiraError):
        jira.fetch_issue("OWNER-1", fetch=boom)


# ------------------------------------------------------------- gitops.py ---


def test_pr_jira_sources_uses_the_pr_url_not_a_directory():
    calls = []

    def runner(args, cwd=None):
        calls.append((args, cwd))
        return '{"title": "OWNER-721: fix login", "body": "", "headRefName": "fix-login"}'

    title, body, branch = gitops.pr_jira_sources("https://github.com/o/r/pull/9", runner=runner)
    assert title == "OWNER-721: fix login"
    assert branch == "fix-login"
    args, cwd = calls[0]
    assert "https://github.com/o/r/pull/9" in args
    assert cwd is None  # explicit PR URL, never a directory-dependent lookup


def test_pr_jira_sources_swallows_failures():
    def runner(args, cwd=None):
        raise RuntimeError("gh not authenticated")

    assert gitops.pr_jira_sources("https://x/pull/1", runner=runner) == ("", "", "")


# --------------------------------------------------------------- store.py ---


def test_jira_info_roundtrip(tmp_path: Path):
    path = tmp_path / "state.json"
    store = Store.load(path)
    store.track(SID1, "/proj/a", "2026-08-18T09:00:00+00:00")
    store.set_jira_info(SID1, "OWNER-721", "In Review", "Jamie Rivera", "2026-08-18T11:00:00+00:00")
    got = Store.load(path).sessions[SID1]
    assert got.jira_key == "OWNER-721"
    assert got.jira_status == "In Review"
    assert got.jira_assignee == "Jamie Rivera"
    assert got.jira_checked_at == "2026-08-18T11:00:00+00:00"


def test_jira_integration_defaults_off():
    store = Store.load(Path("/nonexistent/does-not-exist.json"))
    assert store.get_setting("jira_integration") is False


# --------------------------------------------------------------- format.py ---


def _view_with_jira(key: str = "", status: str = "", assignee: str = "") -> SessionView:
    tracked = TrackedSession(
        SID1, "/proj/a", "2026-08-18T09:00:00+00:00",
        jira_key=key, jira_status=status, jira_assignee=assignee,
    )
    return SessionView(session_id=SID1, tracked=tracked, parsed=None, state=SessionState.NEEDS_REVIEW, live=False)


def test_session_row_jira_columns_align_with_header():
    view = _view_with_jira("OWNER-721", "In Review", "Jamie Rivera")
    row = session_row(view, show_jira=True)
    header = jira_header()
    plain_row, plain_header = row.plain, header.plain
    assert "OWNER-721" in plain_row
    assert "In Review" in plain_row
    assert "Jamie Rivera" in plain_row
    # the KEY/STATUS/ASSIGNEE titles land at the same column offsets as the
    # values they label
    key_col = plain_header.index("JIRA")
    assert plain_row[key_col : key_col + len("OWNER-721")] == "OWNER-721"


def test_session_row_without_jira_key_shows_placeholder():
    view = _view_with_jira()
    row = session_row(view, show_jira=True)
    assert row.plain.count("—") >= 3  # key, status, assignee all blank


def test_jira_key_style_is_muted_not_bright():
    # Must read as calmer than an urgent state color like NEEDS_INPUT's
    # bold red — it's an identifier, not an alert.
    view = _view_with_jira("OWNER-721", "In Review", "Jamie Rivera")
    row = session_row(view, show_jira=True)
    key_start = row.plain.index("OWNER-721")
    style = next(s.style for s in row.spans if s.start <= key_start < s.end)
    assert "bold" not in style
    assert "dim" in style


def test_session_row_omits_jira_columns_when_disabled():
    view = _view_with_jira("OWNER-721", "In Review", "Jamie Rivera")
    row = session_row(view, show_jira=False)
    assert "OWNER-721" not in row.plain


def test_jira_header_column_widths_match_session_row():
    header = jira_header()
    # sanity: the widths format.py exports are what jira_header actually used
    assert header.plain.rstrip().endswith("ASSIGNEE")
    assert JIRA_KEY_WIDTH > 0 and JIRA_STATUS_WIDTH > 0 and JIRA_ASSIGNEE_WIDTH > 0


# ---------------------------------------------------------- app-level ---


@pytest.fixture
def jira_world(claude_dir: Path, tmp_path: Path, now: float, monkeypatch):
    from cagents.app import CagentsApp
    from cagents.sessions import SessionRegistry

    monkeypatch.setenv("JIRA_SITE", "team.atlassian.net")
    monkeypatch.setenv("JIRA_EMAIL", "me@team.com")
    monkeypatch.setenv("JIRA_API_TOKEN", "tok")

    sid = "66666666-6666-6666-6666-666666666666"
    TranscriptBuilder(sid, "/proj/pr").ai_title("PR work").user("go").raw(
        {"type": "pr-link", "sessionId": sid, "prNumber": 9,
         "prUrl": "https://github.com/o/r/pull/9"}
    ).assistant_text("PR opened.").write(claude_dir, mtime=now - 600)
    store = Store.load(tmp_path / "state.json")
    store.track(sid, "/proj/pr", "2026-08-18T09:00:00+00:00")
    store.set_setting("jira_integration", True)

    from conftest import FakeTmux

    tmux = FakeTmux()
    registry = SessionRegistry(store, tmux=tmux, claude_dir=claude_dir)
    gh_runner = lambda args, cwd=None: (
        '{"title": "OWNER-721: fix login", "body": "", "headRefName": "fix-login"}'
    )
    jira_fetch = lambda url, email, token: {
        "fields": {"status": {"name": "In Review"}, "assignee": {"displayName": "Jamie Rivera"}}
    }
    app = CagentsApp(
        store=store, registry=registry, tmux=tmux, claude_dir=claude_dir,
        gh_runner=gh_runner, jira_fetch=jira_fetch,
    )
    return app, store, sid


async def test_jira_poller_resolves_key_status_and_assignee(jira_world):
    app, store, sid = jira_world
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        app._poll_jira_prs()
        await pilot.pause(0.3)
        tracked = store.sessions[sid]
        assert tracked.jira_key == "OWNER-721"
        assert tracked.jira_status == "In Review"
        assert tracked.jira_assignee == "Jamie Rivera"


async def test_jira_poller_self_heals_when_the_recorded_pr_changes(
    claude_dir: Path, tmp_path: Path, now: float, monkeypatch
):
    # Real bug, confirmed live: a session that incidentally links an
    # unrelated PR early on (before its real one exists) got that
    # unrelated PR's ticket cached as jira_key forever — the old poller
    # only ever derived the key once (`if tracked.jira_key: ... else:
    # derive`) and just re-polled that same key's status after that,
    # never noticing the session's recorded PR had since changed to its
    # real one. Must self-heal: re-derive from whatever PR is *currently*
    # recorded, every poll.
    from cagents.app import CagentsApp
    from cagents.sessions import SessionRegistry
    from conftest import FakeTmux

    monkeypatch.setenv("JIRA_SITE", "team.atlassian.net")
    monkeypatch.setenv("JIRA_EMAIL", "me@team.com")
    monkeypatch.setenv("JIRA_API_TOKEN", "tok")

    sid = "88888888-8888-8888-8888-888888888888"
    b = TranscriptBuilder(sid, "/proj/pr3").ai_title("Statements work").user("go").raw(
        {"type": "pr-link", "sessionId": sid, "prNumber": 122273,
         "prUrl": "https://github.com/o/r/pull/122273"}
    ).assistant_text("Looked at an unrelated PR in passing.")
    b.write(claude_dir, mtime=now - 600)
    store = Store.load(tmp_path / "state.json")
    store.track(sid, "/proj/pr3", "2026-08-18T09:00:00+00:00")
    store.set_setting("jira_integration", True)

    tmux = FakeTmux()
    registry = SessionRegistry(store, tmux=tmux, claude_dir=claude_dir)

    def gh_runner(args, cwd=None):
        if "pull/122273" in " ".join(args):
            return '{"title": "[OWNER-674] unrelated JSON serialization fix", "body": "", "headRefName": "owner-644"}'
        return '{"title": "OWNER-682: redesign Statements page", "body": "", "headRefName": "worktree-owner-682"}'

    jira_fetch = lambda url, email, token: {
        "fields": {"status": {"name": "In Review"}, "assignee": None}
    }
    app = CagentsApp(
        store=store, registry=registry, tmux=tmux, claude_dir=claude_dir,
        gh_runner=gh_runner, jira_fetch=jira_fetch,
    )
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        app._poll_jira_prs()
        await pilot.pause(0.3)
        assert store.sessions[sid].jira_key == "OWNER-674"  # the incidental one, for now

        # The session's real PR shows up later in the transcript.
        b.raw(
            {"type": "pr-link", "sessionId": sid, "prNumber": 121997,
             "prUrl": "https://github.com/o/r/pull/121997"}
        ).assistant_text("Opened the real PR for this work.", ts="2026-08-18T10:05:00.000Z")
        b.write(claude_dir, mtime=now - 60)

        app.refresh_data()
        await pilot.pause(0.3)
        app._poll_jira_prs()
        await pilot.pause(0.3)
        tracked = store.sessions[sid]
        assert tracked.jira_key == "OWNER-682"  # self-healed to the real one


async def test_jira_poller_backfills_when_no_pr_recorded_yet(claude_dir: Path, tmp_path: Path, now: float, monkeypatch):
    """No pr-link record, no manual/waiting association — the poller must
    actively look one up (like the 'w' fallback) and keep trying, not
    silently skip the session forever."""
    from cagents.app import CagentsApp
    from cagents.sessions import SessionRegistry

    monkeypatch.setenv("JIRA_SITE", "team.atlassian.net")
    monkeypatch.setenv("JIRA_EMAIL", "me@team.com")
    monkeypatch.setenv("JIRA_API_TOKEN", "tok")

    sid = "77777777-7777-7777-7777-777777777777"
    TranscriptBuilder(sid, "/proj/pr2").ai_title("Untracked PR work").user("go").assistant_text(
        "Opened a PR out of band."
    ).write(claude_dir, mtime=now - 600)
    store = Store.load(tmp_path / "state.json")
    store.track(sid, "/proj/pr2", "2026-08-18T09:00:00+00:00")
    store.set_setting("jira_integration", True)

    from conftest import FakeTmux

    tmux = FakeTmux()
    registry = SessionRegistry(store, tmux=tmux, claude_dir=claude_dir)

    def gh_runner(args, cwd=None):
        if "url" in args:  # find_pr_url's shape
            return "https://github.com/o/r/pull/77"
        return '{"title": "OWNER-77: backfilled", "body": "", "headRefName": "x"}'

    jira_fetch = lambda url, email, token: {
        "fields": {"status": {"name": "Ready for dev"}, "assignee": None}
    }
    app = CagentsApp(
        store=store, registry=registry, tmux=tmux, claude_dir=claude_dir,
        gh_runner=gh_runner, jira_fetch=jira_fetch,
    )
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        app._poll_jira_prs()
        await pilot.pause(0.3)
        tracked = store.sessions[sid]
        assert tracked.jira_key == "OWNER-77"
        assert tracked.jira_status == "Ready for dev"
        assert tracked.jira_assignee == ""


async def test_jira_poller_noops_when_setting_off(jira_world):
    app, store, sid = jira_world
    store.set_setting("jira_integration", False)
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        app._poll_jira_prs()
        await pilot.pause(0.2)
        assert store.sessions[sid].jira_key == ""


async def test_shift_o_opens_jira_card(jira_world, monkeypatch):
    app, store, sid = jira_world
    store.set_jira_info(sid, "OWNER-721", "In Review", "Jamie Rivera", "2026-08-18T11:00:00+00:00")
    opened = []
    monkeypatch.setattr("subprocess.run", lambda args, **kw: opened.append(args))
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        from conftest import select_session

        select_session(app, sid)
        await pilot.pause()
        await pilot.press("O")
        await pilot.pause(0.1)
        assert opened and opened[0] == ["open", "https://team.atlassian.net/browse/OWNER-721"]


async def test_shift_o_warns_when_no_card_linked(jira_world):
    app, store, sid = jira_world
    captured = []
    import textual.app as textual_app

    original = textual_app.App.notify

    def spy(self, message, **kwargs):
        captured.append((kwargs.get("severity", "information"), str(message)))

    textual_app.App.notify = spy
    try:
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            from conftest import select_session

            select_session(app, sid)
            await pilot.pause()
            await pilot.press("O")
            await pilot.pause(0.1)
    finally:
        textual_app.App.notify = original
    assert any(sev == "warning" for sev, _ in captured)


# ------------------------------------------------- settings-toggle wiring ---


async def test_enabling_jira_without_credentials_warns_and_does_not_poll(
    claude_dir: Path, tmp_path: Path, now: float, monkeypatch
):
    from cagents.app import CagentsApp
    from cagents.sessions import SessionRegistry

    monkeypatch.delenv("JIRA_SITE", raising=False)
    monkeypatch.delenv("JIRA_EMAIL", raising=False)
    monkeypatch.delenv("JIRA_API_TOKEN", raising=False)

    sid = "88888888-8888-8888-8888-888888888888"
    TranscriptBuilder(sid, "/proj/pr3").ai_title("Work").user("go").raw(
        {"type": "pr-link", "sessionId": sid, "prNumber": 5,
         "prUrl": "https://github.com/o/r/pull/5"}
    ).assistant_text("done").write(claude_dir, mtime=now - 600)
    store = Store.load(tmp_path / "state.json")
    store.track(sid, "/proj/pr3", "2026-08-18T09:00:00+00:00")

    from conftest import FakeTmux

    tmux = FakeTmux()
    registry = SessionRegistry(store, tmux=tmux, claude_dir=claude_dir)
    gh_runner = lambda args, cwd=None: (
        '{"title": "OWNER-5: fix", "body": "", "headRefName": "x"}'
    )
    app = CagentsApp(store=store, registry=registry, tmux=tmux, claude_dir=claude_dir, gh_runner=gh_runner)

    captured = []
    import textual.app as textual_app

    original = textual_app.App.notify

    def spy(self, message, **kwargs):
        captured.append((kwargs.get("severity", "information"), str(message)))

    textual_app.App.notify = spy
    try:
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            store.set_setting("jira_integration", True)
            app._setting_changed("jira_integration", True)
            await pilot.pause(0.3)
    finally:
        textual_app.App.notify = original
    assert any(sev == "warning" for sev, _ in captured)
    assert store.sessions[sid].jira_key == ""  # never polled: credentials missing


async def test_enabling_jira_with_credentials_polls_immediately(
    claude_dir: Path, tmp_path: Path, now: float, monkeypatch
):
    from cagents.app import CagentsApp
    from cagents.sessions import SessionRegistry

    monkeypatch.setenv("JIRA_SITE", "team.atlassian.net")
    monkeypatch.setenv("JIRA_EMAIL", "me@team.com")
    monkeypatch.setenv("JIRA_API_TOKEN", "tok")

    sid = "99999999-9999-9999-9999-999999999999"
    TranscriptBuilder(sid, "/proj/pr4").ai_title("Work").user("go").raw(
        {"type": "pr-link", "sessionId": sid, "prNumber": 6,
         "prUrl": "https://github.com/o/r/pull/6"}
    ).assistant_text("done").write(claude_dir, mtime=now - 600)
    store = Store.load(tmp_path / "state.json")
    store.track(sid, "/proj/pr4", "2026-08-18T09:00:00+00:00")

    from conftest import FakeTmux

    tmux = FakeTmux()
    registry = SessionRegistry(store, tmux=tmux, claude_dir=claude_dir)
    gh_runner = lambda args, cwd=None: (
        '{"title": "OWNER-6: fix", "body": "", "headRefName": "x"}'
    )
    jira_fetch = lambda url, email, token: {
        "fields": {"status": {"name": "In Review"}, "assignee": {"displayName": "Sam"}}
    }
    app = CagentsApp(
        store=store, registry=registry, tmux=tmux, claude_dir=claude_dir,
        gh_runner=gh_runner, jira_fetch=jira_fetch,
    )
    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        store.set_setting("jira_integration", True)
        app._setting_changed("jira_integration", True)
        await pilot.pause(0.3)
        assert store.sessions[sid].jira_key == "OWNER-6"
