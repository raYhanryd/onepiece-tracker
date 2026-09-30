#!/usr/bin/env python3
"""
One Piece chapter tracker -> Discord notifier.

Checks weebcentral's RSS feed for the One Piece series, compares the
latest chapter against a stored state file, and posts a Discord
notification via webhook when a new chapter is found.

Uses Python stdlib only: urllib, xml.etree.ElementTree, json, argparse.

Exit codes:
  0 = success -- a new chapter was announced to every active webhook, or
      there was nothing to do. A permanently-dead webhook is quarantined
      (and the surviving webhook is told about it) without failing the run.
  1 = failure -- feed unreachable/unparsable, no usable webhook, or a
      delivery that WILL BE RETRIED next run. NOT the same as
      "no new chapter" and must be treated as an alert.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from urllib.parse import urlsplit
from xml.etree import ElementTree

# --- Config -----------------------------------------------------------

RSS_URL = "https://weebcentral.com/series/01J76XY7E9FNDZ1DBBM6PBJPFK/rss"
SERIES_URL = "https://weebcentral.com/series/01J76XY7E9FNDZ1DBBM6PBJPFK/One-Piece"
STATE_PATH = os.path.join(os.path.dirname(__file__), "..", "state", "last_chapter.json")

# Every webhook destination is identified by the env var that holds it.
# Add more by adding more env vars here (and as secrets/steps in the
# workflow) -- DISCORD_WEBHOOK_URL_3, _4, etc. Variables that are unset
# are simply skipped, so one configured webhook means one destination.
WEBHOOK_ENV_VARS = ("DISCORD_WEBHOOK_URL", "DISCORD_WEBHOOK_URL_2")

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)

DISCORD_COLOR_NEW_CHAPTER = 0xE67E22  # orange, "new release" feel
DISCORD_COLOR_ALERT = 0xED4245  # red

CHAPTER_NUM_RE = re.compile(r"Chapter\s+(\d+(?:\.\d+)?)", re.IGNORECASE)

# Discord status codes that mean "this webhook itself is gone/unauthorized".
# Retrying can never help, so the destination gets quarantined instead of
# blocking state updates forever.
PERMANENT_HTTP_STATUSES = frozenset({401, 403, 404})
# Rate limits and server-side blips: worth retrying in-run and next run.
RETRYABLE_HTTP_STATUSES = frozenset({429, 500, 502, 503, 504})

SEND_ATTEMPTS = 3
RETRY_BACKOFF_SECONDS = (2.0, 5.0)
MAX_INLINE_RETRY_DELAY_SECONDS = 30.0
WEBHOOK_TIMEOUT_SECONDS = 15


# --- Logging helpers ----------------------------------------------------


def log_info(msg: str) -> None:
    print(f"[INFO] {msg}", flush=True)


def log_ok(msg: str) -> None:
    print(f"[OK] {msg}", flush=True)


def log_warn(msg: str) -> None:
    print(f"[WARN] {msg}", flush=True)


def log_error(msg: str) -> None:
    # Loud, unmistakable in GitHub Actions logs.
    print(f"\n{'=' * 60}\n[ERROR] {msg}\n{'=' * 60}\n", file=sys.stderr, flush=True)


# --- Core logic -----------------------------------------------------------


class ScraperError(Exception):
    """Raised when the feed can't be fetched/parsed or the state file is broken."""


@dataclass(frozen=True)
class DeliveryOutcome:
    """Result of trying to deliver one payload to one webhook."""

    key: str
    ok: bool
    permanent: bool
    detail: str


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def webhook_fingerprint(url: str) -> str:
    """
    Non-reversible fingerprint of a webhook URL.

    Stored in the (committed) state file so quarantined webhooks can be
    detected as "replaced" without ever writing the URL itself to the repo.
    """
    digest = hashlib.sha256(url.encode("utf-8")).hexdigest()
    return f"sha256:{digest[:16]}"


