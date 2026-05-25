#!/usr/bin/env python3
"""
MA Hunting Calendar Sync
Scrapes the Massachusetts hunting season summary and syncs events to three
Google Calendars: Hunting, Furbearer/Trapping, and Migratory Bird.
"""
import hashlib
import io
import logging
import os
import re
import sys
from datetime import date, timedelta
from logging.handlers import RotatingFileHandler
from pathlib import Path

import requests
from bs4 import BeautifulSoup
from google.auth import default as google_auth_default
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

# ── Logging ────────────────────────────────────────────────────────────────────
_log_dir = Path(__file__).parent / "logs"
_log_dir.mkdir(exist_ok=True)

_fmt = logging.Formatter("%(asctime)s %(levelname)s %(message)s")
_root = logging.getLogger()
_root.setLevel(logging.INFO)

_sh = logging.StreamHandler(sys.stdout)
_sh.setFormatter(_fmt)
_root.addHandler(_sh)

_fh = RotatingFileHandler(_log_dir / "sync.log", maxBytes=1_000_000, backupCount=3)
_fh.setFormatter(_fmt)
_root.addHandler(_fh)

logger = logging.getLogger(__name__)

# ── Config ─────────────────────────────────────────────────────────────────────
HUNTING_CALENDAR_ID   = os.environ.get("HUNTING_CALENDAR_ID",   "primary")
FURBEARER_CALENDAR_ID = os.environ.get("FURBEARER_CALENDAR_ID", "primary")
MIGRATORY_CALENDAR_ID = os.environ.get("MIGRATORY_CALENDAR_ID", "primary")
SCOPES = ["https://www.googleapis.com/auth/calendar"]

MA_HUNTING_URL = (
    "https://www.mass.gov/info-details/"
    "2026-hunting-and-freshwater-fishing-season-summary"
)
MA_MIGRATORY_URL = (
    "https://www.mass.gov/doc/"
    "2025-2026-migratory-game-bird-regulations/download"
)

# Season names containing these keywords go to the Furbearer/Trapping calendar
FURBEARER_KEYWORDS = frozenset({
    "bobcat", "coyote", "cottontail", "fox", "squirrel",
    "opossum", "raccoon", "snowshoe", "hare", "trapping",
})

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate, br",
    "Connection": "keep-alive",
    "Upgrade-Insecure-Requests": "1",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-User": "?1",
    "Cache-Control": "max-age=0",
}

# ── Date parsing ───────────────────────────────────────────────────────────────
MONTH_MAP = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sept": 9, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
}


def _infer_year(month: int, base_year: int) -> int:
    """Hunt year runs fall→spring; Jan–Aug belong to the +1 calendar year."""
    return base_year + 1 if month <= 8 else base_year


def _parse_dates(raw: str, base_year: int = 2025) -> list[tuple[date, date]]:
    """
    Parse MA hunting date strings → list of (start, end) date pairs.

    Handles:
      "Jan 1–Feb 14, 2026"         cross-month with explicit year
      "Oct 5–Nov 28"               cross-month, year inferred
      "Oct 29–31"                  same-month range
      "Oct 3"                      single day
      "Sept 5, 12, 19, Oct 3, 10"  multiple individual days
    Returns [] if the string cannot be parsed.
    """
    s = raw.strip()
    # Normalise dash characters
    for ch in ("–", "—", "‒", "–", "—"):
        s = s.replace(ch, "-")

    # Skip non-date values
    if re.search(r"\b(open|closed|tbd|varies|see|n/?a)\b", s, re.I):
        return []

    # Extract explicit year
    year_m = re.search(r"\b(20\d{2})\b", s)
    explicit_year: int | None = int(year_m.group(1)) if year_m else None
    if explicit_year:
        s = re.sub(r",?\s*20\d{2}", "", s).strip()

    def get_year(month: int) -> int:
        return explicit_year if explicit_year else _infer_year(month, base_year)

    # "Month D1-D2" — same-month range
    m = re.fullmatch(r"(\w+)\s+(\d+)-(\d+)", s)
    if m:
        mon = MONTH_MAP.get(m.group(1).lower())
        if mon:
            yr = get_year(mon)
            return [(date(yr, mon, int(m.group(2))), date(yr, mon, int(m.group(3))))]

    # "Month D1-Month D2" — cross-month range
    m = re.fullmatch(r"(\w+)\s+(\d+)-(\w+)\s+(\d+)", s)
    if m:
        mon1 = MONTH_MAP.get(m.group(1).lower())
        mon2 = MONTH_MAP.get(m.group(3).lower())
        if mon1 and mon2:
            y1 = get_year(mon1)
            y2 = get_year(mon2)
            # End month < start month means it crosses into the next calendar year
            if explicit_year is None and mon2 < mon1:
                y2 = y1 + 1
            return [(date(y1, mon1, int(m.group(2))), date(y2, mon2, int(m.group(4))))]

    # "Month D" — single day
    m = re.fullmatch(r"(\w+)\s+(\d+)", s)
    if m:
        mon = MONTH_MAP.get(m.group(1).lower())
        if mon:
            yr = get_year(mon)
            d = date(yr, mon, int(m.group(2)))
            return [(d, d)]

    # "Sept 5, 12, 19, Oct 3, 10" — multiple individual days
    parts = [p.strip() for p in s.split(",")]
    results: list[tuple[date, date]] = []
    current_mon: int | None = None
    for part in parts:
        mm = re.fullmatch(r"(\w+)\s+(\d+)", part)
        if mm:
            current_mon = MONTH_MAP.get(mm.group(1).lower())
            if current_mon:
                yr = get_year(current_mon)
                d = date(yr, current_mon, int(mm.group(2)))
                results.append((d, d))
        elif re.fullmatch(r"\d+", part) and current_mon:
            yr = get_year(current_mon)
            d = date(yr, current_mon, int(part))
            results.append((d, d))
    if results:
        return results

    logger.warning("Could not parse date string: %r", raw)
    return []


