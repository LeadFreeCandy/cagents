"""Ranking the PR / Jira candidates a conversation mentions.

When nothing is linked yet, `o` and `O` fall back to whatever the
transcript itself mentions. These tests pin down which mention wins and
why — the scoring is the whole feature, so it is tested directly rather
than only through the keypress.
"""

from __future__ import annotations

from pathlib import Path

from conftest import SID1, SID2, TranscriptBuilder

from cagents import linkscan


def _write(builder: TranscriptBuilder, claude_dir: Path) -> Path:
    return builder.write(claude_dir)


class TestPRCandidates:
    def test_none_when_the_conversation_mentions_no_pr(self, claude_dir: Path):
        path = _write(
            TranscriptBuilder(SID1, "/proj/alpha").user("go").assistant_text("done"), claude_dir
        )
        assert linkscan.pr_candidates(path) == []

    def test_a_recorded_pr_link_outranks_one_merely_mentioned(self, claude_dir: Path):
        path = _write(
            TranscriptBuilder(SID1, "/proj/alpha")
            .user("look at https://github.com/o/r/pull/11 for context")
            .assistant_text("reading it")
            .raw({"type": "pr-link", "prNumber": 12, "prUrl": "https://github.com/o/r/pull/12"}),
            claude_dir,
        )
        candidates = linkscan.pr_candidates(path)
        assert [c.value for c in candidates] == [
            "https://github.com/o/r/pull/12",
            "https://github.com/o/r/pull/11",
        ]
        assert candidates[0].likelihood > candidates[1].likelihood
        assert any("opened" in reason for reason in candidates[0].reasons)

    def test_the_pr_gh_pr_create_printed_outranks_one_you_linked_for_context(
        self, claude_dir: Path
    ):
        """`gh pr create` run as a Bash call leaves no pr-link record —
        the session's own PR exists only as that command's output."""
        path = _write(
            TranscriptBuilder(SID1, "/proj/alpha")
            .user("continue from https://github.com/o/r/pull/41, the earlier attempt")
            .assistant_tool_use("t1", "Bash", {"command": "gh pr create --fill"})
            .raw_tool_result("t1", "https://github.com/o/r/pull/42"),
            claude_dir,
        )
        candidates = linkscan.pr_candidates(path)
        assert candidates[0].value == "https://github.com/o/r/pull/42"
        assert any("created" in reason for reason in candidates[0].reasons)

    def test_output_unrelated_to_creating_a_pr_stays_a_weak_signal(self, claude_dir: Path):
        path = _write(
            TranscriptBuilder(SID1, "/proj/alpha")
            .user("continue from https://github.com/o/r/pull/41, the earlier attempt")
            .assistant_tool_use("t1", "Bash", {"command": "gh pr list"})
            .raw_tool_result("t1", "https://github.com/o/r/pull/42"),
            claude_dir,
        )
        candidates = linkscan.pr_candidates(path)
        assert candidates[0].value == "https://github.com/o/r/pull/41"

    def test_a_pr_you_mentioned_outranks_one_only_claude_mentioned(self, claude_dir: Path):
        path = _write(
            TranscriptBuilder(SID1, "/proj/alpha")
            .user("continue the work on https://github.com/o/r/pull/20")
            .assistant_text("also see https://github.com/o/r/pull/99 which is unrelated"),
            claude_dir,
        )
        candidates = linkscan.pr_candidates(path)
        assert candidates[0].value == "https://github.com/o/r/pull/20"

    def test_the_repo_matching_this_session_is_preferred(self, claude_dir: Path):
        path = _write(
            TranscriptBuilder(SID1, "/proj/alpha")
            .assistant_text("compare https://github.com/o/other/pull/3")
            .assistant_text("and https://github.com/o/alpha/pull/4"),
            claude_dir,
        )
        candidates = linkscan.pr_candidates(path, project_dir="/proj/alpha")
        assert candidates[0].value == "https://github.com/o/alpha/pull/4"
        assert any("same repo" in reason for reason in candidates[0].reasons)

    def test_urls_are_normalized_and_deduped(self, claude_dir: Path):
        path = _write(
            TranscriptBuilder(SID1, "/proj/alpha")
            .user("see https://github.com/o/r/pull/7/files and https://github.com/o/r/pull/7.")
            .assistant_text("https://github.com/o/r/pull/7#issuecomment-12345"),
            claude_dir,
        )
        candidates = linkscan.pr_candidates(path)
        assert [c.value for c in candidates] == ["https://github.com/o/r/pull/7"]
        assert candidates[0].label == "PR #7"

    def test_prs_that_reference_the_linked_card_lead(self, claude_dir: Path):
        """GitHub said these reference the session's card, which beats
        anything the transcript merely happens to mention."""
        path = _write(
            TranscriptBuilder(SID1, "/proj/alpha").user(
                "compare against https://github.com/o/alpha/pull/3"
            ),
            claude_dir,
        )
        candidates = linkscan.pr_candidates(
            path,
            project_dir="/proj/alpha",
            card_key="OWNER-663",
            card_prs=["https://github.com/o/alpha/pull/8"],
        )
        assert candidates[0].value == "https://github.com/o/alpha/pull/8"
        assert any("OWNER-663" in reason for reason in candidates[0].reasons)

    def test_a_card_match_the_transcript_also_mentions_adds_up(self, claude_dir: Path):
        path = _write(
            TranscriptBuilder(SID1, "/proj/alpha").user("working on https://github.com/o/alpha/pull/8"),
            claude_dir,
        )
        both, card_only = linkscan.pr_candidates(
            path, card_key="OWNER-663", card_prs=["https://github.com/o/alpha/pull/8"]
        ), linkscan.pr_candidates(
            _write(TranscriptBuilder(SID2, "/proj/alpha").user("go"), claude_dir),
            card_key="OWNER-663",
            card_prs=["https://github.com/o/alpha/pull/8"],
        )
        assert both[0].weight > card_only[0].weight  # corroboration, not a duplicate row
        assert len(both) == 1

    def test_likelihood_leaves_room_for_none_of_these(self, claude_dir: Path):
        """A single weak mention must not read as a certainty."""
        path = _write(
            TranscriptBuilder(SID1, "/proj/alpha").assistant_text(
                "unrelated: https://github.com/o/r/pull/500"
            ),
            claude_dir,
        )
        candidates = linkscan.pr_candidates(path)
        assert 0.0 < candidates[0].likelihood < 0.5
        assert sum(c.likelihood for c in linkscan.pr_candidates(path)) < 1.0


