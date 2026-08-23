"""Optional Jira integration (off by default — spec: `jira_integration`
setting).

For a session's linked PR, resolve the Jira card mentioned in its title,
body, or branch name (however the team links PRs to cards — no Jira-side
"linked issues" API call needed) and fetch its current board column and
assignee. Credentials come from the environment, never from cagents' own
store:

    JIRA_SITE        e.g. "yourteam.atlassian.net"
    JIRA_EMAIL       the account email for the API token
    JIRA_API_TOKEN   an Atlassian API token

Deliberately shallow, like gitops.py: plain HTTP, no state, loud failures
that callers treat as transient (skip this poll, retry next time).
"""

from __future__ import annotations

import base64
import json
import os
import re
import urllib.request
from dataclasses import dataclass

_KEY_RE = re.compile(r"\b([A-Z][A-Z0-9]{1,9}-\d+)\b")


class JiraError(RuntimeError):
    pass


def extract_jira_key(*texts: str) -> str:
    """First JIRA-style key (e.g. OWNER-721) found, checked in the given
    order — callers pass title, then body, then branch name so the most
    deliberate link (the title) wins over an incidental match."""
    for text in texts:
        if not text:
            continue
        match = _KEY_RE.search(text)
        if match:
            return match.group(1)
    return ""


def credentials_configured() -> bool:
    return bool(
        os.environ.get("JIRA_SITE") and os.environ.get("JIRA_EMAIL") and os.environ.get("JIRA_API_TOKEN")
    )


def browse_url(key: str) -> str:
    site = os.environ.get("JIRA_SITE", "").strip().rstrip("/")
    if not site or not key:
        return ""
    return f"https://{site}/browse/{key}"


@dataclass
class JiraIssue:
    key: str
    status: str = ""  # board column, e.g. "In Review"
    assignee: str = ""  # display name, "" if unassigned
    url: str = ""


def _default_fetch(url: str, email: str, token: str, timeout: float = 15.0) -> dict:
    auth = base64.b64encode(f"{email}:{token}".encode("utf-8")).decode("ascii")
    request = urllib.request.Request(
        url, headers={"Authorization": f"Basic {auth}", "Accept": "application/json"}
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def fetch_issue(key: str, fetch=None) -> JiraIssue:
    """The card's current status and assignee. Raises JiraError if
    credentials aren't configured or the lookup fails — callers should
    treat this exactly like a transient `gh` failure."""
    site = os.environ.get("JIRA_SITE", "").strip().rstrip("/")
    email = os.environ.get("JIRA_EMAIL", "").strip()
    token = os.environ.get("JIRA_API_TOKEN", "").strip()
    if not (site and email and token):
        raise JiraError("Jira credentials not configured (JIRA_SITE / JIRA_EMAIL / JIRA_API_TOKEN)")
    fetch = fetch or _default_fetch
    url = f"https://{site}/rest/api/3/issue/{key}?fields=status,assignee"
    try:
        data = fetch(url, email, token)
    except Exception as error:
        raise JiraError(f"Jira lookup for {key} failed: {error}") from error
    fields = data.get("fields") or {}
    status = str((fields.get("status") or {}).get("name", ""))
    assignee_field = fields.get("assignee") or {}
    assignee = str(assignee_field.get("displayName", "")) if assignee_field else ""
    return JiraIssue(key=key, status=status, assignee=assignee, url=browse_url(key))
