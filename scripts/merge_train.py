#!/usr/bin/env python3
# ruff: noqa: T201
"""Keep pull requests up to date with latest, one at a time, so auto-merge can land them.

latest requires a pull request to be up to date before it merges, and GitHub's merge
queue is not available on a personal repository. Auto-merge alone then stalls: after
any merge every other waiting PR is behind and nothing updates it. This script is the
queue. Among open PRs that have auto-merge switched on (the owner's approval), it
walks them in the order auto-merge was enabled and acts on the first one that can
still merge:

- behind latest: merge latest into it (this restarts CI, and auto-merge lands it),
- up to date: do nothing, it is the one in flight and the others wait their turn.

Only one PR is ever updated at a time, because each merge makes every other PR stale
again and their CI runs would be wasted. A PR that cannot merge as it is (conflict,
failed or hung required check, branch GitHub refuses to update) is skipped so it
cannot block the queue; its author has to fix it.

Usage: merge_train.py [--dry-run]    (needs GH_TOKEN and GH_REPO)
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import UTC, datetime, timedelta

REQUIRED_CHECKS = {"lint", "test (3.12)"}  # keep in sync with the ruleset on latest
FAILED = {"FAILURE", "TIMED_OUT", "CANCELLED", "ACTION_REQUIRED", "STARTUP_FAILURE"}
HUNG_AFTER = timedelta(hours=2)  # a required check this old and still pending is stuck

QUERY = """
query($owner: String!, $name: String!) {
  repository(owner: $owner, name: $name) {
    pullRequests(states: OPEN, baseRefName: "latest", first: 100) {
      nodes {
        number
        isDraft
        mergeStateStatus
        autoMergeRequest { enabledAt }
        commits(last: 1) { nodes { commit { statusCheckRollup { contexts(first: 100) {
          nodes { ... on CheckRun { name status conclusion startedAt } }
        } } } } }
      }
    }
  }
}
"""


def _checks(pr: dict) -> list[dict]:
    commit = pr["commits"]["nodes"][0]["commit"]
    rollup = commit["statusCheckRollup"] or {"contexts": {"nodes": []}}
    return [c for c in rollup["contexts"]["nodes"] if c.get("name") in REQUIRED_CHECKS]


def stuck_reason(pr: dict, now: datetime) -> str | None:
    """Return why this PR cannot merge as it is, or None when it can."""
    if pr["mergeStateStatus"] == "DIRTY":
        return "merge conflict with latest"
    for check in _checks(pr):
        if check["conclusion"] in FAILED:
            return f"required check '{check['name']}' failed"
        if check["status"] != "COMPLETED" and check["startedAt"]:
            started = datetime.fromisoformat(check["startedAt"])
            if now - started > HUNG_AFTER:
                return f"required check '{check['name']}' has been pending for hours"
    return None


def choose(prs: list[dict], now: datetime) -> tuple[str, int | None, list[str]]:
    """Return (action, pr number, notes): action is 'update', 'wait' or 'idle'."""
    queue = sorted(
        (p for p in prs if not p["isDraft"] and p["autoMergeRequest"]),
        key=lambda p: p["autoMergeRequest"]["enabledAt"],
    )
    notes = []
    for pr in queue:
        reason = stuck_reason(pr, now)
        if reason:
            notes.append(f"#{pr['number']} skipped: {reason}")
        elif pr["mergeStateStatus"] == "BEHIND":
            return "update", pr["number"], notes
        else:
            return "wait", pr["number"], notes
    return "idle", None, notes


def _gh(*args: str) -> str:
    return subprocess.run(  # noqa: S603
        ["gh", *args],  # noqa: S607
        capture_output=True,
        text=True,
        check=True,
    ).stdout


def main(argv: list[str]) -> int:
    """Fetch the open PRs, pick one, and update its branch unless this is a dry run."""
    dry_run = "--dry-run" in argv
    owner, name = os.environ["GH_REPO"].split("/")
    prs = json.loads(
        _gh(
            "api",
            "graphql",
            "-f",
            f"owner={owner}",
            "-f",
            f"name={name}",
            "-f",
            f"query={QUERY}",
        )
    )["data"]["repository"]["pullRequests"]["nodes"]
    while True:
        action, number, notes = choose(prs, datetime.now(UTC))
        print("\n".join(notes))
        if action != "update":
            print(
                f"{action}: " + (f"#{number} is in flight" if number else "queue empty")
            )
            return 0
        if dry_run:
            print(f"dry run: would update the branch of #{number}")
            return 0
        try:
            _gh(
                "api", "-X", "PUT", f"repos/{owner}/{name}/pulls/{number}/update-branch"
            )
        except subprocess.CalledProcessError as error:
            print(
                f"#{number} skipped: GitHub refused to update it: {error.stderr.strip()}"
            )
            prs = [p for p in prs if p["number"] != number]
            continue
        print(f"updated the branch of #{number}")
        return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
