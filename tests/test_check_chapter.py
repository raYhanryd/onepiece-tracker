"""Unit tests for scripts/check_chapter.py -- stdlib only, fully offline."""

from __future__ import annotations

import importlib.util
import io
import json
import os
import pathlib
import sys
import tempfile
import unittest
import urllib.error
from email.message import Message
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "scripts" / "check_chapter.py"

_spec = importlib.util.spec_from_file_location("check_chapter", MODULE_PATH)
check_chapter = importlib.util.module_from_spec(_spec)
sys.modules["check_chapter"] = check_chapter
_spec.loader.exec_module(check_chapter)

URL_A = "https://discord.com/api/webhooks/111111111111111111/aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
URL_B = "https://discord.com/api/webhooks/222222222222222222/bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
KEY_A = "DISCORD_WEBHOOK_URL"
KEY_B = "DISCORD_WEBHOOK_URL_2"


def make_http_error(url, status, body=b'{"message": "nope"}', retry_after=None):
    headers = Message()
    if retry_after is not None:
        headers["Retry-After"] = str(retry_after)
    return urllib.error.HTTPError(url, status, f"HTTP {status}", headers, io.BytesIO(body))


def outcome(key, ok, permanent=False, detail="detail"):
    return check_chapter.DeliveryOutcome(key=key, ok=ok, permanent=permanent, detail=detail)


class FingerprintAndUrlTests(unittest.TestCase):
    def test_fingerprint_is_stable_and_opaque(self):
        fingerprint = check_chapter.webhook_fingerprint(URL_A)
        self.assertEqual(fingerprint, check_chapter.webhook_fingerprint(URL_A))
        self.assertNotEqual(fingerprint, check_chapter.webhook_fingerprint(URL_B))
        self.assertTrue(fingerprint.startswith("sha256:"))
        self.assertNotIn("aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa", fingerprint)

    def test_accepts_real_discord_webhook_shapes(self):
        for url in (
            URL_A,
            "https://discordapp.com/api/webhooks/123/abc",
            "https://canary.discord.com/api/webhooks/123/abc",
            "https://discord.com/api/v10/webhooks/123/abc",
        ):
            self.assertTrue(check_chapter.is_valid_discord_webhook(url), url)

    def test_rejects_non_webhook_urls(self):
        for url in (
            "",
            "not a url",
            "http://discord.com/api/webhooks/123/abc",  # not https
            "https://example.com/api/webhooks/123/abc",
            "https://discord.com/api/webhooks/only-one-segment",
            "https://discord.com/api/123/abc",
        ):
            self.assertFalse(check_chapter.is_valid_discord_webhook(url), url)


class ChapterNumberTests(unittest.TestCase):
    def test_regex_extracts_numbers(self):
        for title in ("One Piece Chapter 1194", "Chapter 3", "one piece chapter 1089.5"):
            self.assertIsNotNone(check_chapter.CHAPTER_NUM_RE.search(title), title)

    def test_regex_returns_none_for_other_titles(self):
        self.assertIsNone(check_chapter.CHAPTER_NUM_RE.search("One Piece Special"))


class ClassifyAndPendingTests(unittest.TestCase):
    def test_classify_chapter(self):
        self.assertEqual(check_chapter.classify_chapter(1194, None), "missing")
        self.assertEqual(check_chapter.classify_chapter(1194, 1193), "new")
        self.assertEqual(check_chapter.classify_chapter(1194, 1194), "same")
        self.assertEqual(check_chapter.classify_chapter(1193, 1194), "older")
        self.assertEqual(check_chapter.classify_chapter(1089.5, 1089), "new")

    def test_compute_pending_skips_delivered_and_foreign_keys(self):
        targets = [(KEY_A, URL_A), (KEY_B, URL_B)]
        self.assertEqual(
            check_chapter.compute_pending(targets, [KEY_A, "SOME_OLD_SECRET"]),
            [(KEY_B, URL_B)],
        )


