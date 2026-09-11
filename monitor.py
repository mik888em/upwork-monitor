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
import msvcrt
import os
import re
import sys
import traceback
from datetime import datetime, timedelta, timezone
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
    DOM_READY_STABLE_SECONDS,
    EXCLUDE_KEYWORDS,
    HEALTH_ALERTS_ENABLED,
    ROLE_COMPLEXITY_SIGNALS,
    WEB_PROJECT_TITLE_SIGNALS,
    MINIMIZE_BROWSER_AFTER_READY,
    MAX_SIMPLE_FIXED_BUDGET,
    MIN_SIMPLE_SCORE,
    REQUIRED_CLIENT_COUNTRY,
    SEARCH_READY_TIMEOUT_SECONDS,
    SEARCH_URLS,
    SIMPLE_SIGNALS,
    STRONG_SIMPLE_KEYWORDS,
    UPWORK_COOLDOWN_STEPS_MINUTES,
    UPWORK_LONG_DEGRADED_ALERT_HOURS,
    UPWORK_READY_FAILURE_THRESHOLD,
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


# ── Windows single-instance lock ──────────────────────────────────────────────

def acquire_single_instance_lock():
    """
    Acquire a non-blocking one-byte Windows file lock.

    Returns:
        open file handle -> lock acquired
        None             -> another monitor is already running
    """

    LOCK_FILE.parent.mkdir(
        parents=True,
        exist_ok=True,
    )

    handle = open(
        LOCK_FILE,
        "a+b",
    )

    handle.seek(
        0,
        2,
    )

    if handle.tell() == 0:
        handle.write(
            b"\0"
        )
        handle.flush()

    handle.seek(0)

    try:
        msvcrt.locking(
            handle.fileno(),
            msvcrt.LK_NBLCK,
            1,
        )

    except OSError:
        handle.close()
        return None

    return handle


def release_single_instance_lock(
    handle,
) -> None:

    if handle is None:
        return

    try:
        handle.seek(0)

        msvcrt.locking(
            handle.fileno(),
            msvcrt.LK_UNLCK,
            1,
        )

    except OSError:
        pass

    finally:
        try:
            handle.close()
        except Exception:
            pass



# ── State (global deduplication) ──────────────────────────────────────────────

GLOBAL_STATE_FILE = STATE_DIR / "seen_global.json"

HEALTH_STATE_FILE = STATE_DIR / "health.json"


class UpworkReadyTimeout(RuntimeError):
    def __init__(
        self,
        kind: str,
        title: str,
        tile_count: int,
        link_count: int,
    ) -> None:
        self.kind = kind
        self.title = title
        self.tile_count = tile_count
        self.link_count = link_count

        super().__init__(
            "Upwork search did not become fully ready "
            f"within {SEARCH_READY_TIMEOUT_SECONDS} seconds "
            f"(kind={kind}, title={title!r}, "
            f"tiles={tile_count}, links={link_count})."
        )


def _utc_now() -> datetime:
    return datetime.now(
        timezone.utc
    )


def _utc_iso(
    value: datetime,
) -> str:
    return value.astimezone(
        timezone.utc
    ).isoformat()


def _parse_utc(
    value: str,
):
    if not value:
        return None

    try:
        parsed = datetime.fromisoformat(
            value
        )
    except (TypeError, ValueError):
        return None

    if parsed.tzinfo is None:
        parsed = parsed.replace(
            tzinfo=timezone.utc
        )

    return parsed.astimezone(
        timezone.utc
    )


def _default_health_state() -> dict:
    return {
        "consecutive_ready_failures": 0,
        "cooldown_until": "",
        "degraded_notified": False,
        "degraded_since": "",
        "long_degraded_notified": False,
        "backoff_level": 0,
        "last_cooldown_minutes": 0,
        "last_failure_kind": "",
        "last_failure_at": "",
        "last_success_at": "",
    }


