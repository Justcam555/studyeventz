#!/usr/bin/env python3
"""
Agent name normalisation.
Adds a `canonical_name` column to the agents table and populates it using:
  1. Brand-specific rules (IDP, AECC, StudyCo, iae, etc.)
  2. Country-suffix stripping for global networks
  3. Legal-suffix stripping (Pty Ltd, Co. Ltd, Inc, …)
  4. Whitespace / punctuation cleanup

Original `company_name` is never modified.

Usage:
    python3 normalise_agents.py          # apply normalisation
    python3 normalise_agents.py --report # show before/after stats, no changes
"""

import argparse
import re
import sqlite3
from pathlib import Path
from collections import Counter

DB_PATH = Path(__file__).parent / "data" / "agents.db"

# ─── Brand rules  (checked in order, first match wins) ───────────────────────
# Each entry: (regex_pattern, canonical_name)
# Use None as canonical_name to fall through to generic cleaning.

# BRAND_RULES: (regex, canonical_name, parent_company)
# parent_company = None means same as canonical_name
BRAND_RULES = [
    # IDP — global listed company (ASX: IEL)
    (r"\bIDP\b",
        "IDP Education",                        "IDP Education Ltd"),
    # Hotcourses is an IDP subsidiary
    (r"\bHotcourses\b",
        "IDP Education",                        "IDP Education Ltd"),
    # AECC Global
    (r"\bAECC\b|Australia Education Victoria.*AECC|AECC.*Unit Trust",
        "AECC Global",                          "AECC Global"),
    # AAET / StudyCo — same network
    (r"\bAAET\b.*\bStudyCo\b|\bAAET-StudyCo\b|\bAAET\b",
        "AAET-StudyCo",                         "StudyCo Education Group"),
    (r"\bStudyCo\b",
        "AAET-StudyCo",                         "StudyCo Education Group"),
    # iae GLOBAL
    (r"\biae\s*GLOBAL\b|\bIAE\s*Global\b|\biae\s*global\b",
        "iae GLOBAL",                           "iae GLOBAL"),
    # Adventus
    (r"^Adventus",
        "Adventus Education",                   "Adventus Education"),
    # Global Study Partners
    (r"Global Study Partners",
        "Global Study Partners",                "Global Study Partners"),
    # Global Reach
    (r"^Global Reach\b",
        "Global Reach",                         "Global Reach"),
    # SI-UK
    (r"\bSI-UK\b|\bSIUK\b",
        "SI-UK",                                "SI-UK"),
    # Edwise
    (r"\bEdwise\b",
        "Edwise International",                 "Edwise International"),
    # Meridean
    (r"\bMeridean\b",
        "Meridean Overseas",                    "Meridean Overseas"),
    # KC Overseas
    (r"\bKC Overseas\b|\bKCO\b",
        "KC Overseas Education",                "KC Overseas Education"),
    # SIEC
    (r"\bSIEC\b",
        "SIEC Education",                       "SIEC Education"),
    # KIEC
    (r"\bKIEC\b",
        "KIEC Education",                       "KIEC Education"),
    # EduLink One
    (r"\bEduLink One\b|\bEdulink One\b",
        "EduLink One",                          "EduLink One"),
    # Oz Admissions
    (r"\bOz Admissions\b|\bOzAdmissions\b",
        "Oz Admissions",                        "Oz Admissions"),
    # A+ Capec
    (r"\bA\+\s*Capec\b",
        "A+ Capec",                             "A+ Capec"),
    # Hands On Education — covers "Hands On", "HandsOn", BEO/Education Tower variants
    (r"\bHands[\s-]*On\b|Education Tower.*Hands|BEO.*Hands\s*On",
        "Hands On Education",                   "Hands On Education Consultants"),
    # One Education
    (r"^One Education",
        "One Education Consulting",             "One Education Consulting Co., Ltd"),
    # Education For Life
    (r"^Education For Life|^Education for Life",
        "Education For Life",                   "Education For Life Co Ltd"),
    # SOL Edu
    (r"^SOL Edu\b",
        "SOL Edu",                              "SOL Edu Pty Ltd"),
    # Stellar Education
    (r"^Stellar Education",
        "Stellar Education",                    "Stellar Education & Visa Centre"),
    # StudyIn (gostudyin.com network)
    (r"^StudyIn\b",
        "StudyIn",                              "StudyIn"),
    # WIN Education — legal name "WIN EDUCATION SERVICE CO. LTD." and variants
    (r"\bWIN\s+EDUCATION\b|\bWIN\s+Education\b",
        "WIN Education",                        "WIN Education"),
    # AVSS — covers "Australian Visa and Student Services" and branch variants with "&"
    (r"Australian Visa.*(and|&).*Student Services|^AVSS\b",
        "AVSS",                                 "Australian Visa and Student Services"),
    # Yes Education Group — global network across Thailand, Vietnam, Nepal, Indonesia, Cambodia
    # Broadened from "^Yes Education Group" to also catch "Yes Education Cambodia" etc.
    # Canonical kept as "Yes Education Group(Bangkok)" for backward compat with agent_social
    (r"^Yes Education",
        "Yes Education Group(Bangkok)",         "Yes Education Group"),
    # Expert Education & Visa Services / Expert Group Holdings (holding company used across markets)
    # Catches: city-branch variants in Nepal, "Expert Group Holdings Pty Ltd" standalone in
    # Nepal/Sri Lanka/Vietnam, and trading-as entries
    (r"Expert Education.*Visa|Expert Group Holdings",
        "Expert Education - EEVS Thailand",     "Expert Group Holdings Pty Ltd"),
    # BridgeBlue / AMS BridgeBlue — global network, country/city suffixes vary widely
    (r"\bBridgeBlue\b",
        "BridgeBlue",                           "BridgeBlue"),
    # AUG / AusEd-UniEd Group — appears under AUG, AusEd-UniEd, Aused-Unied spellings
    (r"\bAUG\b|AusEd.{0,5}UniEd|Aused.{0,5}Unied",
        "AUG",                                  "AUG (AusEd-UniEd Group)"),
    # SUN Education Group — major Indonesian network with 15+ city branches
    (r"\bSUN\s+Education\b|\bSun\s+Education\s+Group\b",
        "SUN Education Group",                  "SUN Education Group"),
    # ICAN Education — Indonesian network with PT legal name variants and city branches
    (r"\bICAN\s+Education\b|\bICAN\s+EDUCATION\b",
        "ICAN Education",                       "ICAN Education"),
    # JACK Study Abroad — "StudyAbroad" (one word) in Indonesia, "Study Abroad" in Vietnam
    (r"\bJACK\s+Study\s*Abroad\b|\bJack\s+Study\s*Abroad\b",
        "JACK Study Abroad",                    "JACK Study Abroad"),
    # PFEC Global — Sri Lanka branches and city variants
    (r"^PFEC Global",
        "PFEC Global",                          "PFEC Global"),
    # Planet Education — Nepal ("Planet Education - Nepal", "Planet Education LLP") and Sri Lanka
    (r"^Planet Education",
        "Planet Education",                     "Planet Education"),
    # Jeewa — Sri Lanka (Jeewa Education + Jeewa Australian Educational Centre branches)
    (r"^Jeewa|^JEEWA",
        "Jeewa Education",                      "Jeewa Education"),
    # PAC Asia — "PAC Asia Services" (Nepal), "PAC Asia Study abroad" (Sri Lanka),
    # "PAC Asia Eduserve LLP" (Nepal/Sri Lanka)
    (r"^PAC Asia",
        "PAC Asia",                             "PAC Asia Services"),
    # Bada Global — city-branch variants in Vietnam and Indonesia
    (r"^Bada Global",
        "Bada Global",                          "Bada Global Pty Ltd"),
    # Fortrust — "Fortrust International Pte Ltd" and "PT. Indogro... (Fortrust Education Services)"
    (r"\bFortrust\b",
        "Fortrust Education",                   "Fortrust Education"),
    # StudyLink — "StudyLink Company Limited" and "Studylink" (Vietnam)
    (r"^StudyLink\b|^Studylink\b",
        "StudyLink",                            "StudyLink"),
    # Eduyoung / Edu Young (Thailand)
    (r"Edu\s*Young\.?Com|EduYoung",
        "Eduyoung.Com - Thailand",              "Eduyoung.Com - Thailand"),
    # Beyond Study Center (without "Co" suffix)
    (r"^Beyond Study Center\b",
        "Beyond Study Center Co",               "Beyond Study Center Co"),
    # EDNET CO — branches (HO suffix etc.)
    (r"^EDNET\b",
        "EDNET CO",                             "EDNET CO"),
    # Further Education Company
    (r"^FURTHER EDUCATION COMPANY$|^Further Education\b",
        "Further Education",                    "Further Education"),
    # Imagine Global Edu / iGEM Bangkok
    (r"Imagine Global Edu",
        "Imagine Global Edu and Migration- iGEM (Bangkok)", "Imagine Global Edu and Migration- iGEM (Bangkok)"),
    # LCI Group / Liu Cheng International Group
    (r"\bLCI\s*Group\b|Liu Cheng International",
        "Liu Cheng International Group",        "Liu Cheng International Group"),
    # Asiania International Consulting (with trading-as suffix)
    (r"^Asiania International Consulting",
        "Asiania International Consulting",     "Asiania International Consulting"),
    # OEC Global Education — city-branch variants
    (r"OEC Global Education",
        "Oec Global Education",                 "Oec Global Education"),
    # Edvoy — covers "Edvoy Educational Services Ltd/Limited", "IEC Abroad" (former name), country branches
    (r"\bEdvoy\b|IEC Abroad",
        "Edvoy",                                "Edvoy"),
    # BRIT Education UK — mixed caps/hyphens/dashes
    (r"\bBRIT[\s\-–]*Education\s*UK\b|\bBrit[\s\-–]*Education\s*UK\b",
        "BRIT Education UK",                    "BRIT Education UK"),
    # GoUni — "Go Uni" / "Gouni Co. Ltd"
    (r"\bGo\s*Uni\b|\bGoUni\b|\bGouni\b",
        "GoUni",                                "GoUni"),
    # Index Education Services (Sower Education Group)
    (r"\bIndex Education Services\b",
        "Index Education Services",             "Sower Education Group"),
    # IBEC — Indonesia-Britain Education Centre, various punctuation
    (r"\bIBEC\b",
        "IBEC",                                 "IBEC"),
]

