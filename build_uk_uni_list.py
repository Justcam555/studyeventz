#!/usr/bin/env python3
"""
build_uk_uni_list.py — Build a list of UK universities with degree-awarding powers.

Produces data/uk_universities.json with, for each institution:
    name              institution name
    city              city / locality
    website           official website URL
    hesa_provider_id  UKPRN (UK Provider Reference Number)

Source
------
The original brief named the HESA provider register (hesa.ac.uk) as the source.
That site sits behind a Cloudflare "managed challenge" that blocks all automated
access (plain HTTP *and* headless browsers), so it cannot be scraped here.

Instead we query Wikidata's public SPARQL endpoint. The "HESA provider ID" used
on HESA's own provider URLs (e.g. /providers/10007856) is the institution's
UKPRN, which Wikidata stores as property P4971 — so we get the exact same ID
without touching HESA. Filtering to UK items that are universities AND carry a
UKPRN naturally selects real, currently-registered degree-awarding providers
(~150, matching the ~140 target). Every record therefore has a genuine UKPRN.

Usage
-----
    python build_uk_uni_list.py            # build data/uk_universities.json
    python build_uk_uni_list.py --print    # also print the table to stdout
"""

import argparse
import json
import sys
import time
from pathlib import Path

import requests

OUT_PATH = Path(__file__).parent / "data" / "uk_universities.json"

WDQS_ENDPOINT = "https://query.wikidata.org/sparql"
# A descriptive UA is required by the Wikidata query service policy.
HEADERS = {
    "User-Agent": "studyeventz-uk-uni-list/1.0 (university-platform build script)",
    "Accept": "application/sparql-results+json",
}

# UK universities (instances of any subclass of "university", Q3918) located in
# the United Kingdom (Q145) that have a UKPRN (P4971 == the HESA provider ID).
# City: prefer "located in administrative territory" (P131); fall back to
# "headquarters location" (P159). One row per institution via GROUP BY + SAMPLE.
# ?inLondon: true if the institution sits anywhere within Greater London
# (Q23306) — used to collapse the 32 London boroughs to a plain "London".
SPARQL = """
SELECT ?uni ?name
       (SAMPLE(?ukprn)   AS ?id)
       (SAMPLE(?website) AS ?web)
       (SAMPLE(?cityLbl) AS ?city)
       (SAMPLE(?inL)     AS ?inLondon)
WHERE {
  ?uni wdt:P31/wdt:P279* wd:Q3918 ;     # (subclass of) university
       wdt:P17 wd:Q145 ;                 # country = United Kingdom
       wdt:P4971 ?ukprn ;                # UKPRN  (== HESA provider ID)
       rdfs:label ?name . FILTER(LANG(?name) = "en")
  BIND(EXISTS { ?uni wdt:P131* wd:Q23306 } AS ?inL)   # within Greater London?
  OPTIONAL { ?uni wdt:P856 ?website }
  OPTIONAL {
    { ?uni wdt:P131 ?cityItem } UNION { ?uni wdt:P159 ?cityItem }
    ?cityItem rdfs:label ?cityLbl . FILTER(LANG(?cityLbl) = "en")
  }
}
GROUP BY ?uni ?name
ORDER BY ?name
"""

# Administrative wrappers stripped from Wikidata locality labels so the "city"
# field reads as a plain place name (e.g. "City of Plymouth" -> "Plymouth").
_CITY_PREFIXES = (
    "London Borough of ", "Royal Borough of ", "Metropolitan Borough of ",
    "County Borough of ", "City and County of the City of ", "City and County of ",
    "City of ", "Royal Town of ", "Borough of ",
)
_CITY_SUFFIXES = (" District", " (district)")

# Common-name aliases applied after stripping wrappers (formal -> everyday name).
_CITY_ALIASES = {
    "Kingston upon Hull": "Hull",
}

# Per-institution city overrides, keyed by UKPRN, for the handful where Wikidata
# has no location set at all.
_CITY_OVERRIDES = {
    "10003861": "Leeds",   # Leeds Beckett University (no P131/P159 in Wikidata)
}


