"""
fetch_trip_reports.py

Fetches rider trip-report pages and accumulates CANDIDATE numeric fields
(distance, duration, fuel, mileage mentions) with surrounding context into
a single CSV for a quick manual confirm pass.

WHY THESE URLS AND NOT BCMTOURING:
BCMTouring's domain appears to have lapsed and been taken over by an
unrelated (gambling-adjacent) site -- confirmed by search results showing
its old URLs now serving betting content. Don't use it as a source.

WHY THIS SEED LIST (v2): the first pass pulled from broad "Ladakh trip"
searches and only 1 of 10 URLs actually isolated a clean distance figure
for our two target routes (manali_leh, spiti_circuit) -- most were
multi-state trips, unrelated loops (Nubra/Pangong/Hanle), or one was
completely off-topic (a Royal Enfield dealership tour). This list is
curated to be route-specific: either a personal trip report of exactly
Manali-Leh or Shimla-Kaza, or a route-distance reference guide that
states an explicit, sourced km figure for the same route (rental
company / cycling-tour blogs -- not first-person narrative, but useful
ground truth to cross-check the physics baseline's computed distance
against). Both categories still go through manual confirm -- see below.

WHY CANDIDATES, NOT FULLY AUTO-STRUCTURED ROWS:
Trip reports are unstructured prose written by different people in
different formats. A regex confident enough to auto-fill a
"total_distance_km" column will occasionally grab the wrong number (a
day's distance instead of the trip total, etc). Cheaper and more
reliable to extract everything that LOOKS relevant, tag the pattern that
matched, keep the surrounding sentence as context, and let a human
(you) pick the real value in a few minutes per report -- rather than
silently trusting a wrong parse that feeds bad labels into the residual
model later.

USAGE:
    pip install requests --break-system-packages
    python3 fetch_trip_reports.py

Edit SEED_URLS below to add more report URLs as you find them. If a
new URL 404s / times out / returns something that looks like a
different site entirely (redirect hijack, bot-block page), the script
will flag it -- don't ignore that warning, check the domain is still
legitimately owned before trusting content from it.

NEW IN v2 -- boilerplate detection: the last run had the exact same
sentence (a Kia EV ad fragment) show up as a "candidate" on every
single team-bhp.com URL, because it's a shared sidebar widget, not
article content. Real trip narratives from different authors don't
repeat verbatim. So after collecting all candidates, any context
string that appears on 2+ *different* URLs gets flagged
suspected_boilerplate=True in the output instead of being silently
trusted -- still there for you to see, just marked.

Output: trip_report_candidates.csv (each row tagged with route_id so
you can filter to just manali_leh or spiti_circuit candidates)
"""

import re
import csv
import time
import requests

# ---------------------------------------------------------------------------
# Seed URLs -- verified live and fetchable as of this session.
# Add more as you find them (individual rider blogs and team-bhp.com/news/
# articles have been the most reliable so far -- avoid forum sites with
# heavy bot protection, and always sanity-check a domain is still what you
# expect before adding it here, per the BCMTouring lesson above).
# ---------------------------------------------------------------------------

SEED_URLS = [
    # --- Manali-Leh: personal trip reports ---
    ("https://footloosedev.com/manali-leh/", "manali_leh"),
    ("https://www.team-bhp.com/news/recalling-my-journey-manali-leh-bike-35-years-ago", "manali_leh"),
    ("https://www.team-bhp.com/news/hyderabad-ladakh-back-duke-390-epic-7000-km-trip", "manali_leh"),  # multi-state trip -- cumulative day distances given, isolate the Manali->Leh leg manually
    ("https://www.team-bhp.com/news/my-bmw-f850-gs-significant-observations-while-riding-around-ladakh", "manali_leh"),  # fuel-efficiency reference only, no distance total

    # --- Manali-Leh: route-distance reference guides (not trip reports, but explicit sourced km figures) ---
    ("https://rideandfire.in/blog/manali-to-leh-distance-guide-2026", "manali_leh"),
    ("https://cyclingmonks.com/cycling-manali-leh-guide/", "manali_leh"),

    # --- Spiti circuit: personal trip reports ---
    ("https://footloosedev.com/bike-expedition-spiti-valley-itinerary/", "spiti_circuit"),
    ("https://footloosecamps.com/spiti-valley-shimla-to-manali-circuit/", "spiti_circuit"),
    ("https://www.team-bhp.com/news/unforgettable-road-trip-spiti-my-royal-enfield-himalayan-450", "spiti_circuit"),

    # --- Spiti circuit: route-distance reference guides ---
    ("https://trekgo.in/blog/shimla-to-spiti-valley-bike-trip", "spiti_circuit"),
    ("https://wanderon.in/blogs/shimla-to-spiti-valley-bike-trip", "spiti_circuit"),
]

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
}

# A crude signal that a fetched page is NOT what we expect (domain hijack,
# parked-domain redirect, bot-block challenge page). If any of these show
# up prominently, the script warns loudly instead of silently extracting
# garbage candidates from unrelated content.
SUSPICIOUS_MARKERS = [
    "betting", "casino", "cricket betting", "bet now", "odds on",
    "domain is for sale", "buy this domain", "just a moment",
    "checking your browser", "captcha",
]