class ReconcileQuarantineTests(unittest.TestCase):
    def test_keeps_matching_entry(self):
        entry = {"fingerprint": check_chapter.webhook_fingerprint(URL_A), "reason": "404"}
        self.assertEqual(
            check_chapter.reconcile_quarantine({KEY_A: entry}, [(KEY_A, URL_A)]),
            {KEY_A: entry},
        )

    def test_drops_entry_for_removed_secret(self):
        entry = {"fingerprint": check_chapter.webhook_fingerprint(URL_B)}
        self.assertEqual(check_chapter.reconcile_quarantine({KEY_B: entry}, [(KEY_A, URL_A)]), {})

    def test_drops_entry_when_url_changed(self):
        stale = {"fingerprint": check_chapter.webhook_fingerprint(URL_B)}
        self.assertEqual(check_chapter.reconcile_quarantine({KEY_A: stale}, [(KEY_A, URL_A)]), {})


class DeliveryTests(unittest.TestCase):
    def test_malformed_url_fails_permanently_without_any_network_call(self):
        calls = []

        def fake_attempt(key, url, payload):
            calls.append(key)
            return outcome(key, True)

        outcomes = check_chapter.attempt_deliveries(
            [("BAD", "http://nope")], {"embeds": []}, attempt=fake_attempt
        )
        self.assertEqual(calls, [])
        self.assertTrue(outcomes[0].permanent)

    def test_transient_failure_retries_then_succeeds(self):
        calls = {"count": 0}

        def fake_post(url, payload):
            calls["count"] += 1
            if calls["count"] < 3:
                raise urllib.error.URLError("temporary failure")

        with mock.patch.object(check_chapter, "_post_to_discord_webhook", fake_post), \
                mock.patch.object(check_chapter.time, "sleep"):
            result = check_chapter._attempt_webhook(KEY_A, URL_A, {"embeds": []})
        self.assertTrue(result.ok)
        self.assertEqual(calls["count"], 3)

    def test_permanent_http_error_does_not_retry(self):
        calls = {"count": 0}

        def fake_post(url, payload):
            calls["count"] += 1
            raise make_http_error(url, 404, b'{"message": "Unknown Webhook"}')

        with mock.patch.object(check_chapter, "_post_to_discord_webhook", fake_post), \
                mock.patch.object(check_chapter.time, "sleep"):
            result = check_chapter._attempt_webhook(KEY_B, URL_B, {"embeds": []})
        self.assertFalse(result.ok)
        self.assertTrue(result.permanent)
        self.assertIn("404", result.detail)
        self.assertEqual(calls["count"], 1)

    def test_rate_limit_retry_honors_retry_after(self):
        sleeps = []
        calls = {"count": 0}

        def fake_post(url, payload):
            calls["count"] += 1
            if calls["count"] == 1:
                raise make_http_error(url, 429, b'{"retry_after": 0.5}', retry_after=0.5)

        with mock.patch.object(check_chapter, "_post_to_discord_webhook", fake_post), \
                mock.patch.object(check_chapter.time, "sleep", sleeps.append):
            result = check_chapter._attempt_webhook(KEY_A, URL_A, {"embeds": []})
        self.assertTrue(result.ok)
        self.assertEqual(sleeps, [0.5])


class StateFileTests(unittest.TestCase):
    def test_old_state_files_load_with_defaults(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "state.json")
            with open(path, "w") as f:
                json.dump({"chapter_number": 1191, "title": "t", "url": "u", "pub_date": ""}, f)
            with mock.patch.object(check_chapter, "STATE_PATH", path):
                state = check_chapter.load_state()
        self.assertEqual(state["delivered_to"], [])
        self.assertEqual(state["quarantined"], {})

    def test_round_trip_preserves_delivery_tracking(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "state.json")
            with mock.patch.object(check_chapter, "STATE_PATH", path):
                check_chapter.save_state(
                    {"chapter_number": 1194, "title": "T", "url": "U", "pub_date": "P"},
                    [KEY_B, KEY_A, KEY_A],
                    {KEY_B: {"reason": "404", "fingerprint": "sha256:x", "alerted_at": None}},
                )
                state = check_chapter.load_state()
        self.assertEqual(state["delivered_to"], [KEY_A, KEY_B])
        self.assertIn(KEY_B, state["quarantined"])
        self.assertTrue(state["checked_at"])


