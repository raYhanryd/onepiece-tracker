# One Piece Chapter Tracker → Discord

Checks weebcentral's One Piece RSS feed on a schedule and posts a Discord
notification when a new chapter drops. Runs entirely on GitHub Actions —
no server, no paid hosting. Same pattern as `email-digest-gemini`.

## How it works

- `scripts/check_chapter.py` fetches weebcentral's series RSS feed
  (`/series/<id>/rss`), which is far more stable than scraping the HTML
  page directly.
- It compares the latest chapter number to what's stored in
  `state/last_chapter.json`.
- If it's new: sends a Discord embed notification to every configured
  webhook, then updates and commits the state file back to the repo.
- If it's not new: does nothing (after retrying any delivery that failed
  on an earlier run).
- The state file records *which* webhooks already received the current
  chapter, so a webhook that fails can never cause duplicate messages in
  the ones that succeeded. Failed deliveries are retried on the next run,
  only for the webhook(s) that actually failed.
- If the feed can't be fetched or parsed (site down, feed structure
  changed, etc.): this is treated as a **failure**, not "no new chapter."
  The script exits non-zero and logs a loud `[ERROR]` block, so the
  GitHub Actions run shows as failed and you'll get notified.
- If a webhook is *permanently* dead (HTTP 401/403/404 — deleted webhook
  or channel), it is **quarantined**: the script stops retrying it, keeps
  the other webhook(s) working, tells the surviving channel about it once,
  and the run still succeeds. Delete or replace that secret and the
  quarantine clears by itself.

## Why RSS instead of HTML scraping

The original plan was to scrape the series page directly, but weebcentral
exposes a proper RSS feed per series (linked from the series page under
"Track"). It gives clean chapter titles, direct chapter URLs, and publish
dates — no need to guess at CSS selectors or handle layout changes. It's
still fetched with a real browser `User-Agent` header, since some
weebcentral-adjacent infra 403s bare requests.

**Maintenance note:** if weebcentral changes their RSS feed path/format or
retires it, `fetch_latest_chapter()` in `scripts/check_chapter.py` is the
only place that needs updating — the regex extracting the chapter number
from the item title (`CHAPTER_NUM_RE`) is the most likely thing to break.

## Setup

### 1. Create the repo

```bash
cd onepiece-tracker
git init
git add .
git commit -m "Initial commit: One Piece chapter tracker"
gh repo create onepiece-tracker --private --source=. --remote=origin
git push -u origin main
```

(Or create the private repo on github.com first, then `git remote add origin <url>` and push.)

### 2. Add the Discord webhook secret(s)

1. In Discord: Server Settings → Integrations → Webhooks → New Webhook.
   Pick the channel you want notifications in, copy the webhook URL.
2. In GitHub: repo → Settings → Secrets and variables → Actions →
   New repository secret.
   - Name: `DISCORD_WEBHOOK_URL`
   - Value: the webhook URL from Discord

The script reads this from the environment — it's never hardcoded
anywhere in the repo.

**Sending to a second server/channel too:** repeat step 1 in the other
Discord server to get a second webhook URL, then add it as a second
repo secret named `DISCORD_WEBHOOK_URL_2`. The workflow already passes
it through, and the script sends the same notification to every
*configured* webhook — with only one secret set, the second destination
simply doesn't exist. To add a third, fourth, etc., extend the
`WEBHOOK_ENV_VARS` tuple in `scripts/check_chapter.py` and add a matching
secret + env line in `.github/workflows/check-chapter.yml`.

Per-destination delivery tracking means a failure on one webhook never
re-notifies the others, and a permanently dead webhook gets quarantined
(with a one-time heads-up in the surviving channel) instead of failing
every run.

### 3. Permissions for `GITHUB_TOKEN`

The workflow needs to commit the updated `state/last_chapter.json` back
to the repo after a new chapter is found. This requires:

```yaml
permissions:
  contents: write
```

This is already set at the top of `.github/workflows/check-chapter.yml`.
Without it, the push step in the workflow will fail with a permissions
error. (Note: if your org/repo has a stricter default workflow
permissions setting under Settings → Actions → General, make sure
"Read and write permissions" is allowed, or this `permissions:` block
being scoped correctly may still get overridden.)

