"""
Upwork Job Monitor
==================
Monitors multiple Upwork search URLs for new job postings and sends
Telegram notifications with full job + client details.

Architecture:
- 8 systemd timers run this script with index 0-7 (one per search URL)
- Each run opens the search URL in a headless browser (nodriver/Chrome)
- Parses job tiles, fetches client info from each job page
- Sends new jobs to a Telegram channel
- Global deduplication via state/seen_global.json

Configuration:
- Copy .env.example to .env and fill in your values
- Run setup_timers.sh (as root) to install systemd timers
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sys
from pathlib import Path
from urllib.parse import urljoin

import httpx
from bs4 import BeautifulSoup
import nodriver as uc
from dotenv import load_dotenv
from settings import (
    BROAD_TOPIC_KEYWORDS,
    COMPLEXITY_SIGNALS,
    COUNTRY_FLAGS,
    EXCLUDE_KEYWORDS,
    MIN_SIMPLE_SCORE,
    REQUIRED_CLIENT_COUNTRY,
    SEARCH_READY_TIMEOUT_SECONDS,
    SEARCH_URLS,
    SIMPLE_SIGNALS,
    STRONG_SIMPLE_KEYWORDS,
)

# ── Windows UTF-8 console ─────────────────────────────────────────────────────

def configure_console() -> None:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(
                encoding="utf-8",
                errors="replace",
            )
        except Exception:
            pass


configure_console()


# ── Config ────────────────────────────────────────────────────────────────────

load_dotenv(Path(__file__).parent / ".env")

TELEGRAM_BOT_TOKEN: str = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_CHANNEL:   str = os.environ.get("TELEGRAM_CHANNEL", "").strip()

BASE_URL  = "https://www.upwork.com"
STATE_DIR = Path(__file__).parent / "state"
LOCK_FILE = Path(__file__).parent / "monitor.lock"


# ── State (global deduplication) ──────────────────────────────────────────────

GLOBAL_STATE_FILE = STATE_DIR / "seen_global.json"


def load_state() -> set[str]:
    """
    Load seen job UIDs from global state file.
    Also migrates legacy per-index files (seen_0.json … seen_N.json) on first run.
    """
    STATE_DIR.mkdir(exist_ok=True)

    merged: set[str] = set()

    # Migrate old per-index files
    for old_f in STATE_DIR.glob("seen_[0-9]*.json"):
        try:
            merged |= set(json.loads(old_f.read_text()))
        except Exception:
            pass

    if GLOBAL_STATE_FILE.exists():
        try:
            merged |= set(json.loads(GLOBAL_STATE_FILE.read_text()))
        except Exception:
            pass

    if merged:
        _write_state(merged)
        for old_f in STATE_DIR.glob("seen_[0-9]*.json"):
            try:
                old_f.unlink()
            except Exception:
                pass

    return merged


def _write_state(seen: set[str]) -> None:
    STATE_DIR.mkdir(exist_ok=True)
    tmp = GLOBAL_STATE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(sorted(seen), indent=2))
    tmp.rename(GLOBAL_STATE_FILE)


def save_state(seen: set[str]) -> None:
    _write_state(seen)


# ── HTML parsing ──────────────────────────────────────────────────────────────

def clean_text(value) -> str:
    if value is None:
        return ""

    if hasattr(value, "get_text"):
        value = value.get_text(" ", strip=True)

    return " ".join(str(value).split()).strip()


def select_first(element, selectors):
    for selector in selectors:
        try:
            found = element.select_one(selector)
        except Exception:
            found = None

        if found is not None:
            return found

    return None


def select_text(element, selectors) -> str:
    found = select_first(
        element,
        selectors,
    )

    if found is None:
        return ""

    return clean_text(found)


def extract_job_url(card) -> str:
    anchor = select_first(
        card,
        [
            '[data-test*="job-tile-title-link"][href]',
            '[data-test*="job-tile-title"] a[href]',
            'h2 a[href*="/jobs/"]',
            'a[href*="/jobs/"]',
        ],
    )

    if anchor is None:
        return ""

    href = clean_text(
        anchor.get("href", "")
    )

    if not href:
        return ""

    return urljoin(
        BASE_URL,
        href,
    ).split("?", 1)[0]


def extract_job_uid(card, job_url: str) -> str:
    for attribute in (
        "data-ev-job-uid",
        "data-test-key",
        "data-job-uid",
        "data-job-id",
    ):
        value = clean_text(
            card.get(attribute, "")
        )

        if value:
            return value

    match = re.search(
        r"/jobs/~0?(\d+)",
        job_url,
        re.IGNORECASE,
    )

    if match:
        return match.group(1)

    return ""


def extract_skills(card) -> list[str]:
    selectors = [
        '[data-test="token"]',
        '[data-test*="TokenClamp"] [data-test="token"]',
        '.air3-token-container [data-test="token"]',
    ]

    for selector in selectors:
        try:
            elements = card.select(selector)
        except Exception:
            elements = []

        values = []

        for element in elements:
            value = clean_text(element)

            if value and value not in values:
                values.append(value)

        if values:
            return values

    return []


def extract_fixed_budget(card) -> str:
    fixed = select_first(
        card,
        [
            '[data-test="is-fixed-price"]',
            '[data-test*="fixed-price"]',
        ],
    )

    if fixed is None:
        return ""

    text = clean_text(fixed)

    match = re.search(
        r"\$\s*([\d,]+(?:\.\d+)?)",
        text,
    )

    if not match:
        return ""

    return "$" + match.group(1)


def parse_tiles(html: str) -> list[dict]:
    """
    Parse current Upwork search-result cards.

    Verified against the current public Upwork DOM on 2026-09-08.

    Current primary selector:
        article.job-tile

    Older fallback selectors are kept for compatibility.
    """

    soup = BeautifulSoup(
        html,
        "html.parser",
    )

    cards = soup.select(
        "article.job-tile"
    )

    if not cards:
        cards = soup.select(
            'article[data-test="JobTile"]'
        )

    if not cards:
        cards = soup.select(
            '[data-test="JobTile"]'
        )

    print(
        f"  Tiles found: {len(cards)}",
        flush=True,
    )

    jobs = []

    for card in cards:
        job = {}

        job_url = extract_job_url(card)

        job["uid"] = extract_job_uid(
            card,
            job_url,
        )

        job["url"] = job_url

        job["title"] = select_text(
            card,
            [
                '[data-test*="job-tile-title-link"]',
                '[data-test*="job-tile-title"] a',
                'h2 a[href*="/jobs/"]',
                "h2",
            ],
        )

        job["posted"] = select_text(
            card,
            [
                '[data-test="job-pubilshed-date"]',
                '[data-test="PostedOn"]',
                '[data-test*="published-date"]',
                "time",
            ],
        )

        job["rate"] = select_text(
            card,
            [
                '[data-test="job-type-label"]',
            ],
        )

        job["fixed_budget"] = extract_fixed_budget(
            card
        )

        job["level"] = select_text(
            card,
            [
                '[data-test="experience-level"]',
                '[data-test*="experience"]',
            ],
        )

        job["description"] = select_text(
            card,
            [
                '[data-test*="JobDescription"] p',
                '[data-test*="job-description"] p',
                ".air3-line-clamp p",
                "p",
            ],
        )[:300]

        job["skills"] = extract_skills(
            card
        )

        card_text = clean_text(card)

        job["search_text"] = card_text

        job["payment_verified"] = (
            "Payment method verified"
            in card_text
        )

        job["spent"] = select_text(
            card,
            [
                '[data-test="total-spent"]',
                '[data-test*="total-spent"]',
            ],
        )

        rating_element = select_first(
            card,
            [
                '[data-test="client-rating"]',
                '[data-test*="client-rating"]',
            ],
        )

        job["rating"] = ""

        if rating_element is not None:
            job["rating"] = clean_text(
                rating_element.get(
                    "aria-label",
                    "",
                )
            )

            if not job["rating"]:
                job["rating"] = clean_text(
                    rating_element
                )

        job["country"] = select_text(
            card,
            [
                '[data-test="client-location"] strong',
                '[data-test="client-location"]',
                '[data-test*="client-location"]',
            ],
        )

        proposals_match = re.search(
            r"Proposals:\s*"
            r"(Less than \d+|\d+\s+to\s+\d+|\d+\+?)",
            card_text,
            re.IGNORECASE,
        )

        job["proposals"] = (
            proposals_match.group(1).strip()
            if proposals_match
            else ""
        )

        if not job["uid"]:
            print(
                f"  Skip card without UID: "
                f"{job['title'][:80]}",
                flush=True,
            )
            continue

        if not job["title"]:
            print(
                f"  Skip UID {job['uid']}: "
                "title is empty",
                flush=True,
            )
            continue

        if not job["url"]:
            print(
                f"  Skip UID {job['uid']}: "
                "URL is empty",
                flush=True,
            )
            continue

        jobs.append(job)

    return jobs


# ── Client info (job page) ────────────────────────────────────────────────────

_CLIENT_JS = """
(function() {
    if (!document.body) return null;
    var qa = function(s) { return document.querySelector('[data-qa="' + s + '"]'); };
    var it = function(el) { return el ? el.innerText.trim() : ''; };
    var body = document.body.innerText;

    var locRaw = it(qa('client-location'));
    var country = locRaw ? locRaw.split('\\n')[0].trim() : '';

    var proposals = '', interviewing = '', invites_sent = '', unanswered = '', last_viewed = '';
    var actIdx = body.indexOf('Activity on this job');
    if (actIdx !== -1) {
        var actTxt = body.substring(actIdx, actIdx + 600);
        var rx = function(label) {
            var m = actTxt.match(new RegExp(label + '[:\\\\s]*\\\\n([^\\\\n]+)'));
            return m ? m[1].trim() : '';
        };
        proposals    = rx('Proposals');
        interviewing = rx('Interviewing');
        invites_sent = rx('Invites sent');
        unanswered   = rx('Unanswered invites');
        last_viewed  = rx('Last viewed by client');
    }

    return {
        spent:            it(qa('client-spend')),
        country:          country,
        hires:            it(qa('client-hires')),
        rating:           it(qa('client-job-posting-stats')),
        payment_verified: body.indexOf('Payment method verified') !== -1,
        proposals:        proposals,
        interviewing:     interviewing,
        invites_sent:     invites_sent,
        unanswered:       unanswered,
        last_viewed:      last_viewed,
    };
})()
"""


async def _fetch_client_info_inner(job_url: str, browser) -> dict:
    """Open job page in a new tab and extract client + activity data."""
    try:
        tab = await browser.get(job_url, new_tab=True)

        # Wait for client data to appear
        for _ in range(20):
            await asyncio.sleep(1)
            has_client = await tab.evaluate(
                "!!document.querySelector('[data-qa=\"client-spend\"]') || "
                "!!document.querySelector('[data-qa=\"client-location\"]')"
            )
            if has_client:
                break
            body_len = await tab.evaluate(
                "document.body ? document.body.innerText.length : 0"
            )
            if isinstance(body_len, int) and body_len > 8000:
                break

        data = await tab.evaluate(_CLIENT_JS)
        await tab.close()

        if isinstance(data, list):
            data = {
                pair[0]: pair[1].get("value", "") if isinstance(pair[1], dict) else pair[1]
                for pair in data
            }

        info = {}
        if isinstance(data, dict):
            info = {k: v for k, v in data.items() if v not in (None, "", False)}

        print(f"  client_info: {info}", flush=True)
        return info

    except Exception as e:
        print(f"  client_info error: {type(e).__name__}: {e}", flush=True)
        return {}


async def fetch_client_info(browser, job_url: str) -> dict:
    """Fetch client info with a hard timeout."""
    try:
        return await asyncio.wait_for(
            _fetch_client_info_inner(job_url, browser), timeout=45
        )
    except asyncio.TimeoutError:
        print(f"  client_info timeout: {job_url}", flush=True)
        return {}
    except Exception as e:
        print(f"  client_info error: {e}", flush=True)
        return {}


# ── Simple-job filtering ──────────────────────────────────────────────────────

def _phrase_matches(
    text: str,
    phrases,
) -> list[str]:
    normalized = text.casefold()

    matches = []

    for phrase in phrases:
        if phrase.casefold() in normalized:
            matches.append(phrase)

    return matches


def evaluate_simple_job(
    job: dict,
) -> dict:
    """
    Decide whether a job looks like the kind of small/simple task
    we want to alert on.

    Rules:

    1. Explicit exclusion keyword -> reject.
    2. Complexity / long-term signal -> reject.
    3. Strong-simple topic -> accept.
    4. Broad topic such as Python/testing -> requires a simplicity
       signal such as quick/simple/small/fix/one-time.
    """

    searchable = job.get("search_text", "")

    if not searchable:
        searchable = " ".join(
            [
                job.get("title", ""),
                job.get("description", ""),
                " ".join(
                    job.get("skills", [])
                ),
                job.get("rate", ""),
                job.get("level", ""),
            ]
        )

    strong = _phrase_matches(
        searchable,
        STRONG_SIMPLE_KEYWORDS,
    )

    broad = _phrase_matches(
        searchable,
        BROAD_TOPIC_KEYWORDS,
    )

    simple = _phrase_matches(
        searchable,
        SIMPLE_SIGNALS,
    )

    complexity = _phrase_matches(
        searchable,
        COMPLEXITY_SIGNALS,
    )

    excluded = _phrase_matches(
        searchable,
        EXCLUDE_KEYWORDS,
    )

    score = 0

    if strong:
        score += 5

    if broad:
        score += 2

    if simple:
        score += 3

    positive_matches = []

    for group in (
        strong,
        broad,
        simple,
    ):
        for value in group:
            if value not in positive_matches:
                positive_matches.append(value)

    if excluded:
        return {
            "accepted": False,
            "score": score,
            "reason": (
                "excluded keyword: "
                + ", ".join(excluded)
            ),
            "matches": positive_matches,
            "complexity": complexity,
        }

    if complexity:
        return {
            "accepted": False,
            "score": score,
            "reason": (
                "complex/long-term signal: "
                + ", ".join(complexity)
            ),
            "matches": positive_matches,
            "complexity": complexity,
        }

    if score < MIN_SIMPLE_SCORE:
        return {
            "accepted": False,
            "score": score,
            "reason": (
                f"score {score} < "
                f"{MIN_SIMPLE_SCORE}"
            ),
            "matches": positive_matches,
            "complexity": complexity,
        }

    return {
        "accepted": True,
        "score": score,
        "reason": "simple-job filter matched",
        "matches": positive_matches,
        "complexity": complexity,
    }


# ── Telegram formatting ───────────────────────────────────────────────────────

def escape_md(s: str) -> str:
    """Escape Telegram MarkdownV2 special characters."""
    for ch in r"\_*[]()~`>#+-=|{}.!":
        s = s.replace(ch, "\\" + ch)
    return s


def format_job(job: dict) -> str:
    """Format a job dict into a Telegram MarkdownV2 message."""
    t       = escape_md(job["title"])
    rate    = escape_md(job.get("rate", ""))
    level   = escape_md(job.get("level", ""))
    posted  = escape_md(job.get("posted", ""))
    desc    = escape_md(job.get("description", ""))
    url     = job["url"]
    skills  = job.get("skills", [])
    country = job.get("country", "")
    flag    = COUNTRY_FLAGS.get(country, "🌍")

    lines = [
        f"*{t}*",
        "————————————————————————",
        "*Contract details*",
    ]
    if posted:
        lines.append(f"⌛️ {posted}")
    if rate:
        budget = escape_md(job.get("fixed_budget", ""))
        lines.append(f"💻 {rate}: {budget} 💲" if budget else f"💻 {rate} 💲")
    if level:
        lines.append(f"♟ {level}")
    if skills:
        lines.append(f"🛠 {escape_md(', '.join(skills[:6]))}")
    lines.append(f"🔗 [Open job]({url})")
    lines.append("————————————————————————")

    # Client block
    client_lines = []
    if job.get("payment_verified"):
        client_lines.append("✅ Payment verified")
    if job.get("rating"):
        client_lines.append(f"⭐️ {escape_md(job['rating'])}")
    if job.get("spent"):
        client_lines.append(f"💰 {escape_md(job['spent'])} spent")
    if job.get("hires"):
        client_lines.append(f"🤝 {escape_md(job['hires'])}")
    if country:
        client_lines.append(f"{flag} {escape_md(country)}")
    if client_lines:
        lines.append("*Client info*")
        lines.extend(client_lines)
        lines.append("————————————————————————")

    # Activity block
    activity_lines = []
    for label, icon, key in [
        ("Proposals",    "📨", "proposals"),
        ("Interviewing", "💬", "interviewing"),
        ("Invites sent", "📩", "invites_sent"),
        ("Unanswered",   "🔕", "unanswered"),
        ("Last viewed",  "👁",  "last_viewed"),
    ]:
        if job.get(key):
            activity_lines.append(f"{icon} {label}: {escape_md(job[key])}")
    if activity_lines:
        lines.append("*Activity on this job*")
        lines.extend(activity_lines)
        lines.append("————————————————————————")

    if desc:
        suffix = escape_md("...") if len(job["description"]) >= 300 else ""
        lines.append(desc + suffix)

    return "\n".join(lines)


# ── Telegram sender ───────────────────────────────────────────────────────────

async def send_telegram(
    text: str,
) -> None:

    if not TELEGRAM_BOT_TOKEN:
        raise RuntimeError(
            "TELEGRAM_BOT_TOKEN is not configured in .env"
        )

    if not TELEGRAM_CHANNEL:
        raise RuntimeError(
            "TELEGRAM_CHANNEL is not configured in .env"
        )

    url = (
        "https://api.telegram.org/bot"
        f"{TELEGRAM_BOT_TOKEN}"
        "/sendMessage"
    )

    async with httpx.AsyncClient() as client:
        response = await client.post(
            url,
            json={
                "chat_id": TELEGRAM_CHANNEL,
                "text": text,
                "parse_mode": "MarkdownV2",
                "disable_web_page_preview": True,
            },
            timeout=20,
        )

    print(
        f"  TG: {response.status_code}",
        flush=True,
    )

    if response.status_code != 200:
        raise RuntimeError(
            "Telegram API error "
            f"{response.status_code}: "
            f"{response.text}"
        )


# ── Main ──────────────────────────────────────────────────────────────────────

async def wait_for_search_ready(
    page,
) -> int:
    """
    Wait until the real Upwork search results appear.

    We do not click or solve a challenge. We only wait for the same
    automatic Cloudflare transition already verified on this machine.
    """

    for second in range(
        1,
        SEARCH_READY_TIMEOUT_SECONDS + 1,
    ):
        await asyncio.sleep(1)

        try:
            title = await page.evaluate(
                "document.title || ''"
            )
        except Exception:
            title = ""

        try:
            tile_count = await page.evaluate(
                "document.querySelectorAll("
                "'article.job-tile'"
                ").length"
            )
        except Exception:
            tile_count = 0

        try:
            tile_count = int(tile_count)
        except Exception:
            tile_count = 0

        print(
            f"  ready={second:02d}s "
            f"title={str(title)!r} "
            f"tiles={tile_count}",
            flush=True,
        )

        if tile_count > 0:
            return tile_count

    raise RuntimeError(
        "Upwork search results did not become ready "
        f"within {SEARCH_READY_TIMEOUT_SECONDS} seconds."
    )


def _save_seen_after_change(
    seen: set[str],
    dry_run: bool,
) -> None:

    if not dry_run:
        save_state(
            seen
        )


async def run_monitor(
    *,
    dry_run: bool,
    bootstrap: bool,
) -> None:

    seen = load_state()

    print(
        f"Known UIDs: {len(seen)}",
        flush=True,
    )

    browser = await uc.start(
        headless=False
    )

    try:
        search_url = SEARCH_URLS[0]

        print(
            f"Opening US-only search: {search_url}",
            flush=True,
        )

        page = await browser.get(
            search_url
        )

        browser_tile_count = await wait_for_search_ready(
            page
        )

        print(
            f"Browser tiles ready: {browser_tile_count}",
            flush=True,
        )

        await asyncio.sleep(2)

        html = await page.get_content()

        print(
            f"HTML: {len(html)} chars",
            flush=True,
        )

        jobs = parse_tiles(
            html
        )

        print(
            f"Jobs parsed: {len(jobs)}",
            flush=True,
        )

        if not jobs:
            raise RuntimeError(
                "No jobs parsed from current Upwork search."
            )

        # --------------------------------------------------------------
        # Bootstrap:
        # mark everything currently visible as already seen,
        # without Telegram and without opening individual job pages.
        # --------------------------------------------------------------

        if bootstrap:
            added = 0

            for job in jobs:
                uid = job["uid"]

                if uid not in seen:
                    seen.add(uid)
                    added += 1

            save_state(
                seen
            )

            print(
                f"BOOTSTRAP_ADDED={added}",
                flush=True,
            )

            print(
                f"BOOTSTRAP_TOTAL_SEEN={len(seen)}",
                flush=True,
            )

            print(
                "BOOTSTRAP_STATUS=OK",
                flush=True,
            )

            return

        # --------------------------------------------------------------
        # Only jobs not handled before.
        # --------------------------------------------------------------

        new_jobs = [
            job
            for job in jobs
            if job["uid"] not in seen
        ]

        print(
            f"New jobs: {len(new_jobs)}",
            flush=True,
        )

        accepted_count = 0
        us_verified_count = 0
        sent_count = 0

        for job in new_jobs:
            uid = job["uid"]

            evaluation = evaluate_simple_job(
                job
            )

            if not evaluation["accepted"]:
                print(
                    f"FILTER REJECT [{uid}] "
                    f"{job['title'][:80]} | "
                    f"{evaluation['reason']} | "
                    f"score={evaluation['score']}",
                    flush=True,
                )

                if not dry_run:
                    seen.add(uid)

                    _save_seen_after_change(
                        seen,
                        dry_run,
                    )

                continue

            accepted_count += 1

            job["matched_keywords"] = (
                evaluation["matches"]
            )

            print(
                f"FILTER ACCEPT [{uid}] "
                f"{job['title'][:80]} | "
                f"score={evaluation['score']} | "
                f"matches="
                f"{', '.join(evaluation['matches'])}",
                flush=True,
            )

            # ----------------------------------------------------------
            # Strict secondary US verification.
            # ----------------------------------------------------------

            print(
                f"  Fetching client info: "
                f"{job['title'][:70]}",
                flush=True,
            )

            client_info = await fetch_client_info(
                browser,
                job["url"],
            )

            job.update(
                client_info
            )

            country = clean_text(
                job.get(
                    "country",
                    "",
                )
            )

            if not country:
                print(
                    f"COUNTRY UNVERIFIED [{uid}] "
                    "No client country was extracted. "
                    "Fail closed; not sending.",
                    flush=True,
                )

                # Do not mark as seen.
                # If it is still visible next run,
                # client-info retrieval can retry.
                continue

            if (
                country.casefold()
                != REQUIRED_CLIENT_COUNTRY.casefold()
            ):
                print(
                    f"COUNTRY REJECT [{uid}] "
                    f"client={country!r}",
                    flush=True,
                )

                if not dry_run:
                    seen.add(uid)

                    _save_seen_after_change(
                        seen,
                        dry_run,
                    )

                continue

            us_verified_count += 1

            print(
                f"US VERIFIED [{uid}] "
                f"{job['title'][:80]}",
                flush=True,
            )

            if dry_run:
                print(
                    f"DRY_RUN_WOULD_SEND [{uid}] "
                    f"{job['title']}",
                    flush=True,
                )

                continue

            await send_telegram(
                format_job(
                    job
                )
            )

            sent_count += 1

            seen.add(uid)

            _save_seen_after_change(
                seen,
                dry_run,
            )

            await asyncio.sleep(
                0.5
            )

        print(
            f"FILTER_ACCEPTED={accepted_count}",
            flush=True,
        )

        print(
            f"US_VERIFIED={us_verified_count}",
            flush=True,
        )

        print(
            f"TELEGRAM_SENT={sent_count}",
            flush=True,
        )

        print(
            f"TOTAL_SEEN={len(seen)}",
            flush=True,
        )

        if dry_run:
            print(
                "DRY_RUN_STATUS=OK",
                flush=True,
            )
        else:
            print(
                "RUN_STATUS=OK",
                flush=True,
            )

    finally:
        try:
            browser.stop()
        except Exception:
            pass


def parse_cli_args():
    parser = argparse.ArgumentParser(
        description=(
            "Windows US-only Upwork simple-job monitor"
        )
    )

    parser.add_argument(
        "--dry-run",
        action="store_true",
        help=(
            "Run Upwork + filters + US verification "
            "without Telegram or state changes."
        ),
    )

    parser.add_argument(
        "--bootstrap",
        action="store_true",
        help=(
            "Mark currently visible jobs as seen "
            "without Telegram."
        ),
    )

    return parser.parse_args()


if __name__ == "__main__":
    args = parse_cli_args()

    if args.dry_run and args.bootstrap:
        print(
            "ERROR: use either --dry-run or --bootstrap, "
            "not both.",
            flush=True,
        )

        sys.exit(2)

    uc.loop().run_until_complete(
        run_monitor(
            dry_run=args.dry_run,
            bootstrap=args.bootstrap,
        )
    )
