#!/usr/bin/env python3
"""
Scrape the MA hunting season summary page and write seasons.json.
Run this locally (residential IP) whenever the season data changes - typically once a year.

  python scrape.py            # writes seasons.json
  python scrape.py --dry-run  # prints rows, does not write
"""
import json
import re
import sys
from datetime import date
from pathlib import Path

import requests
from bs4 import BeautifulSoup

MA_HUNTING_URL = (
    "https://www.mass.gov/info-details/"
    "2026-hunting-and-freshwater-fishing-season-summary"
)
MA_MIGRATORY_URL = (
    "https://www.mass.gov/doc/"
    "2025-2026-migratory-game-bird-regulations/download"
)

# Default python-requests UA gets 200; spoofed Chrome UA triggers WAF 403
HEADERS = {}

MONTH_MAP = {
    "jan": 1, "january": 1,
    "feb": 2, "february": 2,
    "mar": 3, "march": 3,
    "apr": 4, "april": 4,
    "may": 5,
    "jun": 6, "june": 6,
    "jul": 7, "july": 7,
    "aug": 8, "august": 8,
    "sept": 9, "sep": 9, "september": 9,
    "oct": 10, "october": 10,
    "nov": 11, "november": 11,
    "dec": 12, "december": 12,
}

FURBEARER_KEYWORDS = frozenset({
    "bobcat", "coyote", "cottontail", "fox", "squirrel",
    "opossum", "raccoon", "snowshoe", "hare", "trapping",
})


def _infer_year(month: int, base_year: int) -> int:
    return base_year + 1 if month <= 8 else base_year


def _parse_dates(raw: str, base_year: int = 2026) -> list[tuple[str, str]]:
    """
    Parse MA hunting date strings into a list of (start_iso, end_iso) pairs.

    Handles: dotted abbreviations (Jan., Sept.), en/em dashes, spaces around
    dashes, parenthetical modifiers (stripped), cross-year explicit ranges
    (Dec 21, 2026 - Mar 8, 2027), multiple entries in one cell
    (April 27 - May 23, 2026 Oct. 5 - Nov. 28, 2026), and comma-separated
    individual days (Sept. 5, 12, 19 and Oct. 3, 10, 2026).
    """
    s = raw.strip()

    # Normalise en-dash / em-dash to ASCII hyphen
    s = re.sub(r"[–—‒―]", "-", s)

    # Strip trailing periods from month abbreviations: "Jan." -> "Jan"
    s = re.sub(r"\b([A-Za-z]+)\.", r"\1", s)

    # Strip parenthetical modifiers: "(Mon, Fri, Sat only)" etc.
    s = re.sub(r"\s*\([^)]*\)", "", s).strip()

    # "and" -> "," for lists like "Sept 5, 12, 19 and Oct 3, 10"
    s = re.sub(r"\s+and\s+", ", ", s, flags=re.I)

    # Skip non-date values
    if re.search(r"\b(open|closed|tbd|varies|see|n/?a)\b", s, re.I):
        return []

    # Split cells with multiple date entries:
    #   "April 27 - May 23, 2026 Oct 5 - Nov 28, 2026"
    # Split only where a year is immediately followed by whitespace then a
    # letter (not a dash, which belongs to a cross-year range like "2026 -Mar").
    entries = re.split(r"(?<=\d{4})\s+(?=[A-Za-z])", s)

    results: list[tuple[str, str]] = []
    for entry in entries:
        entry = entry.strip().rstrip(",").strip()
        results.extend(_parse_entry(entry, base_year))
    return results


