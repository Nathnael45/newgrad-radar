#!/usr/bin/env python3
"""Interactive setup survey. Writes profile.json, which every other script reads.

  python3 survey.py             # answer the questions (press Enter to accept a default)
  python3 survey.py --show      # print the current profile
"""
import datetime as dt
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
PROFILE = os.path.join(HERE, "profile.json")
REGIONS = json.load(open(os.path.join(HERE, "data", "regions.json")))
SOURCES = json.load(open(os.path.join(HERE, "data", "sources.json")))

ROLE_FAMILIES = {
    "backend": "Backend / APIs / distributed systems",
    "fullstack": "Full-stack / product engineering",
    "frontend": "Frontend / web UI",
    "infra": "Infrastructure / platform / cloud / DevOps / SRE",
    "data": "Data engineering",
    "ml": "ML / AI engineering",
    "mobile": "Mobile (iOS / Android)",
    "security": "Security engineering",
    "embedded": "Embedded / firmware / systems-level",
    "quant": "Quant / trading systems developer",
    "testing": "QA / test automation / SDET",
    "fde": "Forward-deployed / solutions-style engineering",
    "robotics": "Robotics / simulation / autonomy",
}


def ask(prompt, default=None, cast=str, allow_empty=False):
    hint = f" [{default}]" if default not in (None, "") else ""
    while True:
        raw = input(f"{prompt}{hint}: ").strip()
        if not raw:
            if default is not None:
                return default
            if allow_empty:
                return ""
            continue
        try:
            return cast(raw)
        except ValueError:
            print("  Please try again.")


def ask_yes(prompt, default=True):
    d = "Y/n" if default else "y/N"
    raw = input(f"{prompt} ({d}): ").strip().lower()
    return default if not raw else raw.startswith("y")


def pick_many(prompt, options, default=()):
    """options: dict key -> label. Returns list of keys, in the order the person typed them."""
    keys = list(options)
    for i, k in enumerate(keys, 1):
        print(f"  {i:>2}. {options[k]}")
    dflt = ",".join(str(keys.index(k) + 1) for k in default if k in keys)
    while True:
        raw = input(f"{prompt} (comma-separated numbers){' [' + dflt + ']' if dflt else ''}: ").strip() or dflt
        if not raw:
            return []
        try:
            picked = [keys[int(x) - 1] for x in raw.replace(" ", "").split(",") if x]
            return list(dict.fromkeys(picked))
        except (ValueError, IndexError):
            print("  Use the numbers from the list, e.g. 1,3,4")


def run_survey(existing):
    print("\nnewgrad-radar setup. Press Enter to keep the value in [brackets].\n")
    p = {}

    print("— You —")
    today = dt.date.today()
    p["grad_year"] = ask("Graduation year", existing.get("grad_year", today.year + (1 if today.month > 6 else 0)), int)
    p["grad_month"] = ask("Graduation month (1-12)", existing.get("grad_month", 5), int)
    p["degree"] = ask("Degree you'll have when you start (bachelors / masters / phd)", existing.get("degree", "bachelors")).lower()
    p["internships_count_as_experience"] = ask_yes(
        "Include roles asking for ~1 year of experience if internships might count?", existing.get("internships_count_as_experience", True))

    print("\n— Roles —")
    p["primary_roles"] = pick_many("Roles you want MOST", ROLE_FAMILIES, existing.get("primary_roles", ["backend", "fullstack"]))
    rest = {k: v for k, v in ROLE_FAMILIES.items() if k not in p["primary_roles"]}
    p["ok_roles"] = pick_many("Roles that are fine but lower priority", rest, existing.get("ok_roles", ["infra", "data", "ml"]))
    rest = {k: v for k, v in rest.items() if k not in p["ok_roles"]}
    p["never_roles"] = pick_many("Roles to never show", rest, existing.get("never_roles", ["embedded", "testing"]))
    p["skills"] = [s.strip().lower() for s in ask(
        "Your main skills/languages (comma-separated, used to explain fit)",
        ", ".join(existing.get("skills", ["python", "java", "typescript", "react", "sql", "aws"]))).split(",") if s.strip()]

    print("\n— Locations —")
    region_labels = {k: v["label"] for k, v in REGIONS.items()}
    p["main_regions"] = pick_many("Your MAIN areas (include Remote if you want remote roles)", region_labels,
                                  existing.get("main_regions", ["NYC", "SF Bay", "Remote"]))
    rest = {k: v for k, v in region_labels.items() if k not in p["main_regions"]}
    p["secondary_regions"] = pick_many("Areas that are OK but lower priority (Enter for none)", rest, existing.get("secondary_regions", []))
    print("  Anywhere else in the US:")
    print("    1. never   2. only top-tier companies (FAANG+, quant, well-known tech)   3. any company")
    pol = ask("  Choose 1-3", {"never": "1", "top_tier": "2", "any": "3"}.get(existing.get("elsewhere", "top_tier"), "2"))
    p["elsewhere"] = {"1": "never", "2": "top_tier", "3": "any"}.get(pol, "top_tier")

    print("\n— Pay —")
    p["pay_floor"] = ask("Minimum base salary (USD/year, 0 for no floor)", existing.get("pay_floor", 100000), int)
    p["region_pay_floors"] = {}
    if ask_yes("Different minimum for any specific area (e.g. lower where you can live at home)?", bool(existing.get("region_pay_floors"))):
        for r in p["main_regions"] + p["secondary_regions"]:
            v = ask(f"  Minimum for {r} (Enter = {p['pay_floor']})", existing.get("region_pay_floors", {}).get(r, ""), allow_empty=True)
            if str(v).strip():
                p["region_pay_floors"][r] = int(v)
    p["keep_unlisted_pay"] = ask_yes("Keep jobs that don't list pay?", existing.get("keep_unlisted_pay", True))

    print("\n— Work authorization & clearance —")
    p["needs_sponsorship"] = ask_yes("Will you need visa sponsorship (now or later, e.g. OPT/H-1B)?", existing.get("needs_sponsorship", False))
    p["us_citizen"] = False if p["needs_sponsorship"] else ask_yes("Are you a US citizen? (some roles require it)", existing.get("us_citizen", True))
    print("  Security clearance:  1. I have an active clearance   2. Willing to obtain one   3. Not interested")
    c = ask("  Choose 1-3", {"active": "1", "willing": "2", "no": "3"}.get(existing.get("clearance", "willing"), "2"))
    p["clearance"] = {"1": "active", "2": "willing", "3": "no"}.get(c, "willing")
    if not p["us_citizen"] and p["clearance"] != "no":
        print("  (Most clearances require US citizenship; setting clearance to 'not interested'.)")
        p["clearance"] = "no"

    print("\n— Freshness & sources —")
    p["max_age_days"] = ask("Only show jobs posted within the last N days", existing.get("max_age_days", 7), int)
    src_labels = {k: v["description"] for k, v in SOURCES.items()}
    p["sources"] = pick_many("Sources to use", src_labels,
                             existing.get("sources", [k for k, v in SOURCES.items() if v.get("default")]))
    if "jobright" in p["sources"]:
        print("  Note: jobright pages are read slowly (one every few seconds) and the run backs off if the site asks it to.")

    print("\n— Output —")
    print("  New matches always go to output/jobs.csv and output/LATEST.md.")
    notion = existing.get("notion", {})
    p["notion"] = {"enabled": ask_yes("Also send matches to a Notion database?", notion.get("enabled", False))}
    if p["notion"]["enabled"]:
        print("  1) Create an internal integration at https://www.notion.so/my-integrations and copy its secret.")
        print("  2) export NOTION_TOKEN=<secret>   (keep it out of profile.json and out of git)")
        print("  3) Share a Notion page with the integration, then run:  python3 radar.py notion-setup <page-url-or-id>")
        p["notion"]["database_id"] = ask("  Existing database id (Enter if you'll run notion-setup)", notion.get("database_id", ""), allow_empty=True)
    return p


def main():
    existing = json.load(open(PROFILE)) if os.path.exists(PROFILE) else {}
    if "--show" in sys.argv:
        print(json.dumps(existing, indent=2) if existing else "No profile yet. Run: python3 survey.py")
        return
    profile = run_survey(existing)
    json.dump(profile, open(PROFILE, "w"), indent=2)
    print(f"\nSaved {PROFILE}. Next: python3 radar.py run")


if __name__ == "__main__":
    try:
        main()
    except (KeyboardInterrupt, EOFError):
        print("\nCancelled; nothing saved.")
