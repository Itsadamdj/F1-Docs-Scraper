#!/usr/bin/env python3
"""
F1 document scraper.

Polls the FIA Formula 1 decision-documents page, downloads any newly published
PDFs to a folder on the Desktop (organized per event), and sends a phone push
notification (via ntfy.sh) whenever an *important* document appears — where
"important" means a penalty or a steward decision.

Standard library only — no pip installs required.

Usage:
    python3 f1_docs_scraper.py            # do a single check (used by launchd)
    python3 f1_docs_scraper.py --watch    # loop forever, checking every INTERVAL
    python3 f1_docs_scraper.py --watch 60 # loop, checking every 60 seconds
    python3 f1_docs_scraper.py --test-notify   # send a test push and exit
"""

import json
import os
import re
import sys
import time
import html
import base64
import urllib.request
import urllib.error
from datetime import datetime, timezone
from pathlib import Path

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

# FIA Formula 1 World Championship decision-documents page. The page defaults to
# the most recent event, so this URL always reflects the current Grand Prix.
FIA_URL = "https://www.fia.com/documents/championships/fia-formula-one-world-championship-14"
FIA_BASE = "https://www.fia.com"

# Where downloaded PDFs are saved (one subfolder per event).
DOWNLOAD_DIR = Path.home() / "Desktop" / "F1 Documents"

# State + log live next to this script. F1_STATE_FILE lets the GitHub Action use
# its own separate state file (notify_state.json) so cloud and laptop don't clash.
SCRIPT_DIR = Path(__file__).resolve().parent
STATE_FILE = Path(os.environ.get("F1_STATE_FILE") or (SCRIPT_DIR / "state.json"))

# --- ntfy push configuration ----------------------------------------------
# ntfy.sh is a free, no-account push service. The topic is intentionally NOT
# stored in this (public) repo — it comes from the F1_NTFY_TOPIC env var:
#   * GitHub Action: from the F1_NTFY_TOPIC repository secret.
#   * Laptop agent:  from EnvironmentVariables in the launchd plist (local).
# If unset, NTFY_TOPIC stays "CHANGE_ME" and send_push() does nothing.
NTFY_TOPIC = os.environ.get("F1_NTFY_TOPIC") or "CHANGE_ME"
NTFY_SERVER = os.environ.get("F1_NTFY_SERVER") or "https://ntfy.sh"

# Set F1_NOTIFY=0 to download silently (the laptop agent uses this so that the
# cloud GitHub Action is the single source of phone notifications).
NOTIFY_ENABLED = os.environ.get("F1_NOTIFY", "1") != "0"

# How often --watch mode checks (seconds).
DEFAULT_INTERVAL = 120

# Downloaded PDFs older than this many days are deleted on each run.
RETENTION_DAYS = 30

# A document is "important" if its title or filename contains any of these
# (case-insensitive). Tuned for FIA naming: steward decisions are titled
# "Decision", and penalties show up as Offence / Infringement / Summons / etc.
IMPORTANT_KEYWORDS = [
    "decision",
    "penalty",
    "infringement",
    "offence",
    "offense",
    "summons",
    "reprimand",
    "disqualif",
    "protest",
    "right of review",
    "stewards",
]

# --- Gemini (optional) ------------------------------------------------------
# If GEMINI_API_KEY is set (free tier from https://aistudio.google.com/apikey),
# the notifier reads each new PDF with Gemini to (a) judge whether it's a
# penalty/steward decision — catching things the keyword list misses — and
# (b) write a one-line summary for the push. If the key is absent or the API
# errors/rate-limits, it silently falls back to keyword matching.
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")
# Override with GEMINI_MODEL; otherwise these are tried in order until one works.
GEMINI_MODEL_CANDIDATES = (
    [os.environ["GEMINI_MODEL"]] if os.environ.get("GEMINI_MODEL") else
    ["gemini-2.5-flash", "gemini-2.0-flash", "gemini-flash-latest"]
)
_gemini_model_ok = None  # cached working model name for this process

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def log(msg: str) -> None:
    stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{stamp}] {msg}", flush=True)


def http_get(url: str, timeout: int = 30) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()


# Matches each document anchor pointing at a decision-document PDF.
LINK_RE = re.compile(
    r'<a[^>]+href="(?P<href>/system/files/decision-document/[^"]+\.pdf)"[^>]*>'
    r'(?P<title>.*?)</a>',
    re.IGNORECASE | re.DOTALL,
)
TAG_RE = re.compile(r"<[^>]+>")
PUBLISHED_RE = re.compile(
    r"Published on\s*([0-9]{2}\.[0-9]{2}\.[0-9]{2}\s+[0-9]{2}:[0-9]{2}\s*\w+)",
    re.IGNORECASE,
)


def clean_text(raw: str) -> str:
    text = TAG_RE.sub("", raw)
    text = html.unescape(text)
    return re.sub(r"\s+", " ", text).strip()