def is_valid_discord_webhook(url: str) -> bool:
    """Cheap shape check: https://<discord host>/api/webhooks/<id>/<token>."""
    parts = urlsplit(url)
    if parts.scheme != "https" or not parts.hostname:
        return False
    host = parts.hostname.lower()
    if not (host == "discord.com" or host.endswith(".discord.com")
            or host == "discordapp.com" or host.endswith(".discordapp.com")):
        return False
    segments = [segment for segment in parts.path.split("/") if segment]
    if "webhooks" not in segments:
        return False
    tail = len(segments) - segments.index("webhooks") - 1
    return tail >= 2


def mask_webhook(url: str) -> str:
    """Mask a webhook for logs -- it's a bearer credential."""
    return url[:40] + "..." if len(url) > 40 else url


def get_webhook_targets() -> list[tuple[str, str]]:
    """
    Collect every configured webhook as (env var name, stripped URL).

    Unset/empty variables are skipped silently: with only one secret
    configured there is exactly one destination.
    """
    targets: list[tuple[str, str]] = []
    for key in WEBHOOK_ENV_VARS:
        raw = (os.environ.get(key) or "").strip()
        if not raw:
            continue
        targets.append((key, raw))
    return targets


def fetch_latest_chapter() -> dict:
    """
    Fetch the weebcentral RSS feed and return the latest chapter as a dict:
    {"chapter_number": float, "title": str, "url": str, "pub_date": str}

    Raises ScraperError on any failure -- network, HTTP status, XML parse,
    or missing/unexpected fields. Callers must NOT treat this as
    "no new chapter"; it means the check itself failed.
    """
    req = urllib.request.Request(RSS_URL, headers={"User-Agent": USER_AGENT})

    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            if resp.status != 200:
                raise ScraperError(f"RSS feed returned HTTP {resp.status}")
            raw = resp.read()
    except urllib.error.HTTPError as e:
        raise ScraperError(f"HTTP error fetching RSS feed: {e.code} {e.reason}") from e
    except urllib.error.URLError as e:
        raise ScraperError(f"Network error fetching RSS feed: {e.reason}") from e
    except Exception as e:
        raise ScraperError(f"Unexpected error fetching RSS feed: {e}") from e

    try:
        root = ElementTree.fromstring(raw)
    except ElementTree.ParseError as e:
        raise ScraperError(f"Failed to parse RSS XML: {e}") from e

    items = root.findall("./channel/item")
    if not items:
        raise ScraperError(
            "RSS feed parsed but contained no <item> elements -- "
            "feed structure may have changed"
        )

    first = items[0]
    title_el = first.find("title")
    link_el = first.find("link")

    if title_el is None or not (title_el.text or "").strip():
        raise ScraperError("Latest RSS item is missing a <title>")
    if link_el is None or not (link_el.text or "").strip():
        raise ScraperError("Latest RSS item is missing a <link>")

    title = title_el.text.strip()
    url = link_el.text.strip()

    match = CHAPTER_NUM_RE.search(title)
    if not match:
        raise ScraperError(
            f"Could not extract a chapter number from title: {title!r} -- "
            "title format may have changed"
        )

    chapter_num_str = match.group(1)
    chapter_number = float(chapter_num_str) if "." in chapter_num_str else int(chapter_num_str)

    pub_date_el = first.find("pubDate")
    pub_date = pub_date_el.text.strip() if pub_date_el is not None and pub_date_el.text else ""

    return {
        "chapter_number": chapter_number,
        "title": title,
        "url": url,
        "pub_date": pub_date,
    }


def load_state() -> dict | None:
    """
    Load and normalize the state file. Old files (written before
    per-webhook delivery tracking existed) simply lack the new fields.

    Raises ScraperError if the file exists but can't be read/parsed.
    """
    if not os.path.exists(STATE_PATH):
        return None
    try:
        with open(STATE_PATH, "r") as f:
            data = json.load(f)
    except (json.JSONDecodeError, OSError) as e:
        raise ScraperError(f"Failed to read/parse state file at {STATE_PATH}: {e}") from e

    if not isinstance(data, dict):
        raise ScraperError(f"State file at {STATE_PATH} is not a JSON object.")

    if not isinstance(data.get("chapter_number"), (int, float)):
        data["chapter_number"] = None
    if not isinstance(data.get("delivered_to"), list):
        data["delivered_to"] = []
    if not isinstance(data.get("quarantined"), dict):
        data["quarantined"] = {}
    return data