class DiagnosticTests(unittest.TestCase):
    def test_reports_dead_webhook(self):
        with mock.patch.object(check_chapter, "get_webhook_targets", lambda: [(KEY_A, URL_A)]), \
                mock.patch.object(
                    check_chapter, "_fetch_webhook_info",
                    lambda url: (404, '{"message": "Unknown Webhook"}'),
                ), \
                mock.patch.object(sys, "argv", ["check_chapter.py", "--check-webhooks"]):
            rc = check_chapter.main()
        self.assertEqual(rc, 1)

    def test_reports_healthy_webhook(self):
        body = json.dumps({"name": "onepiece", "channel_id": "123"})
        with mock.patch.object(check_chapter, "get_webhook_targets", lambda: [(KEY_A, URL_A)]), \
                mock.patch.object(check_chapter, "_fetch_webhook_info", lambda url: (200, body)), \
                mock.patch.object(sys, "argv", ["check_chapter.py", "--check-webhooks"]):
            rc = check_chapter.main()
        self.assertEqual(rc, 0)

    def test_two_secrets_on_the_same_webhook_are_only_warned_about(self):
        body = json.dumps({"name": "onepiece", "channel_id": "123"})
        with mock.patch.object(
            check_chapter, "get_webhook_targets", lambda: [(KEY_A, URL_A), (KEY_B, URL_A)]
        ), \
                mock.patch.object(check_chapter, "_fetch_webhook_info", lambda url: (200, body)), \
                mock.patch.object(sys, "argv", ["check_chapter.py", "--check-webhooks"]):
            rc = check_chapter.main()
        self.assertEqual(rc, 0)