def _coerce_nonnegative_int(
    value,
    default: int = 0,
) -> int:
    try:
        return max(
            0,
            int(value),
        )
    except (TypeError, ValueError):
        return default


def load_health_state() -> dict:
    STATE_DIR.mkdir(
        parents=True,
        exist_ok=True,
    )

    defaults = _default_health_state()
    state = dict(
        defaults
    )

    raw = {}

    if HEALTH_STATE_FILE.is_file():
        try:
            loaded = json.loads(
                HEALTH_STATE_FILE.read_text(
                    encoding="utf-8"
                )
            )

            if isinstance(
                loaded,
                dict,
            ):
                raw = loaded

        except Exception:
            raw = {}

    for key, default_value in defaults.items():
        state[key] = raw.get(
            key,
            default_value,
        )

    state["consecutive_ready_failures"] = (
        _coerce_nonnegative_int(
            state.get(
                "consecutive_ready_failures",
                0,
            )
        )
    )

    state["backoff_level"] = (
        _coerce_nonnegative_int(
            state.get(
                "backoff_level",
                0,
            )
        )
    )

    state["last_cooldown_minutes"] = (
        _coerce_nonnegative_int(
            state.get(
                "last_cooldown_minutes",
                0,
            )
        )
    )

    state["degraded_notified"] = bool(
        state.get(
            "degraded_notified",
            False,
        )
    )

    state["long_degraded_notified"] = bool(
        state.get(
            "long_degraded_notified",
            False,
        )
    )

    # Migration from Health Guard V3:
    # if the old state is already in/after its first 30-minute cooldown,
    # the NEXT failed recovery probe should escalate to 60 minutes.
    if "backoff_level" not in raw:
        if (
            state["consecutive_ready_failures"]
            >= UPWORK_READY_FAILURE_THRESHOLD
            or str(
                state.get(
                    "cooldown_until",
                    "",
                )
                or ""
            )
        ):
            state["backoff_level"] = 1

            if (
                state["last_cooldown_minutes"]
                <= 0
            ):
                state["last_cooldown_minutes"] = (
                    int(
                        UPWORK_COOLDOWN_STEPS_MINUTES[0]
                    )
                )

    max_level = max(
        0,
        len(
            UPWORK_COOLDOWN_STEPS_MINUTES
        )
        - 1,
    )

    state["backoff_level"] = min(
        state["backoff_level"],
        max_level,
    )

    # V3 did not have degraded_since. Use its latest known failure as a
    # conservative migration point, rather than inventing an earlier outage.
    if (
        not str(
            state.get(
                "degraded_since",
                "",
            )
            or ""
        )
        and state["consecutive_ready_failures"] > 0
    ):
        state["degraded_since"] = str(
            state.get(
                "last_failure_at",
                "",
            )
            or ""
        )

    return state


def save_health_state(
    state: dict,
) -> None:
    STATE_DIR.mkdir(
        parents=True,
        exist_ok=True,
    )

    tmp = HEALTH_STATE_FILE.with_suffix(
        ".tmp"
    )

    tmp.write_text(
        json.dumps(
            state,
            indent=2,
            sort_keys=True,
        ),
        encoding="utf-8",
    )

    tmp.replace(
        HEALTH_STATE_FILE
    )


def cooldown_remaining_seconds(
    state: dict,
) -> int:
    until = _parse_utc(
        str(
            state.get(
                "cooldown_until",
                "",
            )
            or ""
        )
    )

    if until is None:
        return 0

    seconds = int(
        (
            until
            - _utc_now()
        ).total_seconds()
    )

    return max(
        0,
        seconds,
    )


def cooldown_minutes_for_level(
    level: int,
) -> int:
    steps = tuple(
        int(value)
        for value
        in UPWORK_COOLDOWN_STEPS_MINUTES
    )

    if not steps:
        return 30

    index = min(
        max(
            0,
            _coerce_nonnegative_int(
                level
            ),
        ),
        len(steps) - 1,
    )

    return steps[index]