def _parse_entry(s: str, base_year: int) -> list[tuple[str, str]]:
    """Parse a single normalised date entry into (start_iso, end_iso) pairs."""

    def iso(y: int, m: int, d: int) -> str:
        return date(y, m, d).isoformat()

    # -- Patterns that extract explicit years before any stripping -----------

    # "Month D[, Y] - Month D[, Y]"  cross-month range with optional per-date years
    m = re.fullmatch(
        r"(\w+)\s+(\d+)(?:,\s*(20\d{2}))?\s*-\s*(\w+)\s+(\d+)(?:,\s*(20\d{2}))?",
        s,
    )
    if m:
        mon1 = MONTH_MAP.get(m.group(1).lower())
        mon2 = MONTH_MAP.get(m.group(4).lower())
        if mon1 and mon2:
            y1s, y2s = m.group(3), m.group(6)
            if y1s and y2s:
                y1, y2 = int(y1s), int(y2s)
            elif y2s:
                y2 = int(y2s)
                y1 = y2 if mon1 <= mon2 else y2 - 1
            elif y1s:
                y1 = int(y1s)
                y2 = y1 if mon1 <= mon2 else y1 + 1
            else:
                y1 = _infer_year(mon1, base_year)
                y2 = y1 if mon1 <= mon2 else y1 + 1
            return [(iso(y1, mon1, int(m.group(2))), iso(y2, mon2, int(m.group(5))))]

    # "Month D-D[, Y]"  same-month range with optional year
    m = re.fullmatch(r"(\w+)\s+(\d+)\s*-\s*(\d+)(?:,\s*(20\d{2}))?", s)
    if m:
        mon = MONTH_MAP.get(m.group(1).lower())
        if mon:
            yr = int(m.group(4)) if m.group(4) else _infer_year(mon, base_year)
            return [(iso(yr, mon, int(m.group(2))), iso(yr, mon, int(m.group(3))))]

    # -- Strip year(s) for simpler remaining patterns ------------------------

    year_m = re.search(r"\b(20\d{2})\b", s)
    explicit_year: int | None = int(year_m.group(1)) if year_m else None
    if explicit_year:
        s = re.sub(r",?\s*20\d{2}", "", s).strip()

    def get_year(month: int) -> int:
        return explicit_year if explicit_year else _infer_year(month, base_year)

    # "Month D"  single day
    m = re.fullmatch(r"(\w+)\s+(\d+)", s)
    if m:
        mon = MONTH_MAP.get(m.group(1).lower())
        if mon:
            d = date(get_year(mon), mon, int(m.group(2)))
            return [(d.isoformat(), d.isoformat())]

    # "Sept 5, 12, 19, Oct 3, 10"  multiple individual days
    parts = [p.strip() for p in s.split(",")]
    individual: list[tuple[str, str]] = []
    current_mon: int | None = None
    for part in parts:
        mm = re.fullmatch(r"(\w+)\s+(\d+)", part)
        if mm:
            current_mon = MONTH_MAP.get(mm.group(1).lower())
            if current_mon:
                d = date(get_year(current_mon), current_mon, int(mm.group(2)))
                individual.append((d.isoformat(), d.isoformat()))
        elif re.fullmatch(r"\d+", part) and current_mon:
            d = date(get_year(current_mon), current_mon, int(part))
            individual.append((d.isoformat(), d.isoformat()))
    if individual:
        return individual

    if s:
        print(f"  [WARN] Could not parse: {s!r}", file=sys.stderr)
    return []


def _categorise(section: str, season: str) -> str:
    combined = f"{section} {season}".lower()
    if any(kw in combined for kw in FURBEARER_KEYWORDS):
        return "furbearer"
    if any(kw in combined for kw in ("migratory", "waterfowl", "duck", "goose", "geese", "woodcock")):
        return "migratory"
    return "hunting"


def _season_col(hdrs: list[str]) -> int | None:
    for i, h in enumerate(hdrs):
        if any(kw in h for kw in ("season", "species", "hunt", "youth")):
            return i
    return 0 if hdrs else None


def _date_col(hdrs: list[str]) -> int | None:
    return next((i for i, h in enumerate(hdrs) if "date" in h), None)


def _zone_col(hdrs: list[str]) -> int | None:
    return next((i for i, h in enumerate(hdrs) if "zone" in h), None)


def scrape_hunting() -> list[dict]:
    print(f"Fetching {MA_HUNTING_URL} ...", file=sys.stderr)
    resp = requests.get(MA_HUNTING_URL, headers=HEADERS, timeout=30)
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "html.parser")

    rows: list[dict] = []
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
        hdrs = [c.get_text(strip=True).lower() for c in first_row.find_all(["th", "td"])]

        sc = _season_col(hdrs)
        dc = _date_col(hdrs)
        zc = _zone_col(hdrs)

        if sc is None or dc is None:
            continue

        for row in elem.find_all("tr")[1:]:
            cells = row.find_all(["td", "th"])

            def cell(idx: int | None) -> str:
                if idx is None or len(cells) <= idx:
                    return ""
                return cells[idx].get_text(" ", strip=True)

            season_name = cell(sc)
            date_text   = cell(dc)
            zone_text   = cell(zc) or "Statewide"

            if not season_name or not date_text:
                continue

            dates = _parse_dates(date_text)
            rows.append({
                "section":  current_section,
                "season":   season_name,
                "zone":     zone_text,
                "date_str": date_text,
                "dates":    dates,
                "category": _categorise(current_section, season_name),
            })

    return rows


def scrape_migratory() -> list[dict]:
    try:
        import pdfplumber
        import io as _io
    except ImportError:
        print("[WARN] pdfplumber not installed -- skipping migratory PDF", file=sys.stderr)
        return []

    print("Fetching migratory bird PDF ...", file=sys.stderr)
    try:
        resp = requests.get(MA_MIGRATORY_URL, headers=HEADERS, timeout=60)
        resp.raise_for_status()
    except Exception as e:
        print(f"[WARN] Could not fetch migratory PDF: {e}", file=sys.stderr)
        return []

    rows: list[dict] = []
    try:
        with pdfplumber.open(_io.BytesIO(resp.content)) as pdf:
            for page in pdf.pages:
                text = page.extract_text() or ""
                for line in text.splitlines():
                    match = re.search(
                        r"(.+?)\s{2,}"
                        r"((?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sept?|Oct|Nov|Dec)\w*\.?\s+.+)",
                        line,
                    )
                    if match:
                        season_name = match.group(1).strip()
                        date_text   = match.group(2).strip()
                        dates       = _parse_dates(date_text)
                        if dates:
                            rows.append({
                                "section":  "Migratory Bird",
                                "season":   season_name,
                                "zone":     "Statewide",
                                "date_str": date_text,
                                "dates":    dates,
                                "category": "migratory",
                            })
    except Exception as e:
        print(f"[WARN] Could not parse migratory PDF: {e}", file=sys.stderr)

    return rows


def main() -> None:
    dry_run = "--dry-run" in sys.argv

    rows = scrape_hunting() + scrape_migratory()

    by_cat: dict[str, int] = {}
    for r in rows:
        by_cat[r["category"]] = by_cat.get(r["category"], 0) + 1
    print(
        f"Scraped {len(rows)} rows -- "
        f"hunting={by_cat.get('hunting', 0)}  "
        f"furbearer={by_cat.get('furbearer', 0)}  "
        f"migratory={by_cat.get('migratory', 0)}",
        file=sys.stderr,
    )

    if dry_run:
        for r in rows:
            print(f"  [{r['category']:9}] {r['section']} / {r['season']} ({r['zone']}) -- {r['date_str']}")
            for d in r["dates"][:2]:
                print(f"              -> {d[0]} to {d[1]}")
        return

    out_path = Path(__file__).parent / "seasons.json"
    out_path.write_text(json.dumps(rows, indent=2), encoding="utf-8")
    print(f"Wrote {out_path}", file=sys.stderr)


if __name__ == "__main__":
    main()