def save_state(chapter: dict, delivered_to: list[str], quarantined: dict) -> None:
    os.makedirs(os.path.dirname(STATE_PATH), exist_ok=True)
    payload = {
        "chapter_number": chapter.get("chapter_number"),
        "title": chapter.get("title", ""),
        "url": chapter.get("url", ""),
        "pub_date": chapter.get("pub_date", ""),
        "delivered_to": sorted(set(delivered_to)),
        "quarantined": quarantined,
        "checked_at": _utc_now_iso(),
    }
    with open(STATE_PATH, "w") as f:
        json.dump(payload, f, indent=2)
        f.write("\n")


def classify_chapter(latest_number, stored_number) -> str:
    """'missing' (no/broken state) | 'new' | 'same' | 'older'."""
    if stored_number is None:
        return "missing"
    if latest_number == stored_number:
        return "same"
    if latest_number > stored_number:
        return "new"
    return "older"


def compute_pending(
    targets: list[tuple[str, str]], delivered_to: list[str]
) -> list[tuple[str, str]]:
    """Webhooks that have NOT already received the chapter in question."""
    delivered = {key for key in delivered_to if isinstance(key, str)}
    return [(key, url) for key, url in targets if key not in delivered]


def reconcile_quarantine(quarantined: dict, targets: list[tuple[str, str]]) -> dict:
    """
    Keep only quarantine entries that still apply: the webhook must still be
    configured AND still be the same URL (fingerprint match). A replaced or
    removed secret clears its quarantine automatically.
    """
    target_map = dict(targets)
    reconciled: dict = {}
    for key, entry in quarantined.items():
        if key not in target_map:
            log_info(f"{key}: no longer configured; dropping its quarantine entry.")
            continue
        if not isinstance(entry, dict):
            continue
        if entry.get("fingerprint") != webhook_fingerprint(target_map[key]):
            log_info(f"{key}: webhook URL changed; clearing quarantine and retrying it.")
            continue
        reconciled[key] = entry
    return reconciled


def build_new_chapter_payload(chapter: dict) -> dict:
    # Keep this shape deliberately minimal: title, link, one-line description,
    # colour. The publish date stays in state (see fetch_latest_chapter) but is
    # not shown in the embed.
    embed = {
        "title": chapter.get("title") or f"One Piece Chapter {chapter.get('chapter_number')}",
        "url": chapter.get("url", ""),
        "description": "A new One Piece chapter is out.",
        "color": DISCORD_COLOR_NEW_CHAPTER,
        "timestamp": _utc_now_iso(),
        "footer": {"text": "weebcentral"},
    }
    return {"embeds": [embed]}


def build_quarantine_alert_payload(key: str, reason: str) -> dict:
    return {
        "embeds": [
            {
                "title": "Discord webhook disabled",
                "description": (
                    f"`{key}` failed with a permanent error and has been quarantined, "
                    "so it will not be retried. Notifications continue in this channel. "
                    "Delete or replace that webhook's repo secret -- the quarantine "
                    "clears automatically when the URL changes."
                ),
                "color": DISCORD_COLOR_ALERT,
                "fields": [{"name": "Reason", "value": str(reason)[:1024], "inline": False}],
                "timestamp": _utc_now_iso(),
                "footer": {"text": "chapter tracker"},
            }
        ]
    }


def _retry_after_seconds(err: urllib.error.HTTPError) -> float | None:
    headers = getattr(err, "headers", None)
    if headers is None:
        return None
    raw = headers.get("Retry-After")
    if not raw:
        return None
    try:
        return max(0.0, float(raw))
    except (TypeError, ValueError):
        return None


def _post_to_discord_webhook(webhook_url: str, payload: dict) -> None:
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        webhook_url,
        data=data,
        headers={"Content-Type": "application/json", "User-Agent": USER_AGENT},
        method="POST",
    )

    with urllib.request.urlopen(req, timeout=WEBHOOK_TIMEOUT_SECONDS) as resp:
        # Discord webhooks return 204 No Content on success.
        if resp.status not in (200, 204):
            raise ScraperError(f"Discord webhook returned unexpected status {resp.status}")


