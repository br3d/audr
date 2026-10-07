#!/usr/bin/env python3
"""Unit tests for scripts/notify-board.py.

Deliberately dependency-free: run it with `python3 scripts/test_notify_board.py`.
The backend pytest suite is not the right home for this — it runs inside a
container that only copies `backend/`, so it cannot see `scripts/`. Same reason
as scripts/test_registry_prune.py.

The HTTP layer is stubbed, so nothing here touches the real board. What is under
test is the lifecycle: the failure mode that matters is not a bad request, it is
one outage turning into a dozen `critical` issues (or, worse, an alert quietly
appended to an issue somebody already closed).
"""

from __future__ import annotations

import importlib.util
import os
import sys
import tempfile
import unittest
from pathlib import Path

_SPEC = importlib.util.spec_from_file_location(
    "notify_board", Path(__file__).parent / "notify-board.py"
)
assert _SPEC and _SPEC.loader
nb = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(nb)

CONFIGURED = {
    "PAPERCLIP_API_URL": "https://board.example/api",
    "PAPERCLIP_API_KEY": "test-key-never-logged",
    "PAPERCLIP_COMPANY_ID": "company-1",
    "AUDR_ALERT_ASSIGNEE_AGENT_ID": "agent-infralead",
    "AUDR_ALERT_PROJECT_ID": "project-audr",
}


class FakeApi:
    """Records every call and replies from a scripted table."""

    def __init__(self, replies: dict[tuple[str, str], tuple[int, dict]] | None = None):
        self.calls: list[tuple[str, str, dict | None]] = []
        self.replies = replies or {}
        self.default_issue_id = "issue-new-1"

    def __call__(self, cfg, method, path, payload):
        self.calls.append((method, path, payload))
        for (m, fragment), reply in self.replies.items():
            if m == method and fragment in path:
                return reply
        if method == "POST" and path.endswith("/issues"):
            return 201, {"issue": {"id": self.default_issue_id}}
        if method == "POST" and path.endswith("/comments"):
            return 201, {}
        if method == "PATCH":
            return 200, {}
        if method == "GET":
            return 200, {"issue": {"id": "issue-open-1", "status": "in_progress"}}
        raise AssertionError(f"unscripted call {method} {path}")

    def methods(self):
        return [(m, p.rsplit("/", 2)[-2:] and p) for m, p, _ in self.calls]

    def creates(self):
        return [c for c in self.calls if c[0] == "POST" and c[1].endswith("/issues")]

    def comments(self):
        return [c for c in self.calls if c[0] == "POST" and c[1].endswith("/comments")]

    def patches(self):
        return [c for c in self.calls if c[0] == "PATCH"]


class BoardSinkTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.state = os.path.join(self._tmp.name, "state")
        self._real_request = nb.request
        self._real_env = dict(os.environ)
        for key in nb.REQUIRED + nb.OPTIONAL:
            os.environ.pop(key, None)
        # Keep sleeps out of the retry paths.
        self._real_sleep = nb.time.sleep
        nb.time.sleep = lambda _s: None
        self.addCleanup(self._restore)

    def _restore(self):
        nb.request = self._real_request
        nb.time.sleep = self._real_sleep
        os.environ.clear()
        os.environ.update(self._real_env)
        self._tmp.cleanup()

    def configure(self, **overrides):
        os.environ.update({**CONFIGURED, **overrides})

    def run_sink(self, *argv, api=None):
        api = api or FakeApi()
        nb.request = api
        code = nb.main(["--state-dir", self.state, *argv])
        return code, api

    def recorded(self, key="stand-down"):
        path = nb.state_path(self.state, key)
        return Path(path).read_text().strip() if os.path.exists(path) else None

    # ---------------------------------------------------------------- config

    def test_unconfigured_returns_3_and_sends_nothing(self):
        code, api = self.run_sink("--key", "stand-down", "--", "stand is down")
        self.assertEqual(code, 3)
        self.assertEqual(api.calls, [])

    def test_partial_credentials_count_as_unconfigured(self):
        os.environ["PAPERCLIP_API_URL"] = CONFIGURED["PAPERCLIP_API_URL"]
        code, api = self.run_sink("--", "stand is down")
        self.assertEqual(code, 3)
        self.assertEqual(api.calls, [])

    def test_empty_message_is_refused(self):
        self.configure()
        code, api = self.run_sink("--", "   ")
        self.assertEqual(code, 2)
        self.assertEqual(api.calls, [])

    def test_api_base_tolerates_both_url_spellings(self):
        for spelling in ("https://b.example", "https://b.example/", "https://b.example/api"):
            self.assertEqual(nb.api_base(spelling), "https://b.example")

    def test_api_base_rejects_non_http_schemes(self):
        for bad in ("file:///etc/passwd", "/just/a/path", "ftp://b.example"):
            with self.assertRaises(ValueError):
                nb.api_base(bad)

    def test_bad_url_logs_the_alert_instead_of_raising(self):
        # A typo in the credentials file must not turn an outage into a
        # traceback: the alert text still has to reach the log.
        self.configure(PAPERCLIP_API_URL="file:///etc/passwd")
        code, api = self.run_sink("--key", "stand-down", "--", "stand is down")
        self.assertEqual(code, 1)
        self.assertEqual(api.calls, [])

    # ------------------------------------------------------------- first fire

    def test_first_alert_creates_a_critical_issue_and_records_it(self):
        self.configure()
        code, api = self.run_sink("--key", "stand-down", "--", "🔴 audr stand DOWN\nbody line")
        self.assertEqual(code, 0)
        self.assertEqual(len(api.creates()), 1)
        payload = api.creates()[0][2]
        self.assertEqual(payload["priority"], "critical")
        self.assertEqual(payload["assigneeAgentId"], "agent-infralead")
        self.assertEqual(payload["projectId"], "project-audr")
        self.assertIn("body line", payload["description"])
        self.assertTrue(payload["title"].startswith("🔴 audr stand DOWN — "))
        self.assertEqual(self.recorded(), "issue-new-1")

    def test_priority_is_overridable(self):
        self.configure(AUDR_ALERT_PRIORITY="high")
        _, api = self.run_sink("--key", "k", "--", "something")
        self.assertEqual(api.creates()[0][2]["priority"], "high")

    def test_routing_is_optional(self):
        self.configure()
        del os.environ["AUDR_ALERT_ASSIGNEE_AGENT_ID"]
        del os.environ["AUDR_ALERT_PROJECT_ID"]
        _, api = self.run_sink("--key", "k", "--", "something")
        payload = api.creates()[0][2]
        self.assertNotIn("assigneeAgentId", payload)
        self.assertNotIn("projectId", payload)

    def test_without_a_key_nothing_is_recorded(self):
        # No --key means "unrelated one-off event": no dedupe, no state file.
        self.configure()
        code, api = self.run_sink("--", "one-off")
        self.assertEqual(code, 0)
        self.assertEqual(len(api.creates()), 1)
        self.assertFalse(os.path.isdir(self.state))

    # ----------------------------------------------------------- repeat fires

    def test_repeat_alert_comments_instead_of_opening_a_second_issue(self):
        self.configure()
        self.run_sink("--key", "stand-down", "--", "first")
        code, api = self.run_sink("--key", "stand-down", "--", "still down")
        self.assertEqual(code, 0)
        self.assertEqual(api.creates(), [])
        self.assertEqual(len(api.comments()), 1)
        self.assertEqual(api.comments()[0][2], {"body": "still down"})
        self.assertIn("issue-new-1", api.comments()[0][1])

    def test_a_closed_issue_is_not_reused(self):
        # Someone resolved the alert by hand. Appending to it would put the next
        # outage in a thread nobody is watching.
        self.configure()
        self.run_sink("--key", "stand-down", "--", "first")
        api = FakeApi({("GET", "/api/issues/"): (200, {"issue": {"id": "x", "status": "done"}})})
        api.default_issue_id = "issue-new-2"
        code, api = self.run_sink("--key", "stand-down", "--", "down again", api=api)
        self.assertEqual(code, 0)
        self.assertEqual(len(api.creates()), 1)
        self.assertEqual(self.recorded(), "issue-new-2")

    def test_a_deleted_issue_is_not_reused(self):
        self.configure()
        self.run_sink("--key", "stand-down", "--", "first")
        api = FakeApi({("GET", "/api/issues/"): (404, {})})
        api.default_issue_id = "issue-new-3"
        code, api = self.run_sink("--key", "stand-down", "--", "down again", api=api)
        self.assertEqual(code, 0)
        self.assertEqual(len(api.creates()), 1)
        self.assertEqual(self.recorded(), "issue-new-3")

    def test_unverifiable_issue_is_reused_rather_than_duplicated(self):
        # The board is unreachable for the GET but reachable for the POST. Erring
        # towards a comment keeps one outage to one critical issue.
        self.configure()
        self.run_sink("--key", "stand-down", "--", "first")
        api = FakeApi({("GET", "/api/issues/"): (0, {"error": "boom"})})
        code, api = self.run_sink("--key", "stand-down", "--", "still down", api=api)
        self.assertEqual(code, 0)
        self.assertEqual(api.creates(), [])
        self.assertEqual(len(api.comments()), 1)

    def test_distinct_keys_do_not_share_an_issue(self):
        self.configure()
        self.run_sink("--key", "stand-down", "--", "watchdog alert")
        code, api = self.run_sink("--key", "deploy-failed", "--", "ci alert")
        self.assertEqual(code, 0)
        self.assertEqual(len(api.creates()), 1)
        self.assertEqual(self.recorded("stand-down"), "issue-new-1")
        self.assertEqual(self.recorded("deploy-failed"), "issue-new-1")

    # --------------------------------------------------------------- recovery

    def test_resolve_closes_the_open_issue_and_forgets_it(self):
        self.configure()
        self.run_sink("--key", "stand-down", "--", "down")
        code, api = self.run_sink("--key", "stand-down", "--resolve", "--", "✅ RECOVERED")
        self.assertEqual(code, 0)
        self.assertEqual(len(api.patches()), 1)
        self.assertEqual(api.patches()[0][2], {"status": "done", "comment": "✅ RECOVERED"})
        # One call, not a comment plus a close: the note must not post twice.
        self.assertEqual(api.comments(), [])
        self.assertIsNone(self.recorded())

    def test_resolve_with_nothing_open_opens_no_issue(self):
        self.configure()
        code, api = self.run_sink("--key", "stand-down", "--resolve", "--", "✅ RECOVERED")
        self.assertEqual(code, 0)
        self.assertEqual(api.calls, [])

    def test_failed_close_keeps_the_record_so_repeats_still_dedupe(self):
        self.configure()
        self.run_sink("--key", "stand-down", "--", "down")
        api = FakeApi({("PATCH", "/api/issues/"): (500, {})})
        code, api = self.run_sink("--key", "stand-down", "--resolve", "--", "recovered", api=api)
        self.assertEqual(code, 1)
        self.assertEqual(self.recorded(), "issue-new-1")

    # ---------------------------------------------------------------- failure

    def test_failed_create_reports_1_and_records_nothing(self):
        self.configure()
        api = FakeApi({("POST", "/issues"): (500, {})})
        code, api = self.run_sink("--key", "stand-down", "--", "down", api=api)
        self.assertEqual(code, 1)
        self.assertIsNone(self.recorded())

    def test_failed_comment_reports_1(self):
        self.configure()
        self.run_sink("--key", "stand-down", "--", "down")
        api = FakeApi({("POST", "/comments"): (403, {"error": "nope"})})
        code, api = self.run_sink("--key", "stand-down", "--", "still down", api=api)
        self.assertEqual(code, 1)

    # ------------------------------------------------------------ small parts

    def test_title_is_truncated_and_stamped(self):
        stamp = "2026-10-07T10:40:00Z"
        title = nb.title_for("x" * 400, stamp)
        self.assertLessEqual(len(title), nb.TITLE_MAX)
        self.assertTrue(title.endswith(stamp))
        self.assertIn("…", title)

    def test_title_skips_leading_blank_lines(self):
        self.assertTrue(
            nb.title_for("\n\n  real first line\nrest", "S").startswith("real first line")
        )

    def test_state_filename_is_sanitised(self):
        path = nb.state_path("/tmp/s", "../../etc/passwd")
        self.assertEqual(os.path.dirname(path), "/tmp/s")
        self.assertNotIn("/", os.path.basename(path)[len("issue-") :])

    def test_env_file_takes_only_known_keys_and_strips_quotes(self):
        path = os.path.join(self._tmp.name, "paperclip.env")
        Path(path).write_text(
            "# a comment\n"
            'export PAPERCLIP_API_KEY="quoted-key"\n'
            "PAPERCLIP_COMPANY_ID=company-9\n"
            "UNRELATED_THING=should-be-ignored\n"
            "\n"
        )
        got = nb.load_env_file(path)
        self.assertEqual(
            got, {"PAPERCLIP_API_KEY": "quoted-key", "PAPERCLIP_COMPANY_ID": "company-9"}
        )

    def test_env_file_fills_gaps_but_does_not_override_environment(self):
        path = os.path.join(self._tmp.name, "paperclip.env")
        Path(path).write_text("PAPERCLIP_API_KEY=from-file\nPAPERCLIP_COMPANY_ID=from-file\n")
        os.environ["AUDR_BOARD_ENV"] = path
        os.environ["PAPERCLIP_API_URL"] = "https://env.example"
        os.environ["PAPERCLIP_COMPANY_ID"] = "from-env"
        cfg = nb.resolve_config(self._tmp.name)
        self.assertEqual(cfg["PAPERCLIP_API_KEY"], "from-file")
        self.assertEqual(cfg["PAPERCLIP_COMPANY_ID"], "from-env")

    def test_missing_env_file_is_not_an_error(self):
        self.assertEqual(nb.load_env_file(os.path.join(self._tmp.name, "nope.env")), {})


if __name__ == "__main__":
    unittest.main(verbosity=2) if "-v" in sys.argv else unittest.main()
