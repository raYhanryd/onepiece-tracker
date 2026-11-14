# One Piece Chapter Tracker → Discord

A zero-server GitHub Actions bot that watches the One Piece RSS feed and posts a clean Discord alert when a new chapter is released. No server, database, or paid hosting — the repository itself is the state store.

The project is designed to be copied: each person runs their own instance with their own Discord webhook(s), optional role ping, and schedule. It is intentionally One Piece-only today; see [Tracking another series](#tracking-another-series).

## What you get

- A Discord embed with the chapter title, link, and a short “new chapter” message.
- One or two independent Discord destinations. The second can ping a role; the first stays mention-free.
- Per-webhook delivery tracking, so a failure on one webhook never re-notifies the ones that already succeeded.
- Automatic retries for transient errors, and quarantine for webhooks that are permanently dead (401/403/404).
- Loud failures for feed errors and misconfiguration. “No new chapter” and “the check broke” are deliberately different outcomes.
- 43 offline unit tests, no network or Discord account required.
- Python standard library only. Nothing to install.

## Quick start

### 1. Create your own copy

On GitHub, **Fork** this repository (or use **Use this template → Create a new repository** if it is enabled).

If you fork, open the **Actions** tab and enable workflows first — GitHub disables scheduled workflows on forks by default. A template-created repository is a fresh repository; check that Actions is enabled in its **Actions** tab.

Do not put webhook URLs in the repository, issues, or logs. They are bearer credentials.

### 2. Add at least one Discord webhook

1. In Discord: **Server Settings → Integrations → Webhooks → New Webhook**.
2. Pick the channel, create the webhook, and copy its URL.
3. In your GitHub repo: **Settings → Secrets and variables → Actions → New repository secret**.
   - Name: `DISCORD_WEBHOOK_URL`
   - Value: the webhook URL from Discord

That is the minimum configuration. The workflow already passes the secret into the script.

### 3. Optional: add a second destination and a role ping

Repeat the Discord webhook steps in the second server/channel, then add a second repository secret:

- Name: `DISCORD_WEBHOOK_URL_2`
- Value: the second webhook URL

The script treats a configured webhook 2 as **expecting a role ping**. Add a repository **variable** (not a secret):

- Go to **Settings → Secrets and variables → Actions → Variables → New repository variable**
- Name: `DISCORD_ROLE_ID_2`
- Value: the numeric Discord role ID (for example, the ID copied with Developer Mode enabled)

The role ID is not a credential. If it is missing or non-numeric, the chapter alert is still delivered without a ping, but the run exits non-zero so the problem is visible. `--send-test` and `--check-webhooks` refuse to pass in that state. If you want webhook 2 without a ping, remove the `DISCORD_WEBHOOK_URL_2` entry from `WEBHOOK_ROLE_ENV_VARS` in `scripts/check_chapter.py`.

If the mention appears but nobody is notified, make the role mentionable: **Role settings → Display → Allow anyone to @mention this role**.

### 4. Confirm Actions can commit state

The workflow needs to write `state/last_chapter.json` back to the repo after a new chapter is found. The workflow already declares:

```yaml
permissions:
  contents: write
```

If your organization restricts workflow permissions, allow read/write under **Settings → Actions → General → Workflow permissions**.

### 5. Test before waiting for a chapter

From the **Actions** tab, open **Check for new One Piece chapter → Run workflow** and choose:

1. `check-webhooks` — verifies every configured webhook without sending a message.
2. `send-test` — sends a clearly marked `[TEST]` alert through the real delivery path. It does not touch the chapter state.

The same checks are available locally (see [Testing locally](#testing-locally)).

The repository ships with the state at the latest recorded chapter, so a new copy will not announce chapters that were already recorded. To exercise a real “new chapter” run for your own webhook, use `--force-state` with a lower number, then run a normal check. That will send an alert; only do it when you are ready.

## Configuration reference

| Kind | Name | Required | Purpose |
|---|---|---|---|
| Secret | `DISCORD_WEBHOOK_URL` | Yes, or use webhook 2 instead | First Discord destination |
| Secret | `DISCORD_WEBHOOK_URL_2` | No | Second Discord destination |
| Variable | `DISCORD_ROLE_ID_2` | Yes when webhook 2 is set | Role ping for webhook 2 |
| File | `state/last_chapter.json` | Yes | Last-seen chapter and per-webhook delivery ledger |

At least one webhook must be configured. Unset webhooks are skipped, so one secret means one destination. Adding a third destination requires extending `WEBHOOK_ENV_VARS` in `scripts/check_chapter.py`, adding the secret, and adding the env line in the workflow.

## How it works

1. GitHub Actions wakes up on the cron schedule, or from **Run workflow**.
2. `scripts/check_chapter.py` fetches weebcentral’s RSS feed with a browser-like `User-Agent`.
3. It extracts the latest chapter number, title, URL, and publish date.
4. It compares that chapter with `state/last_chapter.json`.
5. If the chapter is new, it builds the Discord payload per destination: webhook 2 gets the optional role mention, webhook 1 does not.
6. It records each successful delivery independently in `delivered_to`.
7. It commits the updated state file with `[skip ci]`. The workflow serializes runs with a `concurrency` group and rebases before pushing.

If there is no new chapter, the script retries only destinations that are still pending from an earlier failed run, then exits successfully when there is nothing left to do.

### Why RSS instead of HTML scraping

weebcentral exposes a per-series RSS feed with clean titles, links, and publish dates. That is more stable than guessing at page selectors. The feed is still fetched with a real browser `User-Agent`, because some weebcentral-adjacent infrastructure rejects bare requests.

The most likely maintenance point is the chapter-number regex (`CHAPTER_NUM_RE`) in `scripts/check_chapter.py`. If the site starts publishing titles like “Ch. 1199” instead of “Chapter 1199”, that is the line to update.

### Reliability details

- **State is a delivery ledger, not one boolean.** `delivered_to` lists the webhook env-var names that already received the current chapter.
- **Transient failures** (timeouts, 429, 5xx) are retried in-run with backoff, then retried on the next scheduled run — only for the destination that failed.
- **Permanent failures** (401/403/404) quarantine that webhook. The surviving channel gets one heads-up; the run stays green. Replacing the secret changes the stored URL fingerprint, which clears the quarantine automatically.
- **The last usable webhook is never quarantined.** With no working destination, the run keeps failing loudly rather than silently dropping alerts.
- **Feed failures are alerts.** A fetch or parse error exits 1 and logs an `[ERROR]` block; it is never treated as “no new chapter”.
- **Missing role ping is loud too.** The chapter is delivered without the ping, then the run exits 1, so a silent configuration regression is visible.

Webhook URLs are never written to the repo. Quarantine entries contain only the env-var name and a truncated SHA-256 fingerprint of the URL.

## Testing locally

You need Python 3.10+; there are no third-party dependencies.

```bash
# Offline unit tests — no network, Discord, or credentials needed
python3 -m unittest discover -s tests -v

# Fetch and print the latest chapter; no message, no state change
python3 scripts/check_chapter.py --dry-run

# Check configured webhooks without sending anything
export DISCORD_WEBHOOK_URL="https://discord.com/api/webhooks/..."
python3 scripts/check_chapter.py --check-webhooks

# Send a marked [TEST] alert through the real delivery path
export DISCORD_WEBHOOK_URL="https://discord.com/api/webhooks/..."
export DISCORD_WEBHOOK_URL_2="https://discord.com/api/webhooks/..."
export DISCORD_ROLE_ID_2="YOUR_ROLE_ID"  # only if webhook 2 expects a ping
python3 scripts/check_chapter.py --send-test

# Pretend the last-seen chapter was one behind, so the next real run sends
python3 scripts/check_chapter.py --force-state 1197
```

`--send-test` never fetches the feed and never writes state. It refuses to send if an expected role ping is misconfigured, so it cannot pass with a silent no-ping.

`--force-state` **does** write state. If you use it locally, restore the real state before committing, or run the next check with your production webhook environment to let it advance normally. Do not use it on a shared production checkout just to “see” a notification.

## Schedule

The workflow runs five checks per day, in UTC:

| Cron | UTC |
|---|---|
| `0 6 * * *` | 06:00 |
| `30 10 * * *` | 10:30 |
| `0 15 * * *` | 15:00 |
| `30 19 * * *` | 19:30 |
| `45 23 * * *` | 23:45 |

Edit the `on.schedule` block in `.github/workflows/check-chapter.yml` to change the times. GitHub’s scheduled runs can be delayed, especially at busy times; the schedule is a polling interval, not an exact delivery time. Each run still de-duplicates against the committed state.

## State file

`state/last_chapter.json` is committed by the workflow and drives the “already notified” logic:

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

- `delivered_to` — webhook env-var names that already received this chapter. A listed webhook is never notified about it again.
- `quarantined` — permanently dead webhooks, keyed by env-var name, with a reason and a truncated URL fingerprint. The URL itself is never stored.
- Older state files without these fields are read as “nothing delivered, nothing quarantined”.

## Tracking another series

This version is deliberately scoped to One Piece. The feed URL and display text are constants in `scripts/check_chapter.py`:

- `RSS_URL` and `SERIES_URL` — weebcentral’s series feed/page IDs
- the `One Piece` labels in the embed, test payload, and forced-state title

Changing those can adapt the same single-series pipeline to another weebcentral series with the same RSS shape. Running several series from one instance needs a different state model (one latest chapter per series, plus per-series delivery tracking) and is not implemented. The public website idea below is a larger redesign, not a flag flip.

## Hosted website and multiple series

A hosted website where people sign in and configure everything — instead of editing GitHub secrets and variables — is possible, but it is a different architecture from this repository:

- accounts and authentication, so each person owns their configuration;
- a database for users, series, subscriptions, and per-series delivery state;
- encrypted-at-rest storage for Discord webhook URLs, with rotation and revocation;
- a scheduler and worker/queue that poll each distinct series feed and fan out notifications;
- per-series parsing and state (the current state file is a single series);
- a decision on Discord setup: pasting a webhook URL is much simpler than building a Discord bot with OAuth and Manage Webhooks permissions.

That version could support many series and many users, but it would no longer be “a GitHub Action with a JSON file.” This repository is the zero-server, single-series edition.

## Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| Workflow does not run on schedule | Forked repo, Actions disabled, or repo archived | Open **Actions** and enable workflows; check the cron block |
| “No Discord webhook URLs configured” | No secret, or all webhooks quarantined | Add `DISCORD_WEBHOOK_URL`; run `--check-webhooks` |
| Chapter delivered but role did not ping | Role ID missing/invalid, or role is not mentionable | Set `DISCORD_ROLE_ID_2`; allow the role to be mentioned |
| Chapter delivered but run is red | Expected role ping was misconfigured | Fix the variable; the delivery itself succeeded |
| No notification, no error | State already at the latest chapter | Check `state/last_chapter.json`; use `--force-state` only for testing |
| “RSS feed returned HTTP 403/404” | Feed URL retired or blocked | Verify weebcentral’s feed link; update `RSS_URL` |
| “Could not extract a chapter number” | Feed title format changed | Update `CHAPTER_NUM_RE` |
| State file not committing | `contents: write` blocked | Check workflow permissions |
| Duplicate notifications | State commit skipped after a failed delivery | Verify `continue-on-error: true` on the check step and `if: always()` on the commit step |

## Files

```
scripts/check_chapter.py           # RSS fetch, state diff, Discord delivery
state/last_chapter.json            # last-seen chapter + per-webhook delivery ledger
tests/test_check_chapter.py        # offline unit tests
.github/workflows/check-chapter.yml
docs/SHOWCASE.md                   # project write-up and graphics
README.md
LICENSE
```

See [`docs/SHOWCASE.md`](docs/SHOWCASE.md) for a longer project overview, architecture graphics, and showcase copy.

## License

MIT — see [`LICENSE`](LICENSE).
