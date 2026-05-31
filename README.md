# F1 Documents Scraper

Polls the FIA Formula 1 decision-documents page, downloads new PDFs to
`~/Desktop/F1 Documents/<Event>/`, and sends a phone push (via ntfy) whenever a
**penalty or steward decision** is published.

No third-party Python packages — standard library only.

## How it's split (two parts)

| Part | Where | Job |
|---|---|---|
| **Laptop agent** (launchd) | this Mac | Downloads PDFs to the Desktop. Runs silently (`F1_NOTIFY=0`) — sends **no** notifications. Only runs while the Mac is awake. |
| **Cloud notifier** (GitHub Action) | GitHub, 24/7 | The single source of phone notifications. Checks every ~5 min and pushes ntfy alerts for penalties/steward decisions. Does **not** download. |

This split avoids duplicate alerts and means you're notified even when the
laptop is off — while the files still land on your Desktop whenever it's awake.
The two keep separate state files so they don't interfere
(`state.json` local, `notify_state.json` in the repo / committed by the Action).

## Phone notifications (one-time setup)

1. Install the **ntfy** app: [iOS](https://apps.apple.com/app/ntfy/id1625396347) / [Android](https://play.google.com/store/apps/details?id=io.heckel.ntfy).
2. In the app, **Subscribe to topic** and enter your private topic name.

   The topic is **not** stored in this repo (the repo is public). It lives in:
   - the `F1_NTFY_TOPIC` **GitHub Actions secret** (used by the cloud notifier), and
   - the `F1_NTFY_TOPIC` env var in the launchd plist (used by the laptop for
     `--test-notify`).

   Both must be set to the same value. Anyone who knows the topic can read/send
   to it, so keep it out of any committed file.
3. Test it: `F1_NTFY_TOPIC=your-topic python3 f1_docs_scraper.py --test-notify`.

## What counts as "important"

**Without Gemini (default):** title/filename contains any of: decision, penalty,
infringement, offence, summons, reprimand, disqualif, protest, right of review,
stewards. Edit `IMPORTANT_KEYWORDS` in the script to tune.

**With Gemini (optional, free):** the cloud notifier reads each new PDF with the
Gemini API and decides whether it's a penalty/steward decision — catching docs
the keyword list misses — and writes a one-line summary used as the push body
(e.g. *"5s time penalty for Car 4 (Norris) — track limits, Turn 4"*). If the key
is missing or the API errors/rate-limits, it automatically falls back to
keyword matching, so notifications never break.

### Enabling Gemini

1. Get a free API key at <https://aistudio.google.com/apikey>.
2. Add it as a repo secret: `gh secret set GEMINI_API_KEY` (paste the key), or via
   GitHub → Settings → Secrets and variables → Actions.
3. (Optional) If the default model is ever unavailable, set a repo *variable*
   `GEMINI_MODEL` to a valid model name. The script otherwise tries
   `gemini-2.5-flash` → `gemini-2.0-flash` → `gemini-flash-latest`.

Volume is a few dozen PDFs per race weekend — comfortably within the free tier.
Only the cloud notifier uses Gemini; the laptop agent stays keyword-only.

## Running manually

```bash
python3 f1_docs_scraper.py            # one check
python3 f1_docs_scraper.py --watch    # loop every 120s
python3 f1_docs_scraper.py --watch 60 # loop every 60s
```

## Background agent (already installed)

A launchd agent runs the scraper every 120 seconds and at login.

| Action | Command |
|---|---|
| Status | `launchctl list \| grep f1docs` |
| Stop / disable | `launchctl unload ~/Library/LaunchAgents/com.adamj.f1docs.plist` |
| Start / enable | `launchctl load ~/Library/LaunchAgents/com.adamj.f1docs.plist` |
| Live log | `tail -f "scraper.log"` |

- `state.json` — record of every document already seen/downloaded. Delete it to
  re-download everything (and re-seed without notifications).
- First run seeds existing docs **without** sending pushes; only docs that
  appear afterward trigger a notification.
- **Retention:** PDFs older than `RETENTION_DAYS` (default 30) are deleted on
  every run, and empty event folders are pruned. State entries are kept so old
  docs are never re-downloaded. Change `RETENTION_DAYS` in the script to adjust.

## If it stops finding documents

The scraper depends on the FIA page's HTML layout. If FIA changes it, the log
will show `No documents parsed` — the regexes in `parse_documents()` need
updating.