def parse_documents(page_html: str):
    """Yield dicts of {href, url, title, published, filename} for each doc."""
    docs = []
    for m in LINK_RE.finditer(page_html):
        href = m.group("href")
        title = clean_text(m.group("title"))
        # The "Published on ..." stamp is rendered inside the link text; pull it
        # out and strip it from the title.
        published = ""
        pub_match = PUBLISHED_RE.search(title)
        if pub_match:
            published = pub_match.group(1).strip()
            title = title[: pub_match.start()].replace("Published on", "").strip()
        filename = href.rsplit("/", 1)[-1]
        docs.append({
            "href": href,
            "url": FIA_BASE + href,
            "title": title or filename,
            "published": published,
            "filename": filename,
        })
    return docs


def event_folder_for(filename: str) -> str:
    """e.g. 2026_canadian_grand_prix_-_decision_....pdf -> '2026 Canadian Grand Prix'"""
    slug = filename.split("_-_", 1)[0]
    slug = slug.rsplit(".pdf", 1)[0]
    return slug.replace("_", " ").strip().title() or "Misc"


def is_important(doc: dict) -> bool:
    haystack = f"{doc['title']} {doc['filename']}".lower()
    return any(kw in haystack for kw in IMPORTANT_KEYWORDS)


GEMINI_PROMPT = (
    "You are monitoring official FIA Formula 1 documents. Decide whether THIS "
    "document is a PENALTY or a STEWARDS' DECISION affecting a driver or car — "
    "e.g. a time penalty, grid-place drop, fine, reprimand, disqualification, "
    "or a stewards' ruling on an on-track incident or protest. Routine documents "
    "(timetables, entry lists, classifications, scrutineering, event/competition "
    "notes, championship points, car presentations) are NOT important.\n\n"
    "Reply with EXACTLY ONE line and nothing else:\n"
    "  SKIP\n"
    "    -- if it is not a penalty or stewards' decision; or\n"
    "  ALERT: <one concise sentence — driver/car, the offence, and the outcome>\n"
    "    -- if it is.\n"
)


def gemini_assess(pdf_bytes: bytes, title: str):
    """Use Gemini to classify + summarize a PDF.

    Returns (important: bool, summary: str), or (None, "") if Gemini is
    unavailable or errored — signalling the caller to fall back to keywords.
    """
    global _gemini_model_ok
    if not GEMINI_API_KEY:
        return None, ""
    body = json.dumps({
        "contents": [{"parts": [
            {"inline_data": {
                "mime_type": "application/pdf",
                "data": base64.b64encode(pdf_bytes).decode(),
            }},
            {"text": GEMINI_PROMPT + f"\nDocument title: {title}"},
        ]}],
        # 2.5-flash spends "thinking" tokens from this budget before answering,
        # so keep it generous or the one-line answer gets truncated.
        "generationConfig": {"temperature": 0, "maxOutputTokens": 1024},
    }).encode()

    # Prefer a model already known to work this run; else probe candidates.
    models = [_gemini_model_ok] if _gemini_model_ok else GEMINI_MODEL_CANDIDATES
    for model in models:
        url = (f"https://generativelanguage.googleapis.com/v1beta/models/"
               f"{model}:generateContent?key={GEMINI_API_KEY}")
        req = urllib.request.Request(
            url, data=body, headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                payload = json.loads(resp.read())
            text = (payload["candidates"][0]["content"]["parts"][0]["text"]
                    or "").strip()
            _gemini_model_ok = model
            if text.upper().startswith("ALERT"):
                summary = text.split(":", 1)[1].strip() if ":" in text else text
                return True, summary
            return False, ""
        except urllib.error.HTTPError as e:
            if e.code in (400, 403, 404):       # bad/unknown model -> try next
                log(f"Gemini model '{model}' unavailable (HTTP {e.code}); trying next.")
                continue
            log(f"Gemini error (HTTP {e.code}); falling back to keywords.")
            return None, ""
        except Exception as e:
            log(f"Gemini call failed ({e}); falling back to keywords.")
            return None, ""
    log("No working Gemini model found; falling back to keywords. "
        "Set GEMINI_MODEL to a valid model name.")
    return None, ""


def load_state() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text())
        except Exception:
            log("State file unreadable; starting fresh.")
    return {}


def save_state(state: dict) -> None:
    STATE_FILE.write_text(json.dumps(state, indent=2))


def download_pdf(doc: dict) -> Path:
    folder = DOWNLOAD_DIR / event_folder_for(doc["filename"])
    folder.mkdir(parents=True, exist_ok=True)
    dest = folder / doc["filename"]
    if dest.exists():
        return dest
    data = http_get(doc["url"])
    dest.write_bytes(data)
    return dest


