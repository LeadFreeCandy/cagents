"""`o` / `O` on a session with nothing linked yet.

Driven through the real keypress: the app scans the conversation, ranks
what it finds, and the pick is what gets stored and opened.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from conftest import SID1, SID2, FakeTmux, TranscriptBuilder, select_session, ts_ago

from cagents.app import CagentsApp
from cagents.modals import InputModal, LinkCandidateModal
from cagents.sessions import SessionRegistry
from cagents.store import Store


@pytest.fixture
def world(claude_dir: Path, tmp_path: Path, now: float):
    """SID1 mentions three PRs and two Jira keys, none of them linked:
    it opened its own PR with `gh pr create` (whose URL only ever lands
    in tool output, not in a pr-link record), was pointed at an earlier
    attempt, and noticed an unrelated one along the way. SID2 mentions
    nothing."""
    TranscriptBuilder(SID1, "/proj/alpha", git_branch="samir/OWNER-663-timeout").ai_title(
        "Alpha: owner statement timeout"
    ).user(
        "pick up OWNER-663 — see https://github.com/o/alpha/pull/41 for the earlier attempt"
    ).assistant_tool_use(
        "t1", "Bash", {"command": "gh pr create --fill"}
    ).raw_tool_result(
        "t1", "https://github.com/o/alpha/pull/42"
    ).assistant_text(
        "OWNER-112 is adjacent; also https://github.com/o/other/pull/99 is unrelated"
    ).write(claude_dir, mtime=now - 900)
    TranscriptBuilder(SID2, "/proj/alpha").ai_title("Alpha: no links").user("go").assistant_text(
        "nothing to link here"
    ).write(claude_dir, mtime=now - 800)

    store = Store.load(tmp_path / "state.json")
    store.track(SID1, "/proj/alpha", "2026-08-18T09:00:00+00:00")
    store.track(SID2, "/proj/alpha", "2026-08-18T09:10:00+00:00")
    for tracked in store.sessions.values():
        tracked.last_interacted_at = ts_ago(3600)

    tmux = FakeTmux()
    registry = SessionRegistry(store, tmux=tmux, claude_dir=claude_dir)
    app = CagentsApp(registry=registry, store=store, tmux=tmux, sidecar=None)
    return app, store, tmux


class TestPRCandidates:
    async def test_o_ranks_the_prs_the_conversation_mentions(self, world):
        app, store, _ = world
        opened: list[str] = []
        app._open_url = lambda url, label: opened.append(url)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            select_session(app, SID1)
            await pilot.pause()
            await pilot.press("o")
            await pilot.pause()
            assert isinstance(app.screen, LinkCandidateModal)
            # The PR Claude actually opened leads; the ones merely mentioned follow.
            assert [c.value for c in app.screen.candidates] == [
                "https://github.com/o/alpha/pull/42",
                "https://github.com/o/alpha/pull/41",
                "https://github.com/o/other/pull/99",
            ]
            from textual.widgets import OptionList

            listing = app.screen.query_one("#candidate-list", OptionList)
            top = listing.get_option_at_index(0).prompt.plain
            assert "PR #42" in top and "%" in top  # likelihood is shown

    async def test_choosing_a_candidate_stores_and_opens_it(self, world):
        app, store, _ = world
        opened: list[str] = []
        app._open_url = lambda url, label: opened.append(url)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            select_session(app, SID1)
            await pilot.pause()
            await pilot.press("o")
            await pilot.pause()
            await pilot.press("enter")  # top candidate is preselected
            await pilot.pause(0.2)
            assert store.sessions[SID1].pr_url == "https://github.com/o/alpha/pull/42"
            assert opened == ["https://github.com/o/alpha/pull/42"]
            # Linked now, so o goes straight there — no second prompt.
            await pilot.press("o")
            await pilot.pause()
            assert not isinstance(app.screen, LinkCandidateModal)

    async def test_none_of_these_falls_through_to_pasting_one(self, world):
        app, store, _ = world
        app._open_url = lambda url, label: None
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            select_session(app, SID1)
            await pilot.pause()
            await pilot.press("o")
            await pilot.pause()
            await pilot.press("end", "enter")  # last row: none of these
            await pilot.pause(0.2)
            assert isinstance(app.screen, InputModal)
            assert not store.sessions[SID1].pr_url

    async def test_a_conversation_with_no_pr_still_prompts_to_paste(self, world):
        app, store, _ = world
        app._open_url = lambda url, label: None
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            select_session(app, SID2)
            await pilot.pause()
            await pilot.press("o")
            await pilot.pause()
            assert isinstance(app.screen, InputModal)


class TestJiraCandidates:
    async def test_O_ranks_the_cards_the_conversation_mentions(self, world):
        app, store, _ = world
        opened: list[str] = []
        app._open_url = lambda url, label: opened.append(url)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            select_session(app, SID1)
            await pilot.pause()
            await pilot.press("O")
            await pilot.pause()
            assert isinstance(app.screen, LinkCandidateModal)
            assert [c.value for c in app.screen.candidates] == ["OWNER-663", "OWNER-112"]

    async def test_choosing_a_card_pins_it_against_the_poller(self, world, monkeypatch):
        """The Jira poller re-derives every key from the PR each cycle. A
        card the human picked by hand has to survive that."""
        app, store, _ = world
        monkeypatch.setenv("JIRA_SITE", "team.atlassian.net")
        monkeypatch.setenv("JIRA_EMAIL", "a@b.c")
        monkeypatch.setenv("JIRA_API_TOKEN", "t")
        opened: list[str] = []
        app._open_url = lambda url, label: opened.append(url)
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            select_session(app, SID1)
            await pilot.pause()
            await pilot.press("O")
            await pilot.pause()
            await pilot.press("enter")
            await pilot.pause(0.2)
            assert store.sessions[SID1].jira_key == "OWNER-663"
            assert opened == ["https://team.atlassian.net/browse/OWNER-663"]

            # A poll that finds no PR to derive from must not clear the pin.
            store.set_setting("jira_integration", True)
            app.gh_runner = lambda args, cwd=None: ""
            app._poll_jira_worker(list(app.snapshot.views))
            await pilot.pause(0.3)
            assert store.sessions[SID1].jira_key == "OWNER-663"

    async def test_undo_takes_back_a_wrong_pick(self, world, monkeypatch):
        """`O` on a linked card opens it and stops offering the picker, so
        z is the only way back from picking the wrong one."""
        app, store, _ = world
        monkeypatch.setenv("JIRA_SITE", "team.atlassian.net")
        app._open_url = lambda url, label: None
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            select_session(app, SID1)
            await pilot.pause()
            await pilot.press("O")
            await pilot.pause()
            await pilot.press("down", "enter")  # the wrong one
            await pilot.pause(0.2)
            assert store.sessions[SID1].jira_key == "OWNER-112"
            await pilot.press("z")
            await pilot.pause(0.2)
            assert not store.sessions[SID1].jira_key
            assert not store.sessions[SID1].jira_pinned
            # ...and the picker comes back.
            await pilot.press("O")
            await pilot.pause()
            assert isinstance(app.screen, LinkCandidateModal)

    async def test_a_conversation_with_no_card_still_warns(self, world):
        app, store, _ = world
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            select_session(app, SID2)
            await pilot.pause()
            await pilot.press("O")
            await pilot.pause()
            assert not isinstance(app.screen, LinkCandidateModal)
            assert not store.sessions[SID2].jira_key