def _attempt_webhook(key: str, url: str, payload: dict) -> DeliveryOutcome:
    """POST to one webhook, retrying transient failures a bounded number of times."""
    last_detail = "delivery failed"
    for attempt_number in range(1, SEND_ATTEMPTS + 1):
        try:
            _post_to_discord_webhook(url, payload)
            return DeliveryOutcome(key=key, ok=True, permanent=False, detail="delivered")
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", errors="replace").strip()
            last_detail = f"HTTP {e.code}: {body[:200]}" if body else f"HTTP {e.code}"
            if e.code in PERMANENT_HTTP_STATUSES:
                return DeliveryOutcome(key=key, ok=False, permanent=True, detail=last_detail)
            if e.code in RETRYABLE_HTTP_STATUSES and attempt_number < SEND_ATTEMPTS:
                delay = _retry_after_seconds(e)
                if delay is None:
                    delay = RETRY_BACKOFF_SECONDS[attempt_number - 1]
                delay = min(delay, MAX_INLINE_RETRY_DELAY_SECONDS)
                log_warn(f"{key}: {last_detail} -- retrying in {delay:g}s")
                time.sleep(delay)
                continue
            return DeliveryOutcome(key=key, ok=False, permanent=False, detail=last_detail)
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            last_detail = f"network error: {getattr(e, 'reason', e)}"
            if attempt_number < SEND_ATTEMPTS:
                delay = RETRY_BACKOFF_SECONDS[attempt_number - 1]
                log_warn(f"{key}: {last_detail} -- retrying in {delay:g}s")
                time.sleep(delay)
                continue
            return DeliveryOutcome(key=key, ok=False, permanent=False, detail=last_detail)
        except Exception as e:  # never let one bad destination kill the whole run
            return DeliveryOutcome(key=key, ok=False, permanent=False, detail=f"unexpected error: {e}")
    return DeliveryOutcome(key=key, ok=False, permanent=False, detail=last_detail)


def attempt_deliveries(
    targets: list[tuple[str, str]],
    payload: dict,
    attempt=None,
) -> list[DeliveryOutcome]:
    """
    Deliver the payload to every target, collecting outcomes.

    `attempt` is a seam for tests (defaults to the real webhook poster).
    Malformed URLs are reported as permanent failures without any network
    call -- a broken secret must not stop the other destinations.
    """
    if attempt is None:
        attempt = _attempt_webhook
    outcomes: list[DeliveryOutcome] = []
    for key, url in targets:
        if not is_valid_discord_webhook(url):
            outcomes.append(
                DeliveryOutcome(
                    key=key,
                    ok=False,
                    permanent=True,
                    detail=(
                        "not a valid Discord webhook URL (expected "
                        "https://discord.com/api/webhooks/<id>/<token>)"
                    ),
                )
            )
            continue
        outcomes.append(attempt(key, url, payload))
    return outcomes


def _fetch_webhook_info(url: str) -> tuple[int, str]:
    """GET a webhook object from Discord (sends no message)."""
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT}, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=WEBHOOK_TIMEOUT_SECONDS) as resp:
            return resp.status, resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", errors="replace")


def run_webhook_diagnostic() -> int:
    """
    Read-only check of every configured webhook: GETs each URL, sends no
    message, writes no state. 404/401 means the webhook is deleted/dead.
    """
    targets = get_webhook_targets()
    if not targets:
        log_error(
            "No Discord webhook URLs configured "
            "(DISCORD_WEBHOOK_URL / DISCORD_WEBHOOK_URL_2)."
        )
        return 1

    failures = 0
    by_fingerprint: dict[str, list[str]] = {}
    log_info(f"Checking {len(targets)} configured webhook(s); no messages are sent.")

    for key, url in targets:
        by_fingerprint.setdefault(webhook_fingerprint(url), []).append(key)
        if not is_valid_discord_webhook(url):
            log_error(f"{key}: not a valid Discord webhook URL.")
            failures += 1
            continue
        try:
            status, body = _fetch_webhook_info(url)
        except Exception as e:
            log_error(f"{key}: could not reach Discord: {e}")
            failures += 1
            continue

        if status == 200:
            name, channel_id = "", ""
            try:
                info = json.loads(body)
            except json.JSONDecodeError:
                info = {}
            if isinstance(info, dict):
                name = str(info.get("name", ""))
                channel_id = str(info.get("channel_id", ""))
            log_ok(f"{key}: OK ({mask_webhook(url)}) name={name!r} channel_id={channel_id}")
        else:
            log_error(f"{key}: DEAD ({mask_webhook(url)}) HTTP {status}: {body.strip()[:200]}")
            failures += 1

    for keys in by_fingerprint.values():
        if len(keys) > 1:
            log_warn(
                f"{', '.join(keys)} point at the same webhook, so each will post its "
                "own message (duplicate notifications)."
            )

    if failures:
        log_error(f"{failures}/{len(targets)} webhook(s) are not usable.")
        return 1
    log_ok(f"All {len(targets)} webhook(s) look healthy.")
    return 0