# ── Categorisation ─────────────────────────────────────────────────────────────
def _categorise(section: str, season: str) -> str:
    combined = f"{section} {season}".lower()
    if any(kw in combined for kw in FURBEARER_KEYWORDS):
        return "furbearer"
    if any(kw in combined for kw in ("migratory", "waterfowl", "duck", "goose", "geese", "woodcock")):
        return "migratory"
    return "hunting"


# ── Scraping ───────────────────────────────────────────────────────────────────
def scrape_hunting_seasons() -> list[dict]:
    """Scrape the MA 2026 hunting season summary page."""
    logger.info("Fetching %s", MA_HUNTING_URL)
    resp = requests.get(MA_HUNTING_URL, headers=HEADERS, timeout=30)
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "html.parser")

    seasons: list[dict] = []
    current_section = "General"

    for elem in soup.find_all(["h3", "table"]):
        if elem.name == "h3":
            current_section = elem.get_text(strip=True)
            continue

        if "ma__table" not in (elem.get("class") or []):
            continue

        first_row = elem.find("tr")
        if not first_row:
            continue
        header_cells = first_row.find_all(["th", "td"])
        hdrs = [c.get_text(strip=True).lower() for c in header_cells]

        season_col = next((i for i, h in enumerate(hdrs) if "season" in h), None)
        date_col   = next((i for i, h in enumerate(hdrs) if "date" in h), None)
        zone_col   = next((i for i, h in enumerate(hdrs) if "zone" in h), None)

        if season_col is None or date_col is None:
            continue

        for row in elem.find_all("tr")[1:]:
            cells = row.find_all(["td", "th"])

            def cell(idx: int | None) -> str:
                if idx is None or len(cells) <= idx:
                    return ""
                return cells[idx].get_text(" ", strip=True)

            season_name = cell(season_col)
            date_text   = cell(date_col)
            zone_text   = cell(zone_col) or "Statewide"

            if not season_name or not date_text:
                continue

            dates = _parse_dates(date_text)
            seasons.append({
                "section":  current_section,
                "season":   season_name,
                "zone":     zone_text,
                "date_str": date_text,
                "dates":    dates,
                "category": _categorise(current_section, season_name),
            })

    logger.info("Scraped %d season rows from hunting page", len(seasons))
    return seasons


def scrape_migratory_seasons() -> list[dict]:
    """
    Attempt to extract season dates from the migratory bird regulations PDF.
    Falls back to an empty list on any error (non-fatal).
    """
    try:
        import pdfplumber  # optional dependency
    except ImportError:
        logger.warning("pdfplumber not installed — skipping migratory PDF scrape")
        return []

    logger.info("Fetching migratory bird PDF")
    try:
        resp = requests.get(MA_MIGRATORY_URL, headers=HEADERS, timeout=60)
        resp.raise_for_status()
    except Exception as e:
        logger.warning("Could not fetch migratory PDF: %s", e)
        return []

    seasons: list[dict] = []
    try:
        with pdfplumber.open(io.BytesIO(resp.content)) as pdf:
            for page in pdf.pages:
                text = page.extract_text() or ""
                for line in text.splitlines():
                    # Match lines like: "Ducks (except sea ducks)    Oct 4 – Nov 28"
                    m = re.search(
                        r"(.+?)\s{2,}"
                        r"((?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sept?|Oct|Nov|Dec)\s+.+)",
                        line,
                    )
                    if m:
                        season_name = m.group(1).strip()
                        date_text   = m.group(2).strip()
                        dates       = _parse_dates(date_text)
                        if dates:
                            seasons.append({
                                "section":  "Migratory Bird",
                                "season":   season_name,
                                "zone":     "Statewide",
                                "date_str": date_text,
                                "dates":    dates,
                                "category": "migratory",
                            })
    except Exception as e:
        logger.warning("Could not parse migratory PDF: %s", e)
        return []

    logger.info("Extracted %d season rows from migratory PDF", len(seasons))
    return seasons