PATTERNS = [
    ("day_table_row_km_hr", r'\d{2,3}\s*KM\s*\|\s*\d{1,2}\s*Hr', "structured day-table row: distance | time"),
    ("total_distance_labeled", r'total\s+distance[^.\n]{0,30}?\d{2,4}\s*kms?', "explicit 'total distance ... NNN km' phrase"),
    ("distance_mention", r'\d{3,4}\s*kms?\b', "any bare NNN km / NNNN km mention"),
    ("distance_kilometre_word", r'\d{3,4}\s*kilometre', "NNN kilometre(s) mention"),
    ("avg_speed", r'average\s+speed[^.\n]{0,20}?[\d.]+\s*km/?hr', "explicit average speed"),
    ("duration_days", r'\d{1,2}\s*days?\b', "N days mention"),
    ("duration_hours", r'\d{1,2}\s*(?:hrs?|hours?)\b', "N hours mention"),
    ("fuel_tank_litres", r'[\d.]+[\s-]*litres?\s*tank', "fuel tank capacity in litres"),
    ("fuel_litres_generic", r'[\d.]+\s*(?:litres?|ltrs?|L)\b(?:\s*of\s*(?:petrol|fuel))?', "any litres mention"),
    ("mileage_kmpl", r'mileage[^.\n]{0,30}?[\d.]+\s*km', "fuel mileage / efficiency mention"),
]

CONTEXT_WINDOW = 70


def fetch_page_text(url, retries=2, pause_s=1.5):
    """Fetch a URL and return cleaned visible text. Returns None on failure."""
    for attempt in range(retries):
        try:
            resp = requests.get(url, headers=HEADERS, timeout=20)
            if resp.status_code == 200:
                text = strip_html(resp.text)
                return text
            else:
                print(f"    HTTP {resp.status_code} for {url}")
        except requests.RequestException as e:
            print(f"    fetch error ({attempt + 1}/{retries}): {e}")
        time.sleep(pause_s)
    return None


def strip_html(html):
    """Minimal HTML->text: drop script/style blocks and tags, collapse whitespace.
    Pure stdlib/regex -- no bs4 dependency needed."""
    html = re.sub(r'(?is)<(script|style|nav|header|footer)[^>]*>.*?</\1>', ' ', html)
    text = re.sub(r'(?s)<[^>]+>', ' ', html)
    text = re.sub(r'&nbsp;|&amp;|&#\d+;', ' ', text)
    text = re.sub(r'\s+', ' ', text).strip()
    return text


def check_suspicious(url, text):
    lowered = text.lower()
    hits = [marker for marker in SUSPICIOUS_MARKERS if marker in lowered]
    if hits:
        print(f"    ⚠️  WARNING: {url} contains suspicious markers {hits}")
        print(f"        This page may not be what you expect (hijacked domain, bot-block")
        print(f"        page, or ad-parked domain). Verify manually before trusting it.")
        return True
    return False


def extract_candidates(url, route_id, text):
    rows = []
    for label, pattern, description in PATTERNS:
        for m in re.finditer(pattern, text, re.IGNORECASE):
            start = max(0, m.start() - CONTEXT_WINDOW)
            end = min(len(text), m.end() + CONTEXT_WINDOW)
            context = text[start:end].strip()
            rows.append({
                "url": url,
                "route_id": route_id,
                "field_type": label,
                "matched_text": m.group(0),
                "context": context,
                "field_meaning": description,
            })
    return rows


def flag_boilerplate(all_rows):
    """Any 'context' string that appears on 2+ DIFFERENT urls is almost
    certainly shared site furniture (ad widgets, related-article teasers,
    cookie notices) rather than article content -- real trip narratives
    from different authors don't repeat verbatim. Flag, don't drop."""
    context_to_urls = {}
    for row in all_rows:
        context_to_urls.setdefault(row["context"], set()).add(row["url"])

    flagged_count = 0
    for row in all_rows:
        is_boilerplate = len(context_to_urls[row["context"]]) >= 2
        row["suspected_boilerplate"] = is_boilerplate
        if is_boilerplate:
            flagged_count += 1
    return flagged_count


def main():
    all_rows = []
    for url, route_id in SEED_URLS:
        print(f"fetching: {url}  [{route_id}]")
        text = fetch_page_text(url)
        if text is None:
            print("  FAILED -- skipping (may be bot-protected; try opening in browser and pasting text into a local file instead)")
            continue

        if check_suspicious(url, text):
            print("  SKIPPING extraction from this URL due to suspicious content -- verify the domain before re-adding it.")
            continue

        candidates = extract_candidates(url, route_id, text)
        print(f"  {len(candidates)} candidate fields found")
        all_rows.extend(candidates)
        time.sleep(1.0)  # be polite between requests

    if not all_rows:
        print("No candidates extracted from any URL.")
        return

    flagged_count = flag_boilerplate(all_rows)
    if flagged_count:
        print(f"\n{flagged_count} candidate rows matched verbatim across 2+ URLs -- "
              f"flagged suspected_boilerplate=True (likely shared site widgets/ads, not article content)")

    fieldnames = ["url", "route_id", "field_type", "matched_text", "context",
                  "field_meaning", "suspected_boilerplate"]
    with open("trip_report_candidates.csv", "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(all_rows)

    print(f"\nWrote {len(all_rows)} candidate rows to trip_report_candidates.csv")
    print("Next: filter to suspected_boilerplate=False, skim 'context' per url/route_id,")
    print("and manually pull the real total_distance_km / total_time_hr (or days) / fuel_L")
    print("per report into a small labeled_reports.csv -- that's what feeds the residual model.")


if __name__ == "__main__":
    main()