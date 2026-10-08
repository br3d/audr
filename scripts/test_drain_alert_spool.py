#!/usr/bin/env python3
"""Unit tests for scripts/drain-alert-spool.py.

Dependency-free, like scripts/test_notify_board.py: run it with
`python3 scripts/test_drain_alert_spool.py`. No SSH and no board are touched —
what is under test is the collapse, which is where the damage would be.

The failure mode that matters is not a bad request. It is a spool drained a day
after the outage putting a dozen comments on one issue, or — worse — replaying
"🔴 audr stand DOWN" with nothing marking it as history, so somebody is sent to
the host for an incident that ended yesterday.
"""

from __future__ import annotations

import importlib.util
import unittest
from pathlib import Path

_SPEC = importlib.util.spec_from_file_location(
    "drain_alert_spool", Path(__file__).parent / "drain-alert-spool.py"
)
assert _SPEC and _SPEC.loader
drain = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(drain)


def spool(*records: dict) -> str:
    """Render records as the JSONL the host writes, with sane defaults."""
    import json

    lines = []
    for i, record in enumerate(records, start=1):
        full = {
            "ts": f"2026-10-08T0{i}:00:00Z",
            "key": "stand-down",
            "resolve": False,
            "reason": "unconfigured",
            "message": f"alert {i}",
        }
        full.update(record)
        lines.append(json.dumps(full, ensure_ascii=False))
    return "\n".join(lines) + "\n"


class ParseRecords(unittest.TestCase):
    def test_blank_and_malformed_lines_do_not_lose_the_rest(self):
        blob = spool({"message": "first"}) + "\n" + "{truncated\n" + spool({"message": "second"})
        records = drain.parse_records(blob)
        self.assertEqual(["first", "second"], [r["message"] for r in records])

    def test_records_without_a_message_are_dropped(self):
        records = drain.parse_records('{"key":"stand-down","message":""}\n')
        self.assertEqual([], records)


class Collapse(unittest.TestCase):
    def test_hourly_repeats_of_one_outage_become_a_single_send(self):
        # The whole point: 12 spooled reminders are one condition, not twelve.
        records = drain.parse_records(spool(*[{"message": f"down {i}"} for i in range(1, 13)]))
        sends = drain.plan(records)
        self.assertEqual(1, len(sends))
        # And it is the NEWEST one, so the issue carries the current probe count.
        self.assertEqual("down 12", sends[0][0]["message"])

    def test_a_finished_outage_opens_then_closes_one_issue(self):
        records = drain.parse_records(
            spool(
                {"message": "down 1"},
                {"message": "down 2"},
                {"message": "recovered", "resolve": True},
            )
        )
        sends = drain.plan(records)
        self.assertEqual(["down 2", "recovered"], [r["message"] for r, _ in sends])
        self.assertEqual([False, True], [bool(r.get("resolve")) for r, _ in sends])

    def test_recovery_alone_still_closes_the_issue_opened_before_the_channel_broke(self):
        records = drain.parse_records(spool({"message": "recovered", "resolve": True}))
        sends = drain.plan(records)
        self.assertEqual(1, len(sends))
        self.assertTrue(sends[0][0]["resolve"])

    def test_a_recovery_followed_by_a_new_outage_does_not_close_the_new_one(self):
        # Ordering trap: the resolve is older than the latest alert, so sending
        # it after would close an issue describing an outage still in progress.
        records = drain.parse_records(
            spool(
                {"message": "down 1"},
                {"message": "recovered", "resolve": True},
                {"message": "down 3"},
            )
        )
        sends = drain.plan(records)
        self.assertEqual(["down 3"], [r["message"] for r, _ in sends])

    def test_distinct_keys_stay_separate_threads(self):
        records = drain.parse_records(
            spool({"message": "stand"}, {"message": "deploy", "key": "deploy-failed"})
        )
        sends = drain.plan(records)
        self.assertEqual({"stand-down", "deploy-failed"}, {r["key"] for r, _ in sends})

    def test_keyless_alerts_are_each_replayed(self):
        records = drain.parse_records(
            spool({"message": "one", "key": ""}, {"message": "two", "key": ""})
        )
        sends = drain.plan(records)
        self.assertEqual(["one", "two"], [r["message"] for r, _ in sends])

    def test_a_failed_send_carries_back_every_record_it_folded(self):
        # Otherwise collapsing would silently destroy the folded reminders when
        # delivery fails, and the next drain would have nothing to retry.
        records = drain.parse_records(spool({"message": "down 1"}, {"message": "down 2"}))
        sends = drain.plan(records)
        self.assertEqual([1, 2], sorted(sends[0][1]))


class RemoteDirExpr(unittest.TestCase):
    """The default state dir is `~/...`, and getting this wrong fails quietly.

    shlex.quote on the whole path makes the remote bash look for a directory
    literally named `~`; it finds none and the drain prints "spool is empty"
    while alerts accumulate on the host. Exactly the silent-gap failure this
    whole mechanism exists to remove, so it gets its own tests.
    """

    def test_tilde_becomes_HOME_so_the_remote_shell_expands_it(self):
        expr = drain.remote_dir_expr("~/.local/state/audr-notify")
        self.assertTrue(expr.startswith('"$HOME"/'))
        self.assertNotIn("'~", expr)

    def test_bare_tilde(self):
        self.assertEqual('"$HOME"', drain.remote_dir_expr("~"))

    def test_absolute_paths_are_quoted_whole(self):
        self.assertEqual("/var/spool/audr", drain.remote_dir_expr("/var/spool/audr"))

    def test_hostile_characters_are_still_quoted(self):
        expr = drain.remote_dir_expr("/tmp/a b;rm -rf /")  # noqa: S108 - a literal, never created
        self.assertEqual("'/tmp/a b;rm -rf /'", expr)

    def test_a_tilde_path_with_hostile_characters_quotes_the_remainder(self):
        expr = drain.remote_dir_expr("~/spool dir;x")
        self.assertEqual("\"$HOME\"/'spool dir;x'", expr)


class ReplayBody(unittest.TestCase):
    def test_the_original_alert_is_preserved_verbatim(self):
        body = drain.replay_body({"ts": "2026-10-08T01:00:00Z", "message": "🔴 down\nline two"})
        self.assertIn("🔴 down\nline two", body)

    def test_it_is_labelled_as_history_not_a_live_page(self):
        body = drain.replay_body({"ts": "2026-10-08T01:00:00Z", "message": "🔴 down"})
        self.assertIn("delayed replay", body)
        self.assertIn("2026-10-08T01:00:00Z", body)
        self.assertIn("already ended", body)

    def test_the_first_line_still_says_what_broke(self):
        # notify-board.py titles the issue from the first non-blank line. If the
        # explanation leads, every replayed outage lands on the board as
        # "Delayed alert replay — spooled at…" and the row says nothing.
        body = drain.replay_body(
            {"ts": "2026-10-08T01:00:00Z", "message": "🔴 audr stand DOWN\n\nprobe: /health/ready"}
        )
        first = body.splitlines()[0]
        self.assertIn("audr stand DOWN", first)
        self.assertIn("delayed replay", first)

    def test_an_empty_first_line_does_not_break_the_title(self):
        body = drain.replay_body({"ts": "2026-10-08T01:00:00Z", "message": "\n\n  real alert\n"})
        self.assertEqual("⏱ (delayed replay) real alert", body.splitlines()[0])


if __name__ == "__main__":
    unittest.main(verbosity=2)