def normalize_city(city: str | None, in_london: bool) -> str | None:
    """Collapse London boroughs to 'London' and strip administrative wrappers."""
    if in_london:
        return "London"
    if not city:
        return None
    for p in _CITY_PREFIXES:
        if city.startswith(p):
            city = city[len(p):]
    for s in _CITY_SUFFIXES:
        if city.endswith(s):
            city = city[: -len(s)]
    city = city.strip()
    return _CITY_ALIASES.get(city, city) or None


def run_query(retries: int = 4, backoff: float = 3.0) -> list[dict]:
    """Run the SPARQL query with simple retry/backoff; return raw bindings."""
    last_err = None
    for attempt in range(1, retries + 1):
        try:
            resp = requests.get(
                WDQS_ENDPOINT,
                params={"query": SPARQL, "format": "json"},
                headers=HEADERS,
                timeout=90,
            )
            resp.raise_for_status()
            return resp.json()["results"]["bindings"]
        except Exception as e:  # noqa: BLE001 — surface any network/parse issue
            last_err = e
            wait = backoff * attempt
            print(f"  query attempt {attempt}/{retries} failed: {e} — retrying in {wait:.0f}s",
                  file=sys.stderr)
            time.sleep(wait)
    raise SystemExit(f"Wikidata query failed after {retries} attempts: {last_err}")


def val(binding: dict, key: str):
    """Pull a value out of a SPARQL binding, or None if absent/empty."""
    cell = binding.get(key)
    if not cell:
        return None
    v = cell.get("value", "").strip()
    return v or None


def build() -> list[dict]:
    bindings = run_query()
    universities = []
    for b in bindings:
        ukprn = val(b, "id")
        name = val(b, "name")
        if not ukprn or not name:
            continue  # both are required for a usable record
        in_london = (val(b, "inLondon") or "").lower() == "true"
        city = _CITY_OVERRIDES.get(ukprn) or normalize_city(val(b, "city"), in_london)
        universities.append({
            "name": name,
            "city": city,
            "website": val(b, "web"),
            "hesa_provider_id": ukprn,            # UKPRN — HESA's own provider ID
            "wikidata_id": val(b, "uni").rsplit("/", 1)[-1] if val(b, "uni") else None,
        })
    # De-dupe defensively on UKPRN (one institution = one provider ID) and sort.
    by_id = {u["hesa_provider_id"]: u for u in universities}
    return sorted(by_id.values(), key=lambda u: u["name"].lower())


def main() -> None:
    ap = argparse.ArgumentParser(description="Build data/uk_universities.json from Wikidata (UKPRN = HESA provider ID).")
    ap.add_argument("--print", dest="show", action="store_true", help="print the resulting table to stdout")
    ap.add_argument("--out", type=Path, default=OUT_PATH, help=f"output path (default: {OUT_PATH})")
    args = ap.parse_args()

    print("Querying Wikidata for UK universities with a UKPRN (HESA provider ID)...")
    unis = build()

    payload = {
        "source": "Wikidata SPARQL (query.wikidata.org); UKPRN (P4971) == HESA provider ID",
        "note": "HESA register itself is Cloudflare-protected; UKPRN is the same ID HESA uses on its provider pages.",
        "definition": "UK items that are (subclasses of) university with a UKPRN — i.e. registered degree-awarding providers.",
        "count": len(unis),
        "universities": unis,
    }

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

    missing_city = sum(1 for u in unis if not u["city"])
    missing_web = sum(1 for u in unis if not u["website"])
    print(f"\nWrote {len(unis)} universities -> {args.out}")
    print(f"  all have a name + HESA provider ID (UKPRN)")
    print(f"  missing city: {missing_city}   missing website: {missing_web}")

    if args.show:
        print()
        for u in unis:
            print(f"  {u['hesa_provider_id']:<10} {u['name'][:42]:42} {(u['city'] or '-')[:20]:20} {u['website'] or '-'}")


if __name__ == "__main__":
    main()