class TestJiraCandidates:
    def test_none_when_the_conversation_mentions_no_card(self, claude_dir: Path):
        path = _write(
            TranscriptBuilder(SID1, "/proj/alpha").user("go").assistant_text("done"), claude_dir
        )
        assert linkscan.jira_candidates(path) == []

    def test_the_branch_name_wins_over_a_passing_mention(self, claude_dir: Path):
        path = _write(
            TranscriptBuilder(SID1, "/proj/alpha", git_branch="samir/OWNER-663-timeout")
            .user("fix the timeout")
            .assistant_text("this looks related to OWNER-112 as well"),
            claude_dir,
        )
        candidates = linkscan.jira_candidates(path, branch="samir/OWNER-663-timeout")
        assert [c.value for c in candidates] == ["OWNER-663", "OWNER-112"]
        assert any("branch" in reason for reason in candidates[0].reasons)

    def test_the_opening_request_outranks_later_chatter(self, claude_dir: Path):
        path = _write(
            TranscriptBuilder(SID1, "/proj/alpha")
            .user("pick up OWNER-721 please")
            .assistant_text("OWNER-999 touches the same file")
            .assistant_text("and OWNER-999 again"),
            claude_dir,
        )
        candidates = linkscan.jira_candidates(path)
        assert candidates[0].value == "OWNER-721"
        assert any("opening request" in reason for reason in candidates[0].reasons)

    def test_common_non_ticket_tokens_are_not_candidates(self, claude_dir: Path):
        """UTF-8, SHA-256 and friends match a bare JIRA-key regex."""
        path = _write(
            TranscriptBuilder(SID1, "/proj/alpha")
            .user("decode it as UTF-8 and check the SHA-256, see RFC-2822 and CVE-2024")
            .assistant_text("done"),
            claude_dir,
        )
        assert linkscan.jira_candidates(path) == []

    def test_a_real_key_survives_alongside_the_noise(self, claude_dir: Path):
        path = _write(
            TranscriptBuilder(SID1, "/proj/alpha")
            .user("OWNER-663: the file is UTF-8 encoded")
            .assistant_text("done"),
            claude_dir,
        )
        assert [c.value for c in linkscan.jira_candidates(path)] == ["OWNER-663"]