class MainFlowTests(unittest.TestCase):
    """End-to-end runs of main() with only the network layer faked."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.state_path = os.path.join(self._tmp.name, "last_chapter.json")
        state_patcher = mock.patch.object(check_chapter, "STATE_PATH", self.state_path)
        state_patcher.start()
        self.addCleanup(state_patcher.stop)
        argv_patcher = mock.patch.object(sys, "argv", ["check_chapter.py"])
        argv_patcher.start()
        self.addCleanup(argv_patcher.stop)

    def write_state(self, **overrides):
        payload = {
            "chapter_number": 1191,
            "title": "One Piece Chapter 1191",
            "url": "https://weebcentral.com/chapters/x",
            "pub_date": "Fri, 21 Aug 2026 14:58:00 +0000",
            "delivered_to": [],
            "quarantined": {},
            "checked_at": "2026-08-21T15:35:25+00:00",
        }
        payload.update(overrides)
        with open(self.state_path, "w") as f:
            json.dump(payload, f)
        return payload

    def read_state(self):
        with open(self.state_path) as f:
            return json.load(f)

    @staticmethod
    def chapter(number=1194):
        return {
            "chapter_number": number,
            "title": f"One Piece Chapter {number}",
            "url": "https://weebcentral.com/chapters/new",
            "pub_date": "Sat, 26 Sep 2026 03:16:36 +0000",
        }

    def run_main(self, chapter, targets, post):
        with mock.patch.object(check_chapter, "fetch_latest_chapter", lambda: chapter), \
                mock.patch.object(check_chapter, "get_webhook_targets", lambda: targets), \
                mock.patch.object(check_chapter, "_post_to_discord_webhook", post), \
                mock.patch.object(check_chapter.time, "sleep"):
            return check_chapter.main()

    def test_one_dead_webhook_does_not_block_state_and_is_quarantined(self):
        posted = []

        def post(url, payload):
            posted.append(url)
            if url == URL_B:
                raise make_http_error(url, 404, b'{"message": "Unknown Webhook"}')
            return None

        rc = self.run_main(self.chapter(1194), [(KEY_A, URL_A), (KEY_B, URL_B)], post)

        self.assertEqual(rc, 0)
        state = self.read_state()
        self.assertEqual(state["chapter_number"], 1194)
        self.assertEqual(state["delivered_to"], [KEY_A])
        self.assertIn(KEY_B, state["quarantined"])
        self.assertTrue(state["quarantined"][KEY_B]["reason"].startswith("HTTP 404"))
        self.assertTrue(state["quarantined"][KEY_B]["alerted_at"])
        # A got the chapter and then the quarantine alert; B was tried once.
        self.assertEqual(posted, [URL_A, URL_B, URL_A])

    def test_healthy_webhook_is_not_renotified_on_later_runs(self):
        posted = []

        def post(url, payload):
            posted.append(url)
            if url == URL_B:
                raise make_http_error(url, 404)
            return None

        self.assertEqual(self.run_main(self.chapter(1194), [(KEY_A, URL_A), (KEY_B, URL_B)], post), 0)
        self.assertEqual(posted, [URL_A, URL_B, URL_A])

        posted.clear()
        self.assertEqual(self.run_main(self.chapter(1194), [(KEY_A, URL_A), (KEY_B, URL_B)], post), 0)
        self.assertEqual(posted, [], "nothing pending, so nothing should be sent again")

    def test_transient_failure_retries_only_the_failed_webhook_later(self):
        posted = []

        def failing_post(url, payload):
            posted.append(url)
            if url == URL_B:
                raise urllib.error.URLError("temporary network failure")
            return None

        rc = self.run_main(self.chapter(1194), [(KEY_A, URL_A), (KEY_B, URL_B)], failing_post)
        self.assertEqual(rc, 1)
        state = self.read_state()
        self.assertEqual(state["delivered_to"], [KEY_A])
        self.assertEqual(state["quarantined"], {})
        self.assertEqual(posted.count(URL_A), 1)

        posted.clear()

        def ok_post(url, payload):
            posted.append(url)
            return None

        self.assertEqual(self.run_main(self.chapter(1194), [(KEY_A, URL_A), (KEY_B, URL_B)], ok_post), 0)
        self.assertEqual(posted, [URL_B])
        self.assertEqual(self.read_state()["delivered_to"], [KEY_A, KEY_B])

    def test_a_single_dead_webhook_is_not_quarantined_and_keeps_failing(self):
        posted = []

        def post(url, payload):
            posted.append(url)
            raise make_http_error(url, 404, b'{"message": "Unknown Webhook"}')

        rc = self.run_main(self.chapter(1194), [(KEY_A, URL_A)], post)
        self.assertEqual(rc, 1)
        state = self.read_state()
        self.assertEqual(state["quarantined"], {})
        self.assertEqual(state["delivered_to"], [])
        self.assertEqual(state["chapter_number"], 1194)

        posted.clear()
        self.assertEqual(self.run_main(self.chapter(1194), [(KEY_A, URL_A)], post), 1)
        self.assertEqual(posted, [URL_A], "a lone dead webhook must keep retrying loudly")

    def test_replacing_the_secret_clears_quarantine(self):
        self.write_state(
            chapter_number=1194,
            delivered_to=[KEY_A],
            quarantined={
                KEY_B: {
                    "since": "2026-09-01T00:00:00+00:00",
                    "reason": "HTTP 404: Unknown Webhook",
                    "fingerprint": check_chapter.webhook_fingerprint(
                        "https://discord.com/api/webhooks/999999999999999999/oldtoken"
                    ),
                    "alerted_at": "2026-09-01T00:00:00+00:00",
                }
            },
        )
        posted = []

        def post(url, payload):
            posted.append(url)
            return None

        rc = self.run_main(self.chapter(1195), [(KEY_A, URL_A), (KEY_B, URL_B)], post)
        self.assertEqual(rc, 0)
        self.assertEqual(posted, [URL_A, URL_B])
        state = self.read_state()
        self.assertEqual(state["quarantined"], {})
        self.assertEqual(state["delivered_to"], [KEY_A, KEY_B])

    def test_an_older_feed_item_is_ignored(self):
        self.write_state(chapter_number=1194)
        posted = []

        def post(url, payload):
            posted.append(url)
            return None

        rc = self.run_main(self.chapter(1193), [(KEY_A, URL_A)], post)
        self.assertEqual(rc, 0)
        self.assertEqual(posted, [])
        self.assertEqual(self.read_state()["chapter_number"], 1194)

    def test_no_configured_webhook_keeps_failing_without_advancing_state(self):
        rc = self.run_main(self.chapter(1194), [], lambda url, payload: None)
        self.assertEqual(rc, 1)
        self.assertFalse(os.path.exists(self.state_path))


if __name__ == "__main__":
    unittest.main()