def _send_quarantine_alerts(quarantined: dict, targets: list[tuple[str, str]]) -> bool:
    """
    Tell the surviving webhook(s) about freshly quarantined ones, once each.
    Returns True if the quarantine bookkeeping changed (needs saving).
    """
    unalerted = [
        (key, entry)
        for key, entry in quarantined.items()
        if isinstance(entry, dict) and not entry.get("alerted_at")
    ]
    if not unalerted:
        return False

    alert_targets = [(key, url) for key, url in targets if key not in quarantined]
    if not alert_targets:
        log_warn("No healthy webhook available to deliver the quarantine alert yet.")
        return False

    modified = False
    for key, entry in unalerted:
        payload = build_quarantine_alert_payload(key, str(entry.get("reason", "unknown")))
        outcomes = attempt_deliveries(alert_targets, payload)
        if any(outcome.ok for outcome in outcomes):
            entry["alerted_at"] = _utc_now_iso()
            modified = True
            log_ok(f"Told the surviving webhook(s) that {key} is disabled.")
        else:
            log_warn(f"Could not deliver the quarantine alert for {key}; will retry next run.")
    return modified


# --- CLI / main -----------------------------------------------------------


def parse_args():
    parser = argparse.ArgumentParser(description="Check for new One Piece chapters and notify Discord.")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Fetch and print the latest chapter info, but do not send a notification or update state.",
    )
    parser.add_argument(
        "--check-webhooks",
        action="store_true",
        help="Check every configured Discord webhook (sends no messages, writes no state) and exit.",
    )
    parser.add_argument(
        "--force-state",
        metavar="CHAPTER_NUMBER",
        type=float,
        help="Overwrite the stored state to pretend the last-seen chapter was CHAPTER_NUMBER, "
        "then exit. Useful to make the *next* real run treat the actual latest chapter as new.",
    )
    return parser.parse_args()


def _force_state(n: float) -> int:
    n = int(n) if n == int(n) else n
    try:
        existing = load_state()
    except ScraperError:
        existing = None
    quarantined = existing.get("quarantined", {}) if existing else {}
    fake_chapter = {
        "chapter_number": n,
        "title": f"One Piece Chapter {n}",
        "url": SERIES_URL,
        "pub_date": "",
    }
    save_state(fake_chapter, [], quarantined)
    log_ok(f"Forced stored state to chapter {n}. Next normal run will compare against this.")
    return 0


