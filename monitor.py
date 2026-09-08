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
from settings import SEARCH_URLS, SKIP_COUNTRIES, MIN_FIXED_BUDGET, COUNTRY_FLAGS

# ── Config ────────────────────────────────────────────────────────────────────

load_dotenv(Path(__file__).parent / ".env")

TELEGRAM_BOT_TOKEN: str = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHANNEL:   str = os.environ.get("TELEGRAM_CHANNEL", "-5087355913")

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


# ── Filtering ─────────────────────────────────────────────────────────────────

def should_skip(job: dict) -> str | None:
    """Return a skip reason string, or None if the job should be sent."""
    country = (job.get("country") or "").strip().lower()
    if country in SKIP_COUNTRIES:
        return f"country={job.get('country')}"

    budget_str = job.get("fixed_budget", "")
    if budget_str:
        amount = float(re.sub(r"[^\d.]", "", budget_str) or "0")
        if 0 < amount < MIN_FIXED_BUDGET:
            return f"fixed_budget={budget_str} < ${MIN_FIXED_BUDGET}"

    return None


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

async def send_telegram(text: str) -> None:
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    async with httpx.AsyncClient() as client:
        r = await client.post(
            url,
            json={
                "chat_id": TELEGRAM_CHANNEL,
                "text": text,
                "parse_mode": "MarkdownV2",
                "disable_web_page_preview": True,
            },
            timeout=10,
        )
        print(f"  TG: {r.status_code}", flush=True)
        if r.status_code != 200:
            print(f"  TG error: {r.text}", flush=True)


# ── Main ──────────────────────────────────────────────────────────────────────

async def main(url_idx: int) -> None:
    seen = load_state()
    print(f"[url_{url_idx}] Known UIDs: {len(seen)}", flush=True)

    browser = await uc.start(headless=False)
    all_jobs: list[dict] = []

    try:
        for search_url in SEARCH_URLS:
            print(f"Opening: ...{search_url[60:100]}", flush=True)
            page = await browser.get(search_url)

            # Wait for Cloudflare challenge to pass
            for _ in range(20):
                await asyncio.sleep(1)
                title = await page.evaluate("document.title")
                if "moment" not in title.lower():
                    break
            await asyncio.sleep(3)

            html = await page.get_content()
            print(f"  HTML: {len(html)} chars", flush=True)
            all_jobs.extend(parse_tiles(html))
            await asyncio.sleep(2)

        # Deduplicate within this run (same job in multiple searches)
        seen_this_run: set[str] = set()
        unique_jobs = []
        for job in all_jobs:
            if job["uid"] not in seen_this_run:
                seen_this_run.add(job["uid"])
                unique_jobs.append(job)

        # Only jobs we haven't seen before
        new_jobs = [j for j in unique_jobs if j["uid"] not in seen]
        print(f"Jobs parsed: {len(unique_jobs)}, new: {len(new_jobs)}", flush=True)

        for job in new_jobs:
            if job.get("url"):
                print(f"  Fetching client info: {job['title'][:50]}", flush=True)
                client_info = await fetch_client_info(browser, job["url"])
                job.update(client_info)

            skip_reason = should_skip(job)
            if skip_reason:
                print(f"  SKIP ({skip_reason}): {job['title'][:50]}", flush=True)
                seen.add(job["uid"])
                continue

            print(f"  Sending: {job['title'][:60]}", flush=True)
            await send_telegram(format_job(job))
            await asyncio.sleep(0.5)
            seen.add(job["uid"])

    finally:
        browser.stop()
        save_state(seen)

    print(f"Done. Total seen UIDs: {len(seen)}", flush=True)


if __name__ == "__main__":
    idx = int(sys.argv[1]) if len(sys.argv) > 1 else 0

    if idx < 0 or idx >= len(SEARCH_URLS):
        print(
            f"Invalid search index: {idx}",
            flush=True,
        )
        sys.exit(2)

    SEARCH_URLS[:] = [
        SEARCH_URLS[idx]
    ]

    uc.loop().run_until_complete(
        main(idx)
    )