def _elapsed_seconds_since(
    value: str,
):
    parsed = _parse_utc(
        str(
            value
            or ""
        )
    )

    if parsed is None:
        return None

    return max(
        0.0,
        (
            _utc_now()
            - parsed
        ).total_seconds(),
    )


def hours_since_last_success(
    state: dict,
):
    seconds = _elapsed_seconds_since(
        str(
            state.get(
                "last_success_at",
                "",
            )
            or ""
        )
    )

    if seconds is None:
        return None

    return (
        seconds
        / 3600.0
    )


def degraded_hours(
    state: dict,
):
    seconds = _elapsed_seconds_since(
        str(
            state.get(
                "degraded_since",
                "",
            )
            or ""
        )
    )

    if seconds is None:
        return None

    return (
        seconds
        / 3600.0
    )


def log_health_status(
    state: dict,
) -> None:
    last_success = str(
        state.get(
            "last_success_at",
            "",
        )
        or ""
    )

    if last_success:
        print(
            f"LAST_SUCCESS_AT={last_success}",
            flush=True,
        )
    else:
        print(
            "LAST_SUCCESS_AT=UNKNOWN",
            flush=True,
        )

    success_hours = hours_since_last_success(
        state
    )

    if success_hours is None:
        print(
            "HOURS_SINCE_LAST_SUCCESS=UNKNOWN",
            flush=True,
        )
    else:
        print(
            "HOURS_SINCE_LAST_SUCCESS="
            f"{success_hours:.2f}",
            flush=True,
        )

    degraded_since = str(
        state.get(
            "degraded_since",
            "",
        )
        or ""
    )

    if degraded_since:
        print(
            f"DEGRADED_SINCE={degraded_since}",
            flush=True,
        )

        outage_hours = degraded_hours(
            state
        )

        if outage_hours is not None:
            print(
                "DEGRADED_HOURS="
                f"{outage_hours:.2f}",
                flush=True,
            )

    print(
        "BACKOFF_LEVEL="
        f"{_coerce_nonnegative_int(state.get('backoff_level', 0))}",
        flush=True,
    )

    print(
        "LAST_COOLDOWN_MINUTES="
        f"{_coerce_nonnegative_int(state.get('last_cooldown_minutes', 0))}",
        flush=True,
    )

    print(
        "NEXT_COOLDOWN_MINUTES="
        f"{cooldown_minutes_for_level(state.get('backoff_level', 0))}",
        flush=True,
    )



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
    STATE_DIR.mkdir(
        parents=True,
        exist_ok=True,
    )

    tmp = GLOBAL_STATE_FILE.with_suffix(
        ".tmp"
    )

    tmp.write_text(
        json.dumps(
            sorted(seen),
            indent=2,
        ),
        encoding="utf-8",
    )

    # IMPORTANT FOR WINDOWS:
    #
    # Path.rename() / os.rename() cannot replace an existing destination
    # file on Windows and raises WinError 183.
    #
    # Path.replace() uses replacement semantics and atomically replaces
    # the existing seen_global.json.
    tmp.replace(
        GLOBAL_STATE_FILE
    )


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
    """
    Match phrases using token/phrase boundaries.

    The previous implementation used:

        phrase in text

    which caused false positives such as:

        "script"  inside "description"
        "api"     inside unrelated longer words

    Whitespace inside multi-word phrases is flexible.
    """

    normalized = (
        text or ""
    ).casefold()

    matches = []

    for phrase in phrases:
        candidate = (
            phrase or ""
        ).casefold().strip()

        if not candidate:
            continue

        pattern = re.escape(
            candidate
        )

        # Allow one or more whitespace characters between words.
        pattern = pattern.replace(
            r"\ ",
            r"\s+",
        )

        # English keyword boundary at the beginning.
        if candidate[0].isalnum():
            pattern = (
                r"(?<![0-9a-z])"
                + pattern
            )

        # English keyword boundary at the end.
        if candidate[-1].isalnum():
            pattern = (
                pattern
                + r"(?![0-9a-z])"
            )

        if re.search(
            pattern,
            normalized,
        ):
            matches.append(
                phrase
            )

    return matches


