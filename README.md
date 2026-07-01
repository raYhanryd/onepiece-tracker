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
- If it's new: sends a Discord embed notification, then updates and
  commits the state file back to the repo.
- If it's not new: does nothing.
- If the feed can't be fetched or parsed (site down, feed structure
  changed, etc.): this is treated as a **failure**, not "no new chapter."
  The script exits non-zero and logs a loud `[ERROR]` block, so the
  GitHub Actions run shows as failed and you'll get notified.

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

### 2. Add the Discord webhook secret

1. In Discord: Server Settings → Integrations → Webhooks → New Webhook.
   Pick the channel you want notifications in, copy the webhook URL.
2. In GitHub: repo → Settings → Secrets and variables → Actions →
   New repository secret.
   - Name: `DISCORD_WEBHOOK_URL`
   - Value: the webhook URL from Discord

The script reads this from the environment — it's never hardcoded
anywhere in the repo.

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

## Running manually on GitHub

The workflow includes `workflow_dispatch`, so you can trigger it by hand:
repo → Actions → "Check for new One Piece chapter" → Run workflow.
Good for confirming the scheduled workflow is wired up correctly before
waiting for the next cron tick.

## Schedule

Cron times are UTC, roughly spread across the day (5 checks/day):
`06:00`, `10:30`, `15:00`, `19:30`, `23:45` UTC.
Edit the `on.schedule` block in `.github/workflows/check-chapter.yml` to
change this.

## Files

```
scripts/check_chapter.py       # scraper + state diff + Discord notify
state/last_chapter.json        # persisted "last seen" chapter
.github/workflows/check-chapter.yml
.gitignore
README.md
```
