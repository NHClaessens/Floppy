"""Tests for scripts/merge_train.py, the queue that keeps approved PRs up to date.

``scripts/`` is not a package, so the script is loaded by path, as in
test_migration_order.
"""

import importlib.util
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest import TestCase

_SCRIPT = Path(__file__).resolve().parents[3] / "scripts" / "merge_train.py"
_spec = importlib.util.spec_from_file_location("merge_train", _SCRIPT)
train = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(train)

NOW = datetime(2026, 10, 5, 12, 0, tzinfo=UTC)


def pr(number, enabled="2026-10-05T01:00:00Z", state="BEHIND", draft=False, checks=()):
    """Build a PR as the GraphQL query returns it (enabled=None: no auto-merge)."""
    return {
        "number": number,
        "isDraft": draft,
        "mergeStateStatus": state,
        "autoMergeRequest": {"enabledAt": enabled} if enabled else None,
        "commits": {
            "nodes": [
                {
                    "commit": {
                        "statusCheckRollup": {
                            "contexts": {
                                "nodes": [
                                    {
                                        "name": n,
                                        "status": s,
                                        "conclusion": c,
                                        "startedAt": t,
                                    }
                                    for n, s, c, t in checks
                                ]
                            }
                        }
                    }
                }
            ]
        },
    }


class MergeTrainTests(TestCase):
    """One approved PR at a time is updated; the rest wait or are skipped."""

    def test_empty_queue_is_idle(self):
        """No PR with auto-merge on means there is nothing to do."""
        self.assertEqual(train.choose([], NOW)[0], "idle")

    def test_drafts_and_unapproved_prs_are_never_touched(self):
        """A PR without auto-merge (not approved) or a draft is ignored."""
        prs = [pr(1, enabled=None), pr(2, draft=True)]
        self.assertEqual(train.choose(prs, NOW), ("idle", None, []))

    def test_oldest_approval_goes_first(self):
        """The PR whose auto-merge was enabled first is updated, not a lower number."""
        prs = [
            pr(5, enabled="2026-10-05T03:00:00Z"),
            pr(9, enabled="2026-10-05T02:00:00Z"),
        ]
        self.assertEqual(train.choose(prs, NOW)[:2], ("update", 9))

    def test_up_to_date_front_pr_holds_the_line(self):
        """While the front PR runs CI, a behind PR behind it is not updated."""
        prs = [
            pr(1, state="BLOCKED", enabled="2026-10-05T01:00:00Z"),
            pr(2, state="BEHIND", enabled="2026-10-05T02:00:00Z"),
        ]
        self.assertEqual(train.choose(prs, NOW)[:2], ("wait", 1))

    def test_conflicted_pr_is_skipped(self):
        """A PR that conflicts with latest cannot block the PR after it."""
        prs = [
            pr(1, state="DIRTY"),
            pr(2, state="BEHIND", enabled="2026-10-05T02:00:00Z"),
        ]
        action, number, notes = train.choose(prs, NOW)
        self.assertEqual((action, number), ("update", 2))
        self.assertIn("#1 skipped: merge conflict", notes[0])

    def test_failed_required_check_is_skipped(self):
        """A red required check parks the PR instead of re-running its CI."""
        red = pr(
            1, state="BLOCKED", checks=[("test (3.12)", "COMPLETED", "FAILURE", None)]
        )
        _, number, notes = train.choose(
            [red, pr(2, enabled="2026-10-05T02:00:00Z")], NOW
        )
        self.assertEqual(number, 2)
        self.assertIn("'test (3.12)' failed", notes[0])

    def test_failed_optional_check_does_not_block(self):
        """Only the required checks count; CodeQL or the image build do not."""
        ok = pr(1, state="BLOCKED", checks=[("build", "COMPLETED", "FAILURE", None)])
        self.assertEqual(train.choose([ok], NOW)[:2], ("wait", 1))

    def test_hung_required_check_is_skipped(self):
        """A required check pending for hours is treated as stuck."""
        started = (NOW - timedelta(hours=3)).isoformat()
        hung = pr(
            1, state="BLOCKED", checks=[("test (3.12)", "IN_PROGRESS", None, started)]
        )
        self.assertEqual(train.choose([hung], NOW)[0], "idle")

    def test_running_required_check_is_in_flight(self):
        """A recent pending check is the PR being tested right now."""
        started = (NOW - timedelta(minutes=10)).isoformat()
        busy = pr(
            1, state="BLOCKED", checks=[("test (3.12)", "IN_PROGRESS", None, started)]
        )
        self.assertEqual(train.choose([busy], NOW)[:2], ("wait", 1))
