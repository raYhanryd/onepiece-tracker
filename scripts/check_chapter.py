#!/usr/bin/env python3
"""
One Piece chapter tracker -> Discord notifier.

Checks weebcentral's RSS feed for the One Piece series, compares the
latest chapter against a stored state file, and posts a Discord
notification via webhook when a new chapter is found.

Uses Python stdlib only: urllib, xml.etree.ElementTree, json, argparse.

Exit codes:
  0 = success (whether or not a new chapter was found)
  1 = failure (feed unreachable, unparsable, or malformed) -- this is
      NOT the same as "no new chapter" and must be treated as an alert.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import urllib.request
import urllib.error
from datetime import datetime, timezone
from xml.etree import ElementTree

# --- Config -----------------------------------------------------------

RSS_URL = "https://weebcentral.com/series/01J76XY7E9FNDZ1DBBM6PBJPFK/rss"
SERIES_URL = "https://weebcentral.com/series/01J76XY7E9FNDZ1DBBM6PBJPFK/One-Piece"
STATE_PATH = os.path.join(os.path.dirname(__file__), "..", "state", "last_chapter.json")

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)

DISCORD_COLOR_NEW_CHAPTER = 0xE67E22  # orange, "new release" feel

CHAPTER_NUM_RE = re.compile(r"Chapter\s+(\d+(?:\.\d+)?)", re.IGNORECASE)


# --- Logging helpers ----------------------------------------------------

def log_info(msg: str) -> None:
    print(f"[INFO] {msg}", flush=True)


def log_ok(msg: str) -> None:
    print(f"[OK] {msg}", flush=True)


def log_error(msg: str) -> None:
    # Loud, unmistakable in GitHub Actions logs.
    print(f"\n{'=' * 60}\n[ERROR] {msg}\n{'=' * 60}\n", file=sys.stderr, flush=True)


# --- Core logic -----------------------------------------------------------

class ScraperError(Exception):
    """Raised when the feed can't be fetched or parsed reliably."""


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
    if not os.path.exists(STATE_PATH):
        return None
    try:
        with open(STATE_PATH, "r") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError) as e:
        raise ScraperError(f"Failed to read/parse state file at {STATE_PATH}: {e}") from e


def save_state(chapter: dict) -> None:
    os.makedirs(os.path.dirname(STATE_PATH), exist_ok=True)
    payload = {
        "chapter_number": chapter["chapter_number"],
        "title": chapter["title"],
        "url": chapter["url"],
        "pub_date": chapter["pub_date"],
        "checked_at": datetime.now(timezone.utc).isoformat(),
    }
    with open(STATE_PATH, "w") as f:
        json.dump(payload, f, indent=2)
        f.write("\n")


def send_discord_notification(chapter: dict, webhook_url: str) -> None:
    embed = {
        "title": chapter["title"],
        "url": chapter["url"],
        "description": "A new One Piece chapter is out.",
        "color": DISCORD_COLOR_NEW_CHAPTER,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "footer": {"text": "weebcentral"},
    }
    payload = {"embeds": [embed]}

    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        webhook_url,
        data=data,
        headers={"Content-Type": "application/json", "User-Agent": USER_AGENT},
        method="POST",
    )

    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            # Discord webhooks return 204 No Content on success.
            if resp.status not in (200, 204):
                raise ScraperError(f"Discord webhook returned unexpected status {resp.status}")
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")
        raise ScraperError(f"Discord webhook HTTP error {e.code}: {body}") from e
    except urllib.error.URLError as e:
        raise ScraperError(f"Network error sending Discord notification: {e.reason}") from e


# --- CLI / main -----------------------------------------------------------

def parse_args():
    parser = argparse.ArgumentParser(description="Check for new One Piece chapters and notify Discord.")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Fetch and print the latest chapter info, but do not send a notification or update state.",
    )
    parser.add_argument(
        "--force-state",
        metavar="CHAPTER_NUMBER",
        type=float,
        help="Overwrite the stored state to pretend the last-seen chapter was CHAPTER_NUMBER, "
        "then exit. Useful to make the *next* real run treat the actual latest chapter as new.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()

    if args.force_state is not None:
        n = args.force_state
        n = int(n) if n == int(n) else n
        fake_chapter = {
            "chapter_number": n,
            "title": f"One Piece Chapter {n}",
            "url": SERIES_URL,
            "pub_date": "",
        }
        save_state(fake_chapter)
        log_ok(f"Forced stored state to chapter {n}. Next normal run will compare against this.")
        return 0

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
        previous = load_state()
    except ScraperError as e:
        log_error(str(e))
        return 1

    if previous is not None and previous.get("chapter_number") == latest["chapter_number"]:
        log_ok(f"No new chapter (still {latest['chapter_number']}). No action taken.")
        return 0

    webhook_url = os.environ.get("DISCORD_WEBHOOK_URL")
    if not webhook_url:
        log_error("DISCORD_WEBHOOK_URL environment variable is not set.")
        return 1

    try:
        send_discord_notification(latest, webhook_url)
    except ScraperError as e:
        log_error(f"Failed to send Discord notification: {e}")
        return 1

    save_state(latest)
    log_ok(f"New chapter found: {latest['title']} ({latest['url']}). Notified Discord and updated state.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