# ─── Legal-suffix patterns to strip for generic cleaning ─────────────────────
LEGAL_SUFFIXES = re.compile(
    r"""[\s,\-]*(
        pty\.?\s*ltd\.?|
        pvt\.?\s*ltd\.?|
        co\.?\s*ltd\.?|
        co\.,?\s*limited|
        limited|
        ltd\.?|
        llc\.?|
        inc\.?|
        s\.?a\.?s?\.?|
        gmbh|
        as\s+trustee\s+for.*|
        unit\s+trust.*|
        \(registered\)
    )\s*$""",
    re.IGNORECASE | re.VERBOSE,
)

# ─── Country / branch suffixes to keep for global networks ───────────────────
# For companies whose canonical brand already includes the country info:
STRIP_COUNTRY_BRANDS = {
    "AAET-StudyCo", "iae GLOBAL", "Global Study Partners",
    "Global Reach", "IDP Education", "AECC Global",
    "SI-UK", "Edwise International", "Meridean Overseas",
    "KC Overseas Education",
}

COUNTRY_SUFFIX = re.compile(
    r"""[\s\-–—]+
        [\(\[]?
        (
            bangladesh|india|pakistan|nepal|sri\s*lanka|vietnam|indonesia|
            malaysia|philippines|thailand|myanmar|cambodia|laos|china|hong\s*kong|
            taiwan|korea|japan|singapore|uae|oman|kuwait|saudi|ksa|jordan|
            lebanon|turkey|egypt|africa|ghana|nigeria|kenya|ethiopia|zimbabwe|
            brazil|colombia|peru|chile|mexico|uk|australia|head\s*office|
            australia\s*head\s*office|melb|melbourne
        )
        [\)\]]?
        \s*$""",
    re.IGNORECASE | re.VERBOSE,
)


