#!/usr/bin/env python3
"""
fetch_events.py — Pull Maryland Forward Party events for a given month.

The party's website (marylandforwardparty.com) is a Wix site. The public
/events and /get-involved pages render their event lists with JavaScript, so a
plain HTTP fetch returns a ~2.6 MB bundle with no readable events in it. Do NOT
try to scrape the rendered page.

Instead this script uses the two structured sources Wix publishes for crawlers:

  1. event-pages-sitemap.xml  -> the canonical list of every event URL. The
     slugs encode the date and time, e.g.
         door-knocking-2026-06-13-07-00   -> 2026-06-13 07:00
         code-forward-md-2026-06-15-19-00 -> 2026-06-15 19:00
     so the month's dated events can be filtered without fetching anything.

  2. schema.org JSON-LD  -> each event page embeds a
         <script type="application/ld+json"> ... "@type":"Event" ... </script>
     block in its raw HTML with the exact startDate, endDate, location, and
     description. We parse that directly (no summarizer, no JS) for the details.

Named one-off events (festivals, forums, book clubs) have no date in the slug,
so for those we read the JSON-LD startDate to decide whether they fall in the
target month.

Committee/internal meetings are excluded by default because the newsletter
audience is people who are not yet party members.

Usage:
    python3 fetch_events.py 2026-06            # human-readable list
    python3 fetch_events.py 2026-06 --json     # JSON for scripting
    python3 fetch_events.py 2026-06 --all       # include committee meetings

Stdlib only (urllib, json, re, datetime, concurrent.futures) — no pip installs.
"""

import sys
import re
import json
import html
import argparse
import urllib.request
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor

BASE = "https://www.marylandforwardparty.com"
SITEMAP = f"{BASE}/event-pages-sitemap.xml"
UA = "Mozilla/5.0 (compatible; mdfwd-newsletter/1.0)"

# Slug substrings that mark internal committee / team / board meetings. These
# are not for the public newsletter. Add to this list if new meeting series
# appear in the sitemap.
COMMITTEE_PATTERNS = (
    "committee-meeting",
    "committee-monthly",
    "committee-check-in",
    "committee-monthly-team-mtg",
    "monthly-team-mtg",
    "county-leads-call",
    "team-leads-call",
    "volunteer-coordination-monthly",
    "monthly-volunteer-committee",
    "monthly-communications-committee",
    "monthly-legislative-affairs-committee",
    "community-action-committee",
    "community-outreach-engagement-monthly",
    "candidates-committee-monthly",
    "open-board-meeting",
    "baltimore-team-meeting",
    "monthly-county-leads",
)

SLUG_DATE_RE = re.compile(r"(\d{4})-(\d{2})-(\d{2})-(\d{2})-(\d{2})")
JSONLD_RE = re.compile(
    r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
    re.S,
)


def http_get(url: str, timeout: int = 30) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read().decode("utf-8", errors="ignore")


def list_event_slugs() -> list[str]:
    """Return every event slug from the sitemap."""
    xml = http_get(SITEMAP)
    slugs = re.findall(r"event-details/([a-z0-9.\-]+)", xml)
    # de-dupe, preserve order
    seen, out = set(), []
    for s in slugs:
        if s not in seen:
            seen.add(s)
            out.append(s)
    return out


def is_committee(slug: str) -> bool:
    return any(p in slug for p in COMMITTEE_PATTERNS)


def slug_month(slug: str) -> str | None:
    """Return 'YYYY-MM' encoded in the slug, or None if the slug has no date."""
    m = SLUG_DATE_RE.search(slug)
    return f"{m.group(1)}-{m.group(2)}" if m else None


def parse_jsonld_event(slug: str) -> dict | None:
    """Fetch an event page and return its schema.org Event data, or None."""
    try:
        page = http_get(f"{BASE}/event-details/{slug}")
    except Exception as e:
        return {"slug": slug, "error": str(e)}
    for block in JSONLD_RE.findall(page):
        try:
            data = json.loads(block)
        except Exception:
            continue
        for obj in data if isinstance(data, list) else [data]:
            if isinstance(obj, dict) and "Event" in str(obj.get("@type", "")):
                loc = obj.get("location", {})
                if isinstance(loc, dict):
                    name = loc.get("name", "") or ""
                    addr = loc.get("address", "")
                    if isinstance(addr, dict):
                        addr = addr.get("streetAddress", "") or addr.get("name", "")
                    location = ", ".join(p for p in (name, addr) if p)
                else:
                    location = str(loc)
                desc = html.unescape(obj.get("description") or "")
                desc = re.sub(r"\s+", " ", desc).strip()
                location = html.unescape(location)
                name = html.unescape(obj.get("name", ""))
                return {
                    "slug": slug,
                    "name": name,
                    "start": obj.get("startDate", ""),
                    "end": obj.get("endDate", ""),
                    "location": location,
                    "description": desc,
                    "url": f"{BASE}/event-details/{slug}",
                }
    return None


def month_of_iso(iso: str) -> str | None:
    try:
        return datetime.fromisoformat(iso).strftime("%Y-%m")
    except Exception:
        return None


def collect(target_month: str, include_committee: bool) -> list[dict]:
    slugs = list_event_slugs()
    if not include_committee:
        slugs = [s for s in slugs if not is_committee(s)]

    # Candidates to fetch:
    #  - dated slugs whose encoded month matches the target
    #  - undated (named) slugs, which we must open to read the real startDate
    candidates = [
        s for s in slugs
        if slug_month(s) == target_month or slug_month(s) is None
    ]

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(parse_jsonld_event, candidates))

    events = []
    for r in results:
        if not r or r.get("error") or not r.get("start"):
            continue
        if month_of_iso(r["start"]) == target_month:
            events.append(r)
    events.sort(key=lambda e: e["start"])
    return events


def fmt(e: dict) -> str:
    try:
        s = datetime.fromisoformat(e["start"])
        en = datetime.fromisoformat(e["end"]) if e.get("end") else None
        when = s.strftime("%a %b %-d, %Y  %-I:%M %p")
        if en:
            when += en.strftime(" – %-I:%M %p")
    except Exception:
        when = e["start"]
    lines = [f"- **{e['name']}** — {when}"]
    if e.get("location"):
        lines.append(f"  Location: {e['location']}")
    if e.get("description"):
        lines.append(f"  {e['description']}")
    lines.append(f"  {e['url']}")
    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser(description="Fetch MD Forward Party events for a month.")
    ap.add_argument("month", help="Target month as YYYY-MM, e.g. 2026-06")
    ap.add_argument("--json", action="store_true", help="Emit JSON instead of text")
    ap.add_argument("--all", action="store_true", help="Include committee/internal meetings")
    args = ap.parse_args()

    if not re.fullmatch(r"\d{4}-\d{2}", args.month):
        ap.error("month must be in YYYY-MM format, e.g. 2026-06")

    events = collect(args.month, include_committee=args.all)

    if args.json:
        print(json.dumps(events, indent=2))
    else:
        if not events:
            print(f"No public events found for {args.month}.")
            return 0
        print(f"# Maryland Forward Party events — {args.month}\n")
        for e in events:
            print(fmt(e))
            print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