def _fixed_budget_amount(
    job: dict,
):
    value = (
        job.get(
            "fixed_budget",
            "",
        )
        or ""
    )

    match = re.search(
        r"(\d[\d,]*(?:\.\d+)?)",
        value,
    )

    if not match:
        return None

    try:
        return float(
            match.group(1).replace(
                ",",
                "",
            )
        )

    except ValueError:
        return None


def evaluate_simple_job(
    job: dict,
) -> dict:
    """
    Decide whether a job belongs to our small/simple-job alert set.

    Decision order:

    1. Explicit exclusion keyword -> reject.
    2. Hard complexity / long engagement -> reject.
    3. Large fixed-price project -> reject.
    4. Professional role without explicit quick/simple wording -> reject.
    5. Score must reach MIN_SIMPLE_SCORE.
    """

    searchable = (
        job.get(
            "search_text",
            "",
        )
        or ""
    )

    if not searchable:
        searchable = " ".join(
            [
                job.get(
                    "title",
                    "",
                ),
                job.get(
                    "description",
                    "",
                ),
                " ".join(
                    job.get(
                        "skills",
                        [],
                    )
                ),
                job.get(
                    "rate",
                    "",
                ),
                job.get(
                    "level",
                    "",
                ),
            ]
        )

    title = (
        job.get(
            "title",
            "",
        )
        or ""
    )

    title_simple = _phrase_matches(
        title,
        SIMPLE_SIGNALS,
    )

    web_project_title = _phrase_matches(
        title,
        WEB_PROJECT_TITLE_SIGNALS,
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

    role_complexity = _phrase_matches(
        title,
        ROLE_COMPLEXITY_SIGNALS,
    )

    fixed_budget_amount = (
        _fixed_budget_amount(
            job
        )
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
                positive_matches.append(
                    value
                )

    if excluded:
        return {
            "accepted": False,
            "score": score,
            "reason": (
                "excluded keyword: "
                + ", ".join(
                    excluded
                )
            ),
            "matches": positive_matches,
            "complexity": complexity,
            "role_complexity": role_complexity,
            "fixed_budget": fixed_budget_amount,
        }

    if complexity:
        return {
            "accepted": False,
            "score": score,
            "reason": (
                "complex/long-term signal: "
                + ", ".join(
                    complexity
                )
            ),
            "matches": positive_matches,
            "complexity": complexity,
            "role_complexity": role_complexity,
            "fixed_budget": fixed_budget_amount,
        }

    if (
        fixed_budget_amount is not None
        and fixed_budget_amount
        > MAX_SIMPLE_FIXED_BUDGET
    ):
        return {
            "accepted": False,
            "score": score,
            "reason": (
                "fixed budget "
                f"${fixed_budget_amount:,.2f} "
                "exceeds simple-job limit "
                f"${MAX_SIMPLE_FIXED_BUDGET:,.2f}"
            ),
            "matches": positive_matches,
            "complexity": complexity,
            "role_complexity": role_complexity,
            "fixed_budget": fixed_budget_amount,
        }

    if (
        web_project_title
        and not title_simple
    ):
        return {
            "accepted": False,
            "score": score,
            "reason": (
                "web project title without "
                "explicit quick/simple title signal: "
                + ", ".join(
                    web_project_title
                )
            ),
            "matches": positive_matches,
            "complexity": complexity,
            "role_complexity": role_complexity,
            "fixed_budget": fixed_budget_amount,
        }

    if (
        role_complexity
        and not title_simple
    ):
        return {
            "accepted": False,
            "score": score,
            "reason": (
                "professional role without "
                "explicit quick/simple TITLE signal: "
                + ", ".join(
                    role_complexity
                )
            ),
            "matches": positive_matches,
            "complexity": complexity,
            "role_complexity": role_complexity,
            "fixed_budget": fixed_budget_amount,
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
            "role_complexity": role_complexity,
            "fixed_budget": fixed_budget_amount,
        }

    return {
        "accepted": True,
        "score": score,
        "reason": (
            "simple-job filter matched"
        ),
        "matches": positive_matches,
        "complexity": complexity,
        "role_complexity": role_complexity,
        "fixed_budget": fixed_budget_amount,
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
        spent_text = str(
            job["spent"]
        ).strip()

        if "spent" not in spent_text.casefold():
            spent_text += " spent"

        client_lines.append(
            f"💰 {escape_md(spent_text)}"
        )
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



async def send_health_telegram(
    text: str,
) -> bool:
    # Plain-text health notification. Errors are logged but never raised.

    if not HEALTH_ALERTS_ENABLED:
        print(
            "HEALTH_ALERT_STATUS=DISABLED",
            flush=True,
        )
        return False

    if (
        not TELEGRAM_BOT_TOKEN
        or not TELEGRAM_CHANNEL
    ):
        print(
            "HEALTH_ALERT_STATUS=CONFIG_MISSING",
            flush=True,
        )
        return False

    url = (
        "https://api.telegram.org/bot"
        f"{TELEGRAM_BOT_TOKEN}"
        "/sendMessage"
    )

    try:
        async with httpx.AsyncClient() as client:
            response = await client.post(
                url,
                json={
                    "chat_id": TELEGRAM_CHANNEL,
                    "text": text,
                    "disable_web_page_preview": True,
                },
                timeout=20,
            )

    except Exception as exc:
        print(
            "HEALTH_ALERT_STATUS=ERROR "
            f"{type(exc).__name__}: {exc}",
            flush=True,
        )
        return False

    print(
        f"HEALTH_TG={response.status_code}",
        flush=True,
    )

    if response.status_code != 200:
        print(
            "HEALTH_ALERT_STATUS=HTTP_ERROR",
            flush=True,
        )
        return False

    return True


async def maybe_send_long_degraded_alert(
    state: dict,
) -> bool:
    if not HEALTH_ALERTS_ENABLED:
        return False

    if bool(
        state.get(
            "long_degraded_notified",
            False,
        )
    ):
        return False

    outage_hours = degraded_hours(
        state
    )

    if outage_hours is None:
        return False

    if (
        outage_hours
        < float(
            UPWORK_LONG_DEGRADED_ALERT_HOURS
        )
    ):
        return False

    last_success = str(
        state.get(
            "last_success_at",
            "",
        )
        or "unknown"
    )

    last_failure_kind = str(
        state.get(
            "last_failure_kind",
            "",
        )
        or "unknown"
    )

    current_cooldown = (
        _coerce_nonnegative_int(
            state.get(
                "last_cooldown_minutes",
                0,
            )
        )
    )

    message = (
        "⚠ Upwork Monitor still degraded\n"
        "Upwork search has not recovered for "
        f"{outage_hours:.1f} hours.\n"
        f"Last successful readiness: {last_success}.\n"
        f"Last failure: {last_failure_kind}.\n"
        f"Current backoff: {current_cooldown} minutes."
    )

    sent = await send_health_telegram(
        message
    )

    if sent:
        state["long_degraded_notified"] = True

        print(
            "HEALTH_ALERT_SENT=LONG_DEGRADED",
            flush=True,
        )

        save_health_state(
            state
        )

    return sent


async def register_ready_failure(
    error: UpworkReadyTimeout,
) -> None:
    state = load_health_state()
    now = _utc_now()

    failures = (
        int(
            state.get(
                "consecutive_ready_failures",
                0,
            )
        )
        + 1
    )

    state["consecutive_ready_failures"] = failures
    state["last_failure_kind"] = error.kind
    state["last_failure_at"] = _utc_iso(
        now
    )

    if not str(
        state.get(
            "degraded_since",
            "",
        )
        or ""
    ):
        state["degraded_since"] = _utc_iso(
            now
        )

    print(
        f"READY_FAILURE_COUNT={failures}",
        flush=True,
    )

    if (
        failures
        >= UPWORK_READY_FAILURE_THRESHOLD
    ):
        level = _coerce_nonnegative_int(
            state.get(
                "backoff_level",
                0,
            )
        )

        cooldown_minutes = (
            cooldown_minutes_for_level(
                level
            )
        )

        cooldown_until = (
            now
            + timedelta(
                minutes=cooldown_minutes
            )
        )

        state["cooldown_until"] = _utc_iso(
            cooldown_until
        )

        state["last_cooldown_minutes"] = (
            cooldown_minutes
        )

        max_level = max(
            0,
            len(
                UPWORK_COOLDOWN_STEPS_MINUTES
            )
            - 1,
        )

        state["backoff_level"] = min(
            level + 1,
            max_level,
        )

        print(
            "CIRCUIT_STATUS=OPEN",
            flush=True,
        )

        print(
            "COOLDOWN_MINUTES="
            f"{cooldown_minutes}",
            flush=True,
        )

        print(
            "COOLDOWN_UNTIL="
            f"{state['cooldown_until']}",
            flush=True,
        )

        print(
            "NEXT_COOLDOWN_MINUTES="
            f"{cooldown_minutes_for_level(state['backoff_level'])}",
            flush=True,
        )

        if not bool(
            state.get(
                "degraded_notified",
                False,
            )
        ):
            message = (
                "⚠ Upwork Monitor degraded\n"
                f"Upwork readiness failed {failures} consecutive times.\n"
                f"Last failure: {error.kind}.\n"
                "Monitoring entered a "
                f"{cooldown_minutes}-minute cooldown."
            )

            sent = await send_health_telegram(
                message
            )

            if sent:
                state["degraded_notified"] = True

                print(
                    "HEALTH_ALERT_SENT=DEGRADED",
                    flush=True,
                )

    else:
        state["cooldown_until"] = ""

        print(
            "CIRCUIT_STATUS=CLOSED",
            flush=True,
        )

    save_health_state(
        state
    )

    await maybe_send_long_degraded_alert(
        state
    )

    log_health_status(
        state
    )


async def register_ready_success() -> None:
    state = load_health_state()

    failures = int(
        state.get(
            "consecutive_ready_failures",
            0,
        )
    )

    had_cooldown = bool(
        str(
            state.get(
                "cooldown_until",
                "",
            )
            or ""
        )
    )

    was_degraded = (
        bool(
            state.get(
                "degraded_notified",
                False,
            )
        )
        or failures
        >= UPWORK_READY_FAILURE_THRESHOLD
        or had_cooldown
        or bool(
            str(
                state.get(
                    "degraded_since",
                    "",
                )
                or ""
            )
        )
    )

    outage_hours = degraded_hours(
        state
    )

    if (
        was_degraded
        and HEALTH_ALERTS_ENABLED
    ):
        downtime_text = ""

        if outage_hours is not None:
            downtime_text = (
                "\nContinuous degradation lasted about "
                f"{outage_hours:.1f} hours."
            )

        sent = await send_health_telegram(
            "✅ Upwork Monitor recovered\n"
            "Upwork search is ready again.\n"
            "Adaptive backoff reset to 30 minutes."
            + downtime_text
        )

        if sent:
            print(
                "HEALTH_ALERT_SENT=RECOVERED",
                flush=True,
            )

    state["consecutive_ready_failures"] = 0
    state["cooldown_until"] = ""
    state["degraded_notified"] = False
    state["degraded_since"] = ""
    state["long_degraded_notified"] = False
    state["backoff_level"] = 0
    state["last_cooldown_minutes"] = 0
    state["last_failure_kind"] = ""
    state["last_success_at"] = _utc_iso(
        _utc_now()
    )

    save_health_state(
        state
    )

    print(
        "READY_FAILURE_COUNT=0",
        flush=True,
    )

    print(
        "BACKOFF_LEVEL_RESET=0",
        flush=True,
    )

    print(
        "NEXT_COOLDOWN_MINUTES="
        f"{cooldown_minutes_for_level(0)}",
        flush=True,
    )

    print(
        "CIRCUIT_STATUS=CLOSED",
        flush=True,
    )

    log_health_status(
        state
    )


# ── Main ──────────────────────────────────────────────────────────────────────

async def minimize_browser_window(
    page,
) -> bool:
    """
    Minimize the headful Chrome window after the Upwork search page
    has already passed Cloudflare and real job tiles are visible.

    Cloudflare is intentionally allowed to run while the browser is
    visible because that mode is already proven to work on this PC.
    """

    if not MINIMIZE_BROWSER_AFTER_READY:
        print(
            "BROWSER_WINDOW_STATUS="
            "MINIMIZATION_DISABLED",
            flush=True,
        )

        return False

    try:
        await page.minimize()

        await asyncio.sleep(
            0.35
        )

        _window_id, bounds = await page.get_window()

        raw_state = getattr(
            bounds,
            "window_state",
            "",
        )

        state_value = getattr(
            raw_state,
            "value",
            raw_state,
        )

        state_text = str(
            state_value
        ).casefold()

        if "minimized" in state_text:
            print(
                "BROWSER_WINDOW_STATUS=MINIMIZED",
                flush=True,
            )

            return True

        print(
            "BROWSER_WINDOW_STATUS="
            "MINIMIZE_UNCONFIRMED "
            f"state={state_value!r}",
            flush=True,
        )

        return False

    except Exception as exc:
        print(
            "BROWSER_WINDOW_STATUS="
            "MINIMIZE_FAILED "
            f"{type(exc).__name__}: {exc}",
            flush=True,
        )

        return False


async def wait_for_search_ready(
    page,
) -> int:
    # Require both job tiles and hydrated title links to be stable.

    stable_ready = 0
    last_title = ""
    last_tile_count = 0
    last_link_count = 0

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
            link_count = await page.evaluate(
                "document.querySelectorAll("
                "\"article.job-tile "
                "[data-test*='job-tile-title-link'][href], "
                "article.job-tile "
                "[data-test*='job-tile-title'] a[href], "
                "article.job-tile h2 a[href]\""
                ").length"
            )
        except Exception:
            link_count = 0

        try:
            tile_count = int(
                tile_count
            )
        except Exception:
            tile_count = 0

        try:
            link_count = int(
                link_count
            )
        except Exception:
            link_count = 0

        last_title = str(
            title
            or ""
        )

        last_tile_count = tile_count
        last_link_count = link_count

        if (
            tile_count > 0
            and link_count > 0
        ):
            stable_ready += 1
        else:
            stable_ready = 0

        print(
            f"  ready={second:02d}s "
            f"title={last_title!r} "
            f"tiles={tile_count} "
            f"links={link_count} "
            f"stable={stable_ready}/"
            f"{DOM_READY_STABLE_SECONDS}",
            flush=True,
        )

        if (
            stable_ready
            >= DOM_READY_STABLE_SECONDS
        ):
            return tile_count

    folded_title = last_title.casefold()

    challenge_markers = (
        "just a moment",
        "verify you are human",
        "verification",
        "checking your browser",
    )

    if any(
        marker in folded_title
        for marker in challenge_markers
    ):
        kind = "CLOUDFLARE"

    elif (
        last_tile_count > 0
        and last_link_count == 0
    ):
        kind = "DOM_LINKS"

    else:
        kind = "UPWORK_READY"

    raise UpworkReadyTimeout(
        kind=kind,
        title=last_title,
        tile_count=last_tile_count,
        link_count=last_link_count,
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

    if (
        not dry_run
        and not bootstrap
    ):
        health = load_health_state()

        log_health_status(
            health
        )

        remaining = cooldown_remaining_seconds(
            health
        )

        if remaining > 0:
            print(
                "CIRCUIT_STATUS=COOLDOWN",
                flush=True,
            )

            print(
                f"COOLDOWN_REMAINING_SECONDS={remaining}",
                flush=True,
            )

            await maybe_send_long_degraded_alert(
                health
            )

            print(
                "RUN_RESULT=CIRCUIT_COOLDOWN",
                flush=True,
            )

            print(
                "RUN_STATUS=OK",
                flush=True,
            )

            return

        if (
            int(
                health.get(
                    "consecutive_ready_failures",
                    0,
                )
            )
            >= UPWORK_READY_FAILURE_THRESHOLD
        ):
            print(
                "CIRCUIT_STATUS=PROBE_AFTER_COOLDOWN",
                flush=True,
            )

            print(
                "PROBE_BACKOFF_LEVEL="
                f"{_coerce_nonnegative_int(health.get('backoff_level', 0))}",
                flush=True,
            )

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

        try:
            browser_tile_count = await wait_for_search_ready(
                page
            )

        except UpworkReadyTimeout as exc:
            if (
                not dry_run
                and not bootstrap
            ):
                await register_ready_failure(
                    exc
                )

            print(
                "RUN_RESULT="
                "UPWORK_READY_TIMEOUT_"
                f"{exc.kind}",
                flush=True,
            )

            raise

        else:
            if (
                not dry_run
                and not bootstrap
            ):
                await register_ready_success()

        print(
            f"Browser tiles ready: {browser_tile_count}",
            flush=True,
        )

        await minimize_browser_window(
            page
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
            print(
                "RUN_RESULT=PARSER_NO_JOBS",
                flush=True,
            )

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

            print(
                "RUN_RESULT=BOOTSTRAP_OK",
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
                "RUN_RESULT=DRY_RUN_OK",
                flush=True,
            )

            print(
                "DRY_RUN_STATUS=OK",
                flush=True,
            )
        else:
            print(
                "RUN_RESULT=OK",
                flush=True,
            )

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
    lock_handle = acquire_single_instance_lock()

    if lock_handle is None:
        print(
            "SINGLE_INSTANCE_STATUS="
            "SKIPPED_ALREADY_RUNNING",
            flush=True,
        )

        print(
            "RUN_RESULT=SKIPPED_ALREADY_RUNNING",
            flush=True,
        )

        sys.exit(0)

    print(
        "SINGLE_INSTANCE_STATUS=ACQUIRED",
        flush=True,
    )

    exit_code = 0

    try:
        args = parse_cli_args()

        if args.dry_run and args.bootstrap:
            print(
                "RUN_RESULT=CLI_ERROR",
                flush=True,
            )

            print(
                "ERROR: use either --dry-run "
                "or --bootstrap, not both.",
                flush=True,
            )

            exit_code = 2

        else:
            uc.loop().run_until_complete(
                run_monitor(
                    dry_run=args.dry_run,
                    bootstrap=args.bootstrap,
                )
            )

    except UpworkReadyTimeout as exc:
        print(
            f"ERROR_TYPE={type(exc).__name__}",
            flush=True,
        )

        print(
            f"ERROR_MESSAGE={exc}",
            flush=True,
        )

        exit_code = 1

    except Exception as exc:
        print(
            "RUN_RESULT=UNHANDLED_EXCEPTION",
            flush=True,
        )

        print(
            f"ERROR_TYPE={type(exc).__name__}",
            flush=True,
        )

        print(
            f"ERROR_MESSAGE={exc}",
            flush=True,
        )

        traceback.print_exc()

        exit_code = 1

    finally:
        release_single_instance_lock(
            lock_handle
        )

        print(
            "SINGLE_INSTANCE_STATUS=RELEASED",
            flush=True,
        )

    sys.exit(
        exit_code
    )