def normalise(raw: str) -> tuple:
    """Returns (canonical_name, parent_company)."""
    if not raw or raw.strip() in ("Unknown", "", "N/A"):
        return raw, None

    name = raw.strip()

    # 0. Strip Warwick link-icon text artifact
    name = re.sub(r"\s*Link opens in a new window\s*", "", name).strip()

    # 1. Brand rules
    for pattern, canonical, parent in BRAND_RULES:
        if re.search(pattern, name, re.IGNORECASE):
            return canonical, (parent or canonical)

    # 2. Generic cleaning
    cleaned = LEGAL_SUFFIXES.sub("", name).strip().rstrip(".,;:")
    cleaned = re.sub(r"\s+", " ", cleaned).strip().rstrip(".,;:-")
    cleaned = cleaned if cleaned else raw.strip()

    return cleaned, cleaned


# ─── Apply to database ────────────────────────────────────────────────────────

def apply_normalisation(dry_run=False):
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row

    # Add columns if missing
    cols = [r[1] for r in conn.execute("PRAGMA table_info(agents)").fetchall()]
    if "canonical_name" not in cols:
        conn.execute("ALTER TABLE agents ADD COLUMN canonical_name TEXT")
        print("✓  Added canonical_name column")
    if "parent_company" not in cols:
        conn.execute("ALTER TABLE agents ADD COLUMN parent_company TEXT")
        print("✓  Added parent_company column")
    conn.commit()

    rows = conn.execute(
        "SELECT id, company_name FROM agents WHERE company_name IS NOT NULL"
    ).fetchall()

    changes = 0
    change_samples = []
    canonical_counts = Counter()

    updates = []
    for row in rows:
        original = row["company_name"].strip()
        canonical, parent = normalise(original)
        canonical_counts[canonical] += 1
        if canonical != original:
            changes += 1
            if len(change_samples) < 20:
                change_samples.append((original, canonical, parent))
        updates.append((canonical, parent, row["id"]))

    if not dry_run:
        conn.executemany(
            "UPDATE agents SET canonical_name=?, parent_company=? WHERE id=?", updates
        )
        conn.commit()
        print(f"✓  Normalised {changes:,} of {len(rows):,} agent records")
    else:
        print(f"DRY RUN — would normalise {changes:,} of {len(rows):,} records")

    # ── Report ────────────────────────────────────────────────────────────────
    print(f"\nSample changes ({min(len(change_samples),20)}):")
    print(f"  {'Original':<50} → {'Canonical':<30} Parent")
    print("  " + "─" * 95)
    for orig, can, par in change_samples:
        print(f"  {orig[:49]:<50} → {can:<30} {par or ''}")

    print(f"\nTop 20 canonical names by frequency:")
    for name, cnt in canonical_counts.most_common(20):
        print(f"  {cnt:>5}  {name}")

    conn.close()


def report():
    """Show current state of canonical names."""
    conn = sqlite3.connect(DB_PATH)
    cols = [r[1] for r in conn.execute("PRAGMA table_info(agents)").fetchall()]
    if "canonical_name" not in cols:
        print("canonical_name column doesn't exist yet. Run without --report first.")
        conn.close()
        return

    rows = conn.execute("""
        SELECT canonical_name, COUNT(*) as c, COUNT(DISTINCT university_id) as unis
        FROM agents
        WHERE canonical_name IS NOT NULL
        GROUP BY canonical_name
        ORDER BY unis DESC, c DESC
        LIMIT 40
    """).fetchall()

    print(f"\n{'Canonical Name':<50} {'Unis':>5} {'Records':>8}")
    print("─" * 68)
    for r in rows:
        print(f"  {r[0][:48]:<50} {r[1]:>5} {r[2]:>8}")

    conn.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--report", action="store_true", help="Show stats only, no changes")
    parser.add_argument("--dry-run", action="store_true", help="Show what would change, no writes")
    args = parser.parse_args()

    if args.report:
        report()
    else:
        apply_normalisation(dry_run=args.dry_run)