## Testing locally before relying on the schedule

You'll need Python 3.10+ (for the `dict | None` type hint used in the
script — bump the annotation or drop it if you're on an older Python).

**1. Dry run — see what the script would do, without side effects:**

```bash
python3 scripts/check_chapter.py --dry-run
```

This fetches the real latest chapter from weebcentral and prints it.
No Discord message sent, no state file changes.

**2. Simulate "there IS a new chapter":**

```bash
# Pretend the last-seen chapter was one behind the real latest
python3 scripts/check_chapter.py --force-state 1185

# Now run normally — it should detect the real latest as "new"
export DISCORD_WEBHOOK_URL="https://discord.com/api/webhooks/..."
python3 scripts/check_chapter.py
```

Check your Discord channel for the notification, and check
`state/last_chapter.json` — it should now reflect the real latest chapter.

**3. Confirm "no new chapter" doesn't misfire:**

```bash
# Run again immediately, right after the run above
python3 scripts/check_chapter.py
```

You should see `[OK] No new chapter (still <N>). No action taken.` in the
logs, and no second Discord message.

**4. Confirm failures are loud:**

Temporarily break something — e.g. set `DISCORD_WEBHOOK_URL` to garbage,
or point `RSS_URL` in the script at a bad URL — and confirm you get an
`[ERROR]` block and a non-zero exit code (`echo $?` after running should
print `1`).

**5. Check your webhooks without sending anything:**

```bash
export DISCORD_WEBHOOK_URL="https://discord.com/api/webhooks/..."
python3 scripts/check_chapter.py --check-webhooks
```

This GETs each configured webhook and reports OK or DEAD (`HTTP 404`
usually means the webhook was deleted or its channel is gone). No
messages are sent and the state file is not touched.

**6. Run the unit tests:**

```bash
python3 -m unittest discover -s tests -v
```

The suite is fully offline: it fakes the network layer and covers
per-webhook delivery tracking, quarantine, retries, and the
no-duplicate-messages behaviour.

## Running manually on GitHub

The workflow includes `workflow_dispatch`, so you can trigger it by hand:
repo → Actions → "Check for new One Piece chapter" → Run workflow. The
`mode` input chooses what runs:

- `check` (default) — the normal check.
- `dry-run` — fetch and print the latest chapter, send nothing.
- `check-webhooks` — GET each configured webhook without sending a
  message; reports OK or DEAD per secret. Use this after rotating a
  webhook URL, or when you suspect one is broken.

Good for confirming the scheduled workflow is wired up correctly before
waiting for the next cron tick.

## Schedule

Cron times are UTC, roughly spread across the day (5 checks/day):
`06:00`, `10:30`, `15:00`, `19:30`, `23:45` UTC.
Edit the `on.schedule` block in `.github/workflows/check-chapter.yml` to
change this.

## State file format

`state/last_chapter.json` is committed back to the repo whenever there is
something to record, and drives the "already notified" logic:

```json
{
  "chapter_number": 1194,
  "title": "One Piece Chapter 1194",
  "url": "https://weebcentral.com/chapters/...",
  "pub_date": "Sat, 26 Sep 2026 03:16:36 +0000",
  "delivered_to": ["DISCORD_WEBHOOK_URL"],
  "quarantined": {},
  "checked_at": "2026-09-30T07:00:00+00:00"
}
```

- `delivered_to` — env-var names of the webhooks that already received
  this chapter. A webhook listed here is never notified about it again.
- `quarantined` — webhooks that failed permanently, with the reason and a
  truncated SHA-256 fingerprint of their URL (the URL itself is never
  written to the repo). Replacing the secret changes the fingerprint,
  which clears the entry and starts retrying that destination.

Old state files (without these fields) are read as "nothing delivered,
nothing quarantined".

## Files

```
scripts/check_chapter.py       # scraper + state diff + Discord notify
state/last_chapter.json        # persisted "last seen" chapter + delivery tracking
tests/test_check_chapter.py    # offline unit tests for the above
.github/workflows/check-chapter.yml
.gitignore
README.md
```