def main() -> int:
    args = parse_args()

    if args.force_state is not None:
        return _force_state(args.force_state)

    if args.check_webhooks:
        return run_webhook_diagnostic()

    try:
        latest = fetch_latest_chapter()
    except ScraperError as e:
        log_error(f"Scraper failure -- NOT the same as 'no new chapter'. Details: {e}")
        return 1

    if args.dry_run:
        log_info("Dry run -- fetched latest chapter, not sending notification or updating state.")
        print(json.dumps(latest, indent=2))
        return 0

    try:
        state = load_state()
    except ScraperError as e:
        log_error(str(e))
        return 1

    targets = get_webhook_targets()
    target_map = dict(targets)

    stored_quarantine = state.get("quarantined", {}) if state else {}
    quarantined = reconcile_quarantine(stored_quarantine, targets)

    stored_number = state.get("chapter_number") if state else None
    kind = classify_chapter(latest["chapter_number"], stored_number)

    if kind == "older":
        log_warn(
            f"Feed reported chapter {latest['chapter_number']}, older than the stored "
            f"{stored_number}; ignoring it (feed reorder?) and keeping state as-is."
        )
        return 0

    if kind == "same":
        chapter = {
            "chapter_number": stored_number,
            "title": state.get("title", ""),
            "url": state.get("url", ""),
            "pub_date": state.get("pub_date", ""),
        }
        delivered_to = [key for key in state.get("delivered_to", []) if isinstance(key, str)]
    else:
        chapter = latest
        delivered_to = []

    configured_keys = {key for key, _ in targets}
    delivered_to = [key for key in delivered_to if key in configured_keys]

    active = [(key, url) for key, url in targets if key not in quarantined]
    pending = compute_pending(active, delivered_to)

    if not pending:
        if kind in ("new", "missing"):
            # Nothing can be announced: no webhooks configured, or all of them
            # are quarantined. Don't advance the state, so this keeps failing
            # loudly until a working webhook exists.
            if not targets:
                log_error(
                    "No Discord webhook URLs configured "
                    "(DISCORD_WEBHOOK_URL / DISCORD_WEBHOOK_URL_2)."
                )
            else:
                log_error(
                    "Every configured Discord webhook is quarantined; nothing can "
                    f"announce chapter {latest['chapter_number']}. Fix or replace the "
                    "repo secret(s) -- quarantine clears automatically when a URL changes."
                )
            return 1
        log_ok(
            f"No new chapter (still {chapter['chapter_number']}) and nothing pending. "
            "No action taken."
        )
        if _send_quarantine_alerts(quarantined, targets):
            save_state(chapter, delivered_to, quarantined)
        return 0

    outcomes = attempt_deliveries(pending, build_new_chapter_payload(chapter))
    for outcome in outcomes:
        if outcome.ok:
            log_ok(f"Notified {outcome.key} ({mask_webhook(target_map[outcome.key])}).")

    delivered_to = sorted(set(delivered_to) | {outcome.key for outcome in outcomes if outcome.ok})
    permanent_failures = [o for o in outcomes if not o.ok and o.permanent]
    retry_failures = [o for o in outcomes if not o.ok and not o.permanent]

    if permanent_failures:
        failed_keys = {outcome.key for outcome in permanent_failures}
        survivors = [key for key, _ in active if key not in failed_keys]
        if survivors:
            for outcome in permanent_failures:
                quarantined[outcome.key] = {
                    "since": _utc_now_iso(),
                    "reason": outcome.detail,
                    "fingerprint": webhook_fingerprint(target_map[outcome.key]),
                    "alerted_at": None,
                }
                log_error(
                    f"Quarantining {outcome.key}: {outcome.detail} -- notifications "
                    f"continue via {', '.join(survivors)}."
                )
        else:
            # Never quarantine the last usable destination: with no working
            # path left, this must stay loud every run.
            retry_failures.extend(permanent_failures)
            for outcome in permanent_failures:
                log_error(
                    f"{outcome.key} failed ({outcome.detail}) and no other webhook is "
                    "usable; not quarantining so this keeps alerting."
                )

    # Save BEFORE deciding the exit code: a webhook that already got the
    # message must never be notified again because a different one failed.
    save_state(chapter, delivered_to, quarantined)

    if _send_quarantine_alerts(quarantined, targets):
        save_state(chapter, delivered_to, quarantined)

    if retry_failures:
        details = " | ".join(f"{outcome.key}: {outcome.detail}" for outcome in retry_failures)
        log_error(
            f"Delivery failed for {len(retry_failures)}/{len(pending)} webhook(s). "
            "State was saved, so the webhook(s) that succeeded will NOT be re-notified; "
            f"the failed one(s) will be retried next run. Details: {details}"
        )
        return 1

    log_ok(
        f"New chapter recorded: {chapter.get('title') or chapter.get('chapter_number')} "
        f"({chapter.get('url', '')}). Delivered to {len(delivered_to)} webhook(s); "
        "state updated."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