# ── Event building ─────────────────────────────────────────────────────────────
def _event_id(season: str, zone: str, start: date) -> str:
    key = f"{season}|{zone}|{start.isoformat()}".lower()
    return "ma_" + hashlib.md5(key.encode()).hexdigest()[:16]


def build_event(row: dict, start: date, end: date) -> dict:
    zone = row["zone"]
    summary = f"MA: {row['season']}"
    if zone and zone.lower() not in ("statewide", ""):
        summary += f" ({zone})"

    description = "\n".join([
        f"Section: {row['section']}",
        f"Dates: {row['date_str']}",
        f"Zone: {zone}",
        "",
        f"Source: {MA_HUNTING_URL}",
    ])

    # Google Calendar all-day end date is exclusive
    end_exclusive = (end + timedelta(days=1)).isoformat()

    return {
        "summary":     summary,
        "description": description,
        "start":       {"date": start.isoformat()},
        "end":         {"date": end_exclusive},
        "extendedProperties": {
            "private": {
                "ma_hunting_id":   _event_id(row["season"], zone, start),
                "ma_hunting_sync": "true",
            }
        },
    }


# ── Calendar sync ──────────────────────────────────────────────────────────────
def get_calendar_service():
    creds, _ = google_auth_default(scopes=SCOPES)
    return build("calendar", "v3", credentials=creds, cache_discovery=False)


def get_existing_events(service, calendar_id: str) -> dict[str, dict]:
    events: dict[str, dict] = {}
    page_token = None
    while True:
        result = (
            service.events()
            .list(
                calendarId=calendar_id,
                privateExtendedProperty="ma_hunting_sync=true",
                maxResults=250,
                pageToken=page_token,
                showDeleted=False,
            )
            .execute()
        )
        for ev in result.get("items", []):
            ma_id = ev.get("extendedProperties", {}).get("private", {}).get("ma_hunting_id")
            if ma_id:
                events[ma_id] = ev
        page_token = result.get("nextPageToken")
        if not page_token:
            break
    return events


def sync_calendar(service, calendar_id: str, rows: list[dict], label: str) -> int:
    existing = get_existing_events(service, calendar_id)
    logger.info("[%s] %d existing  |  %d source rows", label, len(existing), len(rows))

    # Build the full set of wanted events (one per date-range)
    wanted: dict[str, dict] = {}
    for row in rows:
        for start, end in row["dates"]:
            ma_id = _event_id(row["season"], row["zone"], start)
            wanted[ma_id] = build_event(row, start, end)

    created = updated = deleted = errors = 0

    for ma_id, event in wanted.items():
        try:
            if ma_id in existing:
                service.events().patch(
                    calendarId=calendar_id,
                    eventId=existing[ma_id]["id"],
                    body=event,
                ).execute()
                updated += 1
                logger.info("[%s] Updated: %s", label, event["summary"])
            else:
                service.events().insert(
                    calendarId=calendar_id,
                    body=event,
                ).execute()
                created += 1
                logger.info("[%s] Created: %s", label, event["summary"])
        except HttpError as e:
            logger.error("[%s] API error for %s: %s", label, event["summary"], e)
            errors += 1
        except Exception as e:
            logger.error("[%s] Unexpected error for %s: %s", label, event["summary"], e)
            errors += 1

    for ma_id, ev in existing.items():
        if ma_id not in wanted:
            try:
                service.events().delete(
                    calendarId=calendar_id,
                    eventId=ev["id"],
                ).execute()
                deleted += 1
                logger.info("[%s] Deleted: %s (no longer in source)", label, ev.get("summary", ma_id))
            except HttpError as e:
                logger.error("[%s] Delete error for %s: %s", label, ma_id, e)
                errors += 1

    logger.info(
        "[%s] created=%d  updated=%d  deleted=%d  errors=%d",
        label, created, updated, deleted, errors,
    )
    return errors


# ── Entry point ────────────────────────────────────────────────────────────────
def sync_all() -> int:
    logger.info("MA Hunting Calendar sync started")

    all_rows  = scrape_hunting_seasons()
    all_rows += scrape_migratory_seasons()

    hunting_rows   = [r for r in all_rows if r["category"] == "hunting"]
    furbearer_rows = [r for r in all_rows if r["category"] == "furbearer"]
    migratory_rows = [r for r in all_rows if r["category"] == "migratory"]

    logger.info(
        "Rows — hunting: %d  furbearer: %d  migratory: %d",
        len(hunting_rows), len(furbearer_rows), len(migratory_rows),
    )

    service = get_calendar_service()
    errors  = 0
    errors += sync_calendar(service, HUNTING_CALENDAR_ID,   hunting_rows,   "Hunting")
    errors += sync_calendar(service, FURBEARER_CALENDAR_ID, furbearer_rows, "Furbearer")
    errors += sync_calendar(service, MIGRATORY_CALENDAR_ID, migratory_rows, "Migratory")

    logger.info("Sync complete — total errors: %d", errors)
    return errors


if __name__ == "__main__":
    sys.exit(sync_all())