def cleanup_old(days: int = RETENTION_DAYS) -> None:
    """Delete downloaded PDFs older than `days`, then remove empty event folders.

    State entries are kept so deleted-but-old documents are never re-downloaded.
    """
    if not DOWNLOAD_DIR.exists():
        return
    cutoff = time.time() - days * 86400
    removed = 0
    for pdf in DOWNLOAD_DIR.rglob("*.pdf"):
        try:
            if pdf.stat().st_mtime < cutoff:
                pdf.unlink()
                removed += 1
        except OSError as e:
            log(f"Could not delete {pdf}: {e}")
    # Prune now-empty event folders.
    for folder in sorted(DOWNLOAD_DIR.glob("*"), reverse=True):
        if folder.is_dir() and not any(folder.iterdir()):
            try:
                folder.rmdir()
            except OSError:
                pass
    if removed:
        log(f"Cleanup: deleted {removed} PDF(s) older than {days} days.")


def send_push(title: str, message: str, click_url: str = "") -> None:
    if not NOTIFY_ENABLED:
        log(f"Notifications disabled (F1_NOTIFY=0); would have pushed: {title}")
        return
    if not NTFY_TOPIC or NTFY_TOPIC.startswith("CHANGE"):
        log("ntfy topic not configured; skipping push.")
        return
    url = f"{NTFY_SERVER}/{NTFY_TOPIC}"
    headers = {
        "Title": title.encode("ascii", "ignore").decode(),
        "Priority": "high",
        "Tags": "checkered_flag,rotating_light",
        "User-Agent": USER_AGENT,
    }
    if click_url:
        headers["Click"] = click_url
    req = urllib.request.Request(
        url, data=message.encode("utf-8"), headers=headers, method="POST"
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            resp.read()
        log(f"Push sent: {title}")
    except urllib.error.URLError as e:
        log(f"Push failed: {e}")


# ---------------------------------------------------------------------------
# Core check
# ---------------------------------------------------------------------------

def check_once(notify_only: bool = False) -> None:
    """One pass over the FIA page.

    notify_only=True (used by the cloud GitHub Action) skips downloads and
    retention cleanup — it only detects new important docs and pushes.
    """
    state = load_state()
    first_run = len(state) == 0

    try:
        page = http_get(FIA_URL).decode("utf-8", "replace")
    except Exception as e:
        log(f"Failed to fetch FIA page: {e}")
        return

    docs = parse_documents(page)
    if not docs:
        log("No documents parsed — FIA page layout may have changed.")
        return

    new_docs = [d for d in docs if d["href"] not in state]
    log(f"{len(docs)} docs on page, {len(new_docs)} new. (notify_only={notify_only})")

    important_new = []  # list of (doc, summary)
    # Only the cloud notifier uses Gemini, and not while seeding (first run).
    use_gemini = notify_only and not first_run and bool(GEMINI_API_KEY)
    for doc in new_docs:
        important = is_important(doc)
        summary = ""
        if use_gemini:
            try:
                g_important, g_summary = gemini_assess(http_get(doc["url"]),
                                                       doc["title"])
                if g_important is not None:        # None == fall back to keywords
                    important, summary = g_important, g_summary
                time.sleep(1)                      # gentle on free-tier limits
            except Exception as e:
                log(f"Gemini assess failed for {doc['filename']}: {e}")

        entry = {
            "title": doc["title"],
            "published": doc["published"],
            "important": important,
            "seen_at": datetime.now(timezone.utc).isoformat(),
        }
        if summary:
            entry["summary"] = summary
        if not notify_only:
            try:
                entry["saved_to"] = str(download_pdf(doc))
            except Exception as e:
                log(f"Download failed for {doc['filename']}: {e}")
                continue
        state[doc["href"]] = entry
        tag = "IMPORTANT" if important else "ok"
        log(f"  [{tag}] {doc['title']}" + (f" — {summary}" if summary else ""))
        if important:
            important_new.append((doc, summary))

    save_state(state)
    if not notify_only:
        cleanup_old()

    if first_run:
        log(f"First run: seeded {len(new_docs)} existing docs (no push sent).")
        return

    for doc, summary in important_new:
        event = event_folder_for(doc["filename"])
        stamp = f"{event} · {doc['published']}".strip(" ·")
        message = f"{summary}\n{stamp}".strip() if summary else stamp
        send_push(
            title=f"F1 — {doc['title']}",
            message=message,
            click_url=doc["url"],
        )


def watch(interval: int) -> None:
    log(f"Watching FIA F1 documents every {interval}s. Ctrl-C to stop.")
    while True:
        try:
            check_once()
        except Exception as e:
            log(f"Unexpected error: {e}")
        time.sleep(interval)


def main() -> None:
    args = sys.argv[1:]
    if args and args[0] == "--test-notify":
        send_push("F1 docs scraper test", "If you see this, push works ✅",
                  click_url=FIA_URL)
        return
    if args and args[0] == "--notify-only":
        check_once(notify_only=True)
        return
    if args and args[0] == "--watch":
        interval = int(args[1]) if len(args) > 1 else DEFAULT_INTERVAL
        watch(interval)
        return
    check_once()


if __name__ == "__main__":
    main()
