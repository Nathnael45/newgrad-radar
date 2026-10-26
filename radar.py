#!/usr/bin/env python3
"""newgrad-radar: find fresh new-grad software jobs that match your profile.

  python3 survey.py                      # one-time: answer questions -> profile.json
  python3 radar.py run                   # collect + auto-review + publish (CSV, Markdown, optional Notion)

Step by step (e.g. when an AI agent does the review, see AGENT_RUNBOOK.md):
  python3 radar.py collect               # scrape sources, filter, read job descriptions -> state/candidates.json
  python3 radar.py show [START END]      # print candidates compactly
  python3 radar.py review                # rule-based review -> state/review/auto-<timestamp>.json
  python3 radar.py publish DECISIONS     # write outputs for a decisions file (.json from review, or .txt from an agent)
  python3 radar.py notion-setup PAGE     # create the Notion database under a page shared with your integration
  python3 radar.py stats

Standard library only; Python 3.9+.
"""
import concurrent.futures as cf
import csv
import datetime as dt
import html
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(HERE, "data")
STATE = os.path.join(HERE, "state")
OUTPUT = os.path.join(HERE, "output")
PROFILE_PATH = os.path.join(HERE, "profile.json")
SEEN_PATH = os.path.join(STATE, "seen.json")
CANDIDATES_PATH = os.path.join(STATE, "candidates.json")
REJECTED_PATH = os.path.join(STATE, "rejected.csv")

REGIONS = json.load(open(os.path.join(DATA, "regions.json")))
COMPANIES = json.load(open(os.path.join(DATA, "companies.json")))
SOURCE_DEFS = json.load(open(os.path.join(DATA, "sources.json")))
TODAY = dt.date.today()
UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0 Safari/537.36"
JOBRIGHT_DELAY_S, JOBRIGHT_MAX_PER_RUN = 8, 20
SHORTLINK_HOSTS = {"go.vansh.nyc", "bit.ly", "tinyurl.com", "lnkd.in"}


def load_profile():
    if not os.path.exists(PROFILE_PATH):
        sys.exit("No profile.json yet. Run: python3 survey.py")
    p = json.load(open(PROFILE_PATH))
    p.setdefault("secondary_regions", [])
    p.setdefault("region_pay_floors", {})
    p.setdefault("never_roles", [])
    p.setdefault("ok_roles", [])
    p.setdefault("skills", [])
    p.setdefault("max_age_days", 7)
    p.setdefault("elsewhere", "top_tier")
    p.setdefault("sources", [k for k, v in SOURCE_DEFS.items() if v.get("default")])
    return p


PROFILE = None  # set in main()


# ---------------------------------------------------------------- http + text helpers

def get(url, as_json=False, timeout=20):
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/json, text/html;q=0.9, */*;q=0.8"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        body = r.read().decode("utf-8", "replace")
    return json.loads(body) if as_json else body


def strip_html(s):
    s = html.unescape(s or "")
    s = re.sub(r"(?is)<(script|style)[^>]*>.*?</\1>", " ", s)
    s = re.sub(r"(?i)<br\s*/?>|</(p|li|div|h\d|tr)>", "\n", s)
    s = re.sub(r"<[^>]+>", " ", s)
    s = html.unescape(s)
    s = re.sub(r"[ \t\r\f\v]+", " ", s)
    return re.sub(r"\n\s*\n+", "\n", s).strip()


def canon_url(u):
    p = urllib.parse.urlsplit(u.strip())
    q = [(k, v) for k, v in urllib.parse.parse_qsl(p.query)
         if not k.lower().startswith("utm_") and k.lower() not in ("ref", "source", "gh_src", "lever-source")]
    host = p.netloc.lower().replace("boards.greenhouse.io", "job-boards.greenhouse.io")
    return urllib.parse.urlunsplit((p.scheme or "https", host, p.path.rstrip("/"), urllib.parse.urlencode(q), ""))


def parse_qs(q):
    return {k: v[0] for k, v in urllib.parse.parse_qs(q).items()}


MD_LINK = re.compile(r"\[([^\]]*)\]\((https?://[^)\s]+)\)")


def md_text(cell):
    return strip_html(MD_LINK.sub(r"\1", cell).replace("**", "")).strip()


def posted_to_age(posted):
    """'Oct 22' -> '3d' relative to today (dates in the future roll back a year)."""
    try:
        d = dt.datetime.strptime(f"{posted.strip()} {TODAY.year}", "%b %d %Y").date()
    except ValueError:
        return ""
    if d > TODAY:
        d = d.replace(year=d.year - 1)
    return f"{(TODAY - d).days}d"


def age_days(age):
    """'0d' -> 0, '13d' -> 13, '2mo' -> 60, '1w' -> 7; None if unknown."""
    m = re.match(r"\s*(\d+)\s*(d|w|mo|m|y)", (age or "").lower())
    return int(m.group(1)) * {"d": 1, "w": 7, "mo": 30, "m": 30, "y": 365}[m.group(2)] if m else None


# ---------------------------------------------------------------- source parsers

def parse_speedyapply(md):
    jobs = []
    for section, tag in (("FAANG+", "TABLE_FAANG"), ("Quant", "TABLE_QUANT"), ("Other", "TABLE")):
        m = re.search(rf"<!-- {tag}_START -->(.*?)<!-- {tag}_END -->", md, re.S)
        if not m:
            continue
        header = None
        for line in m.group(1).splitlines():
            if not line.startswith("|"):
                continue
            cells = [c.strip() for c in line.strip().strip("|").split("|")]
            if header is None:
                header = [c.lower() for c in cells]
                continue
            if set(cells[0]) <= set("-: ") or len(cells) != len(header):
                continue
            row = dict(zip(header, cells))
            links = re.findall(r'href="([^"]+)"', row.get("posting", ""))
            if links:
                jobs.append({"source": "speedyapply", "tier": section, "company": strip_html(row.get("company", "")),
                             "title": strip_html(row.get("position", "")), "location": strip_html(row.get("location", "")),
                             "salary_text": strip_html(row.get("salary", "")), "url": links[0], "age": strip_html(row.get("age", ""))})
    return jobs


def parse_simplify(md):
    start = md.find("## 💻 Software Engineering")
    if start < 0:
        return []
    body = md[start:]
    end = min([i for i in (body.find("🗃️ Inactive"), body.find("\n## ", 5)) if i > 0] or [len(body)])
    jobs, last_company = [], ""
    for row in re.findall(r"<tr>(.*?)</tr>", body[:end], re.S):
        tds = re.findall(r"<td>(.*?)</td>", row, re.S)
        if len(tds) < 5 or "🔒" in row:
            continue
        company = strip_html(tds[0]).replace("🔥", "").strip()
        company = last_company if company == "↳" else company
        last_company = company
        title = strip_html(tds[1])
        loc_raw = re.sub(r"<summary>.*?</summary>", "", tds[2], flags=re.S)
        location = "; ".join(x for x in (strip_html(p) for p in re.split(r"</br>|<br\s*/?>", loc_raw)) if x)
        links = [l for l in re.findall(r'href="([^"]+)"', tds[3]) if "simplify.jobs/p/" not in l]
        if links:
            flags = ["advanced degree"] if "🎓" in title else []
            jobs.append({"source": "simplify", "tier": "", "company": company, "title": re.sub(r"[🎓🛂🇺🇸🔥]", "", title).strip(),
                         "location": location, "salary_text": "", "url": links[0], "age": strip_html(tds[4]), "flags": flags})
    return jobs


def parse_md_table(md, source, cols):
    jobs, header, last_company = [], None, ""
    for line in md.splitlines():
        if not line.startswith("|"):
            header = None if not line.strip() else header
            continue
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if header is None:
            header = [c.lower() for c in cells]
            continue
        if set(cells[0]) <= set("-: ") or len(cells) != len(header) or cols["company"] not in header or "🔒" in line:
            continue
        row = dict(zip(header, cells))
        company = md_text(row[cols["company"]])
        company = last_company if company in ("↳", "") else company
        last_company = company
        cell = row.get(cols["link"], "")
        links = [u for u in re.findall(r'href="([^"]+)"', cell) + [m.group(2) for m in MD_LINK.finditer(cell)] if "simplify.jobs/p/" not in u]
        if not links:
            continue
        location = "; ".join(x for x in (strip_html(p) for p in re.split(r"</br>|<br\s*/?>", row.get(cols["location"], ""))) if x)
        if cols.get("work_model") and "remote" in row.get(cols["work_model"], "").lower():
            location += "; Remote"
        jobs.append({"source": source, "tier": "", "company": company, "title": re.sub(r"[🇺🇸🛂🎓🔥]", "", md_text(row[cols["title"]])).strip(),
                     "location": location, "salary_text": "", "url": links[0],
                     "age": posted_to_age(md_text(row.get(cols["posted"], ""))), "flags": []})
    return jobs


def get_retry(url, tries=3, **kw):
    for n in range(tries):
        try:
            return get(url, timeout=30, **kw)
        except (urllib.error.URLError, TimeoutError) as e:
            if isinstance(e, urllib.error.HTTPError) and e.code < 500 or n == tries - 1:
                raise
            time.sleep(2 * (n + 1))


def fetch_company_boards():
    want = re.compile(COMPANIES["watchlist_title_regex"], re.I)

    def board_jobs(w):
        out = []
        try:
            if w["ats"] == "greenhouse":
                for j in get_retry(f"https://boards-api.greenhouse.io/v1/boards/{w['board']}/jobs", as_json=True).get("jobs", []):
                    out.append((j["title"], (j.get("location") or {}).get("name", "").replace(" • ", "; "),
                                f"https://job-boards.greenhouse.io/{w['board']}/jobs/{j['id']}", j.get("first_published") or j.get("updated_at")))
            elif w["ats"] == "lever":
                for j in get_retry(f"https://api.lever.co/v0/postings/{w['board']}?mode=json", as_json=True):
                    cat = j.get("categories") or {}
                    loc = "; ".join(cat.get("allLocations") or [cat.get("location", "")]) + ("; Remote" if j.get("workplaceType") == "remote" else "")
                    out.append((j["text"], loc, j["hostedUrl"], dt.datetime.fromtimestamp(j["createdAt"] / 1000).isoformat()))
            elif w["ats"] == "ashby":
                for j in get_retry(f"https://api.ashbyhq.com/posting-api/job-board/{w['board']}", as_json=True).get("jobs", []):
                    locs = [j.get("location", "")] + [x.get("location", "") for x in j.get("secondaryLocations") or []]
                    out.append((j["title"].strip(), "; ".join(l for l in locs if l) + ("; Remote" if j.get("isRemote") else ""),
                                j["jobUrl"], j.get("publishedAt")))
        except Exception as e:
            print(f"  company board {w['company']}: failed ({type(e).__name__})", file=sys.stderr)
        jobs = []
        for title, loc, url, published in out:
            if not want.search(title):
                continue
            try:
                age = f"{(TODAY - dt.date.fromisoformat(published[:10])).days}d"
            except (TypeError, ValueError):
                age = ""
            jobs.append({"source": "company board", "tier": "Top company", "company": w["company"], "title": title,
                         "location": loc, "salary_text": "", "url": url, "age": age, "flags": []})
        return jobs

    with cf.ThreadPoolExecutor(12) as ex:
        return [j for batch in ex.map(board_jobs, COMPANIES["watchlist"]) for j in batch]


PARSERS = {
    "speedyapply": parse_speedyapply,
    "simplify": parse_simplify,
    "vanshb03": lambda md: parse_md_table(md, "vanshb03", {"company": "company", "title": "role", "location": "location",
                                                           "link": "application/link", "posted": "date posted"}),
    "jobright": lambda md: parse_md_table(md, "jobright", {"company": "company", "title": "job title", "location": "location",
                                                          "link": "job title", "posted": "date posted", "work_model": "work model"}),
}


def source_url(defn):
    y = PROFILE["grad_year"]
    return defn["url"].format(year=y, year_minus_1=y - 1)


# ---------------------------------------------------------------- matching

STATES = {"al": "alabama", "ak": "alaska", "az": "arizona", "ar": "arkansas", "ca": "california", "co": "colorado",
          "ct": "connecticut", "de": "delaware", "dc": "district of columbia", "fl": "florida", "ga": "georgia", "hi": "hawaii",
          "id": "idaho", "il": "illinois", "in": "indiana", "ia": "iowa", "ks": "kansas", "ky": "kentucky", "la": "louisiana",
          "me": "maine", "md": "maryland", "ma": "massachusetts", "mi": "michigan", "mn": "minnesota", "ms": "mississippi",
          "mo": "missouri", "mt": "montana", "ne": "nebraska", "nv": "nevada", "nh": "new hampshire", "nj": "new jersey",
          "nm": "new mexico", "ny": "new york", "nc": "north carolina", "nd": "north dakota", "oh": "ohio", "ok": "oklahoma",
          "or": "oregon", "pa": "pennsylvania", "ri": "rhode island", "sc": "south carolina", "sd": "south dakota",
          "tn": "tennessee", "tx": "texas", "ut": "utah", "vt": "vermont", "va": "virginia", "wa": "washington",
          "wv": "west virginia", "wi": "wisconsin", "wy": "wyoming"}
NON_US_RX = re.compile(r"(?i)\b(uk|united kingdom|london|canada|toronto|vancouver|montreal|ontario|british columbia|bc|qc|"
                       r"germany|berlin|munich|hamburg|ireland|dublin|india|bangalore|bengaluru|hyderabad|singapore|israel|tel aviv|"
                       r"europe|emea|apac|australia|sydney|japan|tokyo|china|shanghai|hong kong|netherlands|amsterdam|france|paris|"
                       r"poland|warsaw|ukraine|mexico|brazil|são paulo|switzerland|zurich|spain|madrid|barcelona)\b")
US_STATE_RX = re.compile(r",\s*(" + "|".join(k.upper() for k in STATES) + r")\b")


def regions_for(text, names=None):
    """Which of the named regions (default: all known) a location string mentions."""
    names = names or list(REGIONS)
    found = set()
    segs = [s.strip().lower() for s in re.split(r";|\n|\||/| or |\+\d+", text or "") if s.strip()]
    for name in names:
        rule = REGIONS[name]
        for seg in segs:
            if any(a in seg for a in rule.get("any", [])) or seg.strip(" .") in rule.get("exact", []):
                found.add(name)
                continue
            for st, cities in rule.get("places", {}).items():
                if re.search(rf",\s*({st}|{STATES.get(st, st)})\b", seg) and any(re.search(rf"\b{re.escape(c)}\b", seg) for c in cities):
                    found.add(name)
    return found


def wanted_regions():
    return PROFILE["main_regions"] + [r for r in PROFILE["secondary_regions"] if r not in PROFILE["main_regions"]]


def norm_company(c):
    c = re.sub(r"[^\w\s.&+-]", "", c.lower())
    c = re.sub(r"\b(inc|llc|ltd|corp|corporation|co|technologies|technology|labs|the)\b\.?", "", c)
    return re.sub(r"\s+", " ", c).strip()


def is_top_tier(job):
    if job.get("tier") in ("FAANG+", "Quant", "Top company"):
        return True
    c = norm_company(job["company"])
    exact_only = set(COMPANIES.get("top_tier_exact_only", []))
    return any(c == t or (t not in exact_only and c.startswith(t + " ")) for t in COMPANIES["top_tier"])


def location_ok(regions, job):
    if regions:
        return True
    return {"any": True, "top_tier": is_top_tier(job), "never": False}[PROFILE["elsewhere"]]


def pay_floor(regions):
    overrides = [PROFILE["region_pay_floors"][r] for r in regions if r in PROFILE["region_pay_floors"]]
    return min(overrides) if overrides else PROFILE.get("pay_floor", 0)


def parse_salary(text):
    """Best-effort annual number (max of a range): '$193k/yr', '$55/hr', '$120,000 - $150,000'."""
    if not text:
        return None
    t = text.lower().replace(",", "")
    nums = [float(m.group(1)) * (1000 if m.group(2) else 1) for m in re.finditer(r"\$\s*(\d+(?:\.\d+)?)\s*(k)?", t)]
    if not nums:
        return None
    v = max(nums)
    if "/hr" in t or "hour" in t or v < 500:
        v *= 2080
    return int(v) if v >= 20000 else None


# Role families recognised from titles; used to drop "never" roles early and to rate fit later.
ROLE_TITLE_RX = {
    "embedded": r"firmware|embedded|fpga|asic|\brtl\b|silicon|hardware|analog|\brf\b|circuit|soc\b",
    "testing": r"\bqa\b|quality assurance|\bsdet\b|test engineer|testing engineer|\btest\b|validation|verification",
    "mobile": r"\bios\b|android|mobile",
    "frontend": r"front[- ]?end|\bui\b|\bux\b|web developer",
    "ml": r"machine learning|\bml\b|\bai\b|deep learning|computer vision|perception|\bllm\b|inference",
    "data": r"data engineer|data platform|analytics engineer|\betl\b|data infra",
    "security": r"security|cyber",
    "infra": r"infra|platform|devops|\bsre\b|site reliability|cloud|network|kernel|systems engineer",
    "quant": r"quant|trading",
    "robotics": r"robotics|simulation|autonomy|autonomous|\bros\b",
    "fde": r"forward[- ]deployed|solutions engineer|customer engineer|client services|customer success",
    "fullstack": r"full[- ]?stack|product engineer",
    "backend": r"back[- ]?end|\bapi\b|distributed",
}
TITLE_REQUIRE_ANY = ["software", "engineer", "developer", "sde", "swe", "full stack", "full-stack", "fullstack",
                     "backend", "back-end", "member of technical staff", "programmer"]
SENIORITY_RX = [r"\bsenior\b", r"\bsr\.?\b", r"\bstaff\b", r"\bprincipal\b", r"\blead\b", r"\bmanager\b", r"\bdirector\b",
                r"\barchitect\b", r"\bhead of\b", r"\b(ii|iii|iv)\b", r"\b(engineer|developer|sde|swe)\s*[2-9]\b", r"\blevel\s*[2-9]\b",
                r"\bl[4-9]\b", r"\bmid[- ]?level\b", r"\bexperienced\b", r"\bintern(ship)?\b", r"\bco-?op\b", r"\bpostdoc",
                r"sales engineer", r"support engineer", r"technician", r"data analyst", r"business analyst", r"research scientist",
                r"applied scientist", r"data scientist", r"product manager", r"designer", r"mechanical", r"electrical",
                r"manufacturing"]


def title_families(title):
    t = title.lower()
    return [f for f, rx in ROLE_TITLE_RX.items() if re.search(rx, t)]


def prefilter(job):
    """Cheap checks on list data only. Returns (keep, reason)."""
    days = age_days(job.get("age"))
    if days is not None and days > PROFILE["max_age_days"]:
        return False, f"posted {days} days ago"
    title = job["title"].lower()
    if not any(k in title for k in TITLE_REQUIRE_ANY):
        return False, "not a software title"
    for rx in SENIORITY_RX:
        if re.search(rx, title):
            return False, f"title matches /{rx}/"
    gy = PROFILE["grad_year"]
    years = {int(y) for y in re.findall(r"\b(20\d\d)\b", title)}
    if years and str(gy) not in title and max(years) < gy:
        return False, f"title targets {sorted(years)} grads"
    if PROFILE.get("degree", "bachelors") == "bachelors" and (re.search(r"\bph\.?d\b|master'?s\b|\bms\b", title) or "advanced degree" in job.get("flags", [])):
        return False, "advanced degree required"
    never = set(PROFILE["never_roles"]) & set(title_families(title))
    if never and not (set(title_families(title)) & set(PROFILE["primary_roles"])):
        return False, f"role type you excluded ({', '.join(sorted(never))})"
    regions = regions_for(job["location"], wanted_regions())
    job["regions"] = sorted(regions, key=wanted_regions().index)
    if not regions and NON_US_RX.search(job["location"]) and not US_STATE_RX.search(job["location"]):
        return False, "outside the US"
    job["hidden_locations"] = bool(re.search(r"\+\d+", job["location"]))
    if not location_ok(regions, job) and not job["hidden_locations"]:
        return False, f"location '{job['location'][:60]}' not in your areas"
    sal = parse_salary(job.get("salary_text"))
    if sal and sal < pay_floor(regions):
        return False, f"pay {sal} below your floor"
    return True, ""


# ---------------------------------------------------------------- job descriptions

_ashby_cache = {}


def fetch_jd(url):
    """dict(text, location, pay, closed, method). text is '' when the page can't be read without a browser."""
    u = urllib.parse.urlsplit(url)
    host, parts = u.netloc.lower(), [p for p in u.path.split("/") if p]

    def gh(board, jid):
        j = get(f"https://boards-api.greenhouse.io/v1/boards/{board}/jobs/{jid}?pay_transparency=true", as_json=True)
        pay = " ".join(f"${r.get('min_cents', 0)//100}-${r.get('max_cents', 0)//100}" for r in j.get("pay_input_ranges", []) or [])
        return dict(text=strip_html(j.get("content", "")), location=(j.get("location") or {}).get("name", ""), pay=pay, method="greenhouse")

    try:
        if "greenhouse.io" in host and "jobs" in parts:
            board = parse_qs(u.query).get("for") or parts[parts.index("jobs") - 1]
            return gh(board, parts[parts.index("jobs") + 1])
        if host == "jobs.lever.co" and len(parts) >= 2:
            j = get(f"https://api.lever.co/v0/postings/{parts[0]}/{parts[1]}", as_json=True)
            text = j.get("descriptionPlain", "") + "\n" + "\n".join(
                l.get("text", "") + "\n" + strip_html(l.get("content", "")) for l in j.get("lists", [])) + "\n" + j.get("additionalPlain", "")
            sr = j.get("salaryRange") or {}
            cat = j.get("categories") or {}
            return dict(text=text, location="; ".join(cat.get("allLocations") or [cat.get("location", "")]),
                        pay=f"${sr.get('min')}-${sr.get('max')} {sr.get('interval', '')}" if sr else "", method="lever")
        if host == "jobs.ashbyhq.com" and len(parts) >= 2:
            org, jid = parts[0], parts[1]
            if org not in _ashby_cache:
                _ashby_cache[org] = get(f"https://api.ashbyhq.com/posting-api/job-board/{org}?includeCompensation=true", as_json=True).get("jobs", [])
            j = next((x for x in _ashby_cache[org] if x.get("id") == jid), None)
            if not j:
                return dict(text="", location="", pay="", closed=True, method="ashby")
            locs = [j.get("location", "")] + [s.get("location", "") for s in j.get("secondaryLocations", []) or []] + (["Remote"] if j.get("isRemote") else [])
            return dict(text=j.get("descriptionPlain", ""), location="; ".join(l for l in locs if l),
                        pay=(j.get("compensation") or {}).get("compensationTierSummary", "") or "", method="ashby")
        if host == "jobs.smartrecruiters.com" and len(parts) >= 2:
            j = get(f"https://api.smartrecruiters.com/v1/companies/{parts[0]}/postings/{parts[1].split('-')[0]}", as_json=True)
            secs = (j.get("jobAd") or {}).get("sections", {})
            loc = j.get("location") or {}
            return dict(text="\n".join(strip_html((secs.get(k) or {}).get("text", "")) for k in ("jobDescription", "qualifications", "additionalInformation")),
                        location=f"{loc.get('city', '')}, {loc.get('region', '')}{'; Remote' if loc.get('remote') else ''}", pay="", method="smartrecruiters")
        if host == "apply.workable.com" and "j" in parts:
            j = get(f"https://apply.workable.com/api/v2/accounts/{parts[0]}/jobs/{parts[parts.index('j') + 1]}", as_json=True)
            loc = j.get("location") or {}
            return dict(text="\n".join(strip_html(j.get(k, "")) for k in ("description", "requirements", "benefits")),
                        location=f"{loc.get('city', '')}, {loc.get('region', '')}{'; Remote' if j.get('remote') else ''}", pay="", method="workable")
        if "myworkdayjobs.com" in host and "job" in parts:
            p = [x for x in parts if not re.fullmatch(r"[a-z]{2}-[A-Z]{2}", x)]
            j = get(f"https://{host}/wday/cxs/{host.split('.')[0]}/{p[0]}/{'/'.join(p[p.index('job'):])}", as_json=True)
            info = j.get("jobPostingInfo", {})
            return dict(text=strip_html(info.get("jobDescription", "")), location="; ".join(l for l in [info.get("location", "")] + (info.get("additionalLocations") or []) if l),
                        pay="", method="workday")
        if host.endswith("jobright.ai") and "info" in parts:
            page = get(url)
            if "Security check" in page[:600]:
                return dict(text="", location="", pay="", method="challenged")
            m = re.search(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', page, re.S)
            j = json.loads(m.group(1))["props"]["pageProps"]["dataSource"]["jobResult"] if m else {}
            if not j:
                text = strip_html(page)
                pay = re.search(r"\$[\d.]+K/yr\s*-\s*\$[\d.]+K/yr", text)
                return dict(text=text[:6000], location="", pay=pay.group(0) if pay else "", method="jobright-lite")
            q = j.get("qualifications") or {}
            yoe = ((j.get("detailQualifications") or {}).get("mustHave") or {}).get("yoe") or []
            text = "\n".join([j.get("jobTitle", ""), f"Seniority: {j.get('jobSeniority', '')}", j.get("jobSummary", "")]
                             + [f"Required: {x}" for x in q.get("mustHave", [])] + [f"Experience: {x}" for x in yoe]
                             + [f"Preferred: {x}" for x in q.get("preferredHave", []) or []]
                             + (["Active security clearance required"] if j.get("isClearanceRequired") else []))
            loc = "; ".join(j.get("jobLocations") or []) + ("; Remote" if j.get("isRemote") or j.get("workModel") == "Remote" else "")
            return dict(text=text, location=loc, pay=j.get("salaryDesc") or "", closed=bool(j.get("isDeleted")), method="jobright")
        gh_jid = parse_qs(u.query).get("gh_jid")
        if gh_jid:  # company career page that wraps Greenhouse: try likely board names
            guesses = [p for p in parts if re.fullmatch(r"[a-z0-9-]{3,40}", p)] + [host.split(".")[-2], host.split(".")[0]]
            for board in dict.fromkeys(g for g in guesses if g not in ("www", "jobs", "careers", "job", "job-board", "company")):
                try:
                    return gh(board, gh_jid)
                except urllib.error.HTTPError:
                    continue
        page = get(url)
        for blob in re.findall(r'<script[^>]+application/ld\+json[^>]*>(.*?)</script>', page, re.S):
            try:
                data = json.loads(blob.strip())
            except ValueError:
                continue
            for d in (data if isinstance(data, list) else data.get("@graph", [data]) if isinstance(data, dict) else []):
                if isinstance(d, dict) and d.get("@type") == "JobPosting":
                    bs = (d.get("baseSalary") or {}).get("value") if isinstance(d.get("baseSalary"), dict) else None
                    pay = f"${bs.get('minValue', '')}-${bs.get('maxValue', bs.get('value', ''))} {bs.get('unitText', '')}" if isinstance(bs, dict) else ""
                    locs = d.get("jobLocation") or []
                    locs = locs if isinstance(locs, list) else [locs]
                    loc = "; ".join(f"{(l.get('address') or {}).get('addressLocality', '')}, {(l.get('address') or {}).get('addressRegion', '')}"
                                    for l in locs if isinstance(l, dict)) + ("; Remote" if d.get("jobLocationType") == "TELECOMMUTE" else "")
                    return dict(text=strip_html(d.get("description", "")), location=loc, pay=pay, method="jsonld")
        text = strip_html(page)
        if len(text) > 1500 and re.search(r"(?i)qualifications|requirements|experience", text):
            return dict(text=text[:20000], location="", pay="", method="html")
        return dict(text="", location="", pay="", method="needs_browser")
    except urllib.error.HTTPError as e:
        if e.code in (404, 410):
            return dict(text="", location="", pay="", closed=True, method=f"http{e.code}")
        return dict(text="", location="", pay="", method=f"needs_browser(http{e.code})")
    except Exception as e:
        return dict(text="", location="", pay="", method=f"needs_browser({type(e).__name__})")


SNIPPET_RX = re.compile(
    r"(?i)(\d+\+?\s*(?:-|–|to)?\s*\d*\+?\s*years?|new grad|new college|recent grad|graduat|entry[- ]level|early[- ]career|university grad|"
    r"class of|\b20[2-3]\d\b|bachelor|degree|salary|compensation|base pay|pay range|\$\s?\d|remote|hybrid|on-?site|in[- ]office|"
    r"clearance|ts/sci|top secret|secret\b|polygraph|start date|sponsor|citizen)")
PRIORITY_RX = re.compile(r"(?i)(years?|new grad|new college|recent grad|graduat|entry[- ]level|early[- ]career|university|class of|"
                         r"\b20[2-3]\d\b|clearance|ts/sci|secret|polygraph|sponsor|citizen)")


def snippets(text, limit=900):
    lines, seen = [], set()
    for line in re.split(r"\n|(?<=[.!?])\s+(?=[A-Z])", text):
        line = line.strip()
        if 15 < len(line) < 350 and SNIPPET_RX.search(line) and line not in seen:
            seen.add(line)
            lines.append(line)
    lines.sort(key=lambda l: 0 if PRIORITY_RX.search(l) else 1)
    out, total = [], 0
    for l in lines:
        if total + len(l) > limit:
            break
        out.append(l)
        total += len(l)
    return out


def yoe_min(text):
    vals = [int(m.group(1)) for m in re.finditer(r"(?i)(\d{1,2})\+?\s*(?:-|–|to)?\s*(\d{1,2})?\+?\s*years?(?:\s+of)?(?:\s+\w+){0,4}\s+experience", text)]
    return min(vals) if vals else None


# ---------------------------------------------------------------- state

def norm_title(t):
    t = re.sub(r"[^a-z0-9 ]", " ", t.lower())
    t = re.sub(r"\b(new grad(uate)?s?|graduate|early career|university grad|college grad|20\d\d|start|usa|us)\b", " ", t)
    return " ".join(sorted(set(t.split())))


def job_sig(company, title, location):
    location = location.split(";")[0]
    regs = sorted(regions_for(location))
    place = ",".join(regs) if regs else re.split(r"[;,+]", location.lower())[0].strip()
    return f"{norm_company(company)}|{norm_title(title)}|{place}"


def load_seen():
    return json.load(open(SEEN_PATH)) if os.path.exists(SEEN_PATH) else {}


def save_seen(seen):
    os.makedirs(STATE, exist_ok=True)
    json.dump(seen, open(SEEN_PATH, "w"), indent=1, sort_keys=True)


# ---------------------------------------------------------------- collect

def collect():
    os.makedirs(STATE, exist_ok=True)
    seen = load_seen()
    jobs = []
    for name in PROFILE["sources"]:
        defn = SOURCE_DEFS.get(name)
        if not defn or defn["parser"] == "watchlist":
            continue
        try:
            parsed = PARSERS[defn["parser"]](get(source_url(defn), timeout=60))
            print(f"{name}: {len(parsed)} listings", file=sys.stderr)
            jobs += parsed
        except Exception as e:
            print(f"{name}: failed ({e})", file=sys.stderr)
    if "company_boards" in PROFILE["sources"]:
        wl = fetch_company_boards()
        print(f"company boards: {len(wl)} early-career postings from {len(COMPANIES['watchlist'])} companies", file=sys.stderr)
        jobs = wl + jobs  # first, so dedupe keeps the direct company link

    uniq, by_sig = {}, {}
    for j in jobs:
        j["key"] = canon_url(j["url"])
        sig = (norm_company(j["company"]), j["title"].lower(), j["location"].lower().split(";")[0])
        prev = uniq.get(j["key"]) or by_sig.get(sig)
        if prev:
            prev["salary_text"] = prev.get("salary_text") or j.get("salary_text", "")
            prev["tier"] = prev.get("tier") or j.get("tier", "")
            continue
        uniq[j["key"]] = by_sig[sig] = j
    seen_sigs = {v["sig"]: k for k, v in seen.items() if v.get("sig")}
    new = []
    for j in uniq.values():
        if j["key"] in seen:
            continue
        j["sig"] = job_sig(j["company"], j["title"], j["location"])
        if j["sig"] in seen_sigs:
            seen[j["key"]] = {"status": "duplicate", "reason": f"same job as {seen_sigs[j['sig']]}", "date": TODAY.isoformat(), "sig": j["sig"]}
            continue
        new.append(j)
    print(f"unique listings: {len(uniq)}, new since last run: {len(new)}", file=sys.stderr)

    new_file = not os.path.exists(REJECTED_PATH)
    with open(REJECTED_PATH, "a", newline="") as f:
        w = csv.writer(f)
        if new_file:
            w.writerow(["date", "source", "company", "title", "location", "reason", "url"])

        def reject(j, reason):
            seen[j["key"]] = {"status": "rejected", "reason": reason, "date": TODAY.isoformat(), "sig": j.get("sig", "")}
            w.writerow([TODAY.isoformat(), j["source"], j["company"], j["title"], j["location"], reason, j["url"]])

        survivors = []
        for j in new:
            ok, why = prefilter(j)
            survivors.append(j) if ok else reject(j, why)
        # Some lists use short redirect links; resolve them so links and duplicate checks use the real posting URL.
        def resolve(j):
            if urllib.parse.urlsplit(j["url"]).netloc.lower() in SHORTLINK_HOSTS:
                try:
                    req = urllib.request.Request(j["url"], method="HEAD", headers={"User-Agent": UA})
                    with urllib.request.urlopen(req, timeout=15) as r:
                        j["url"] = r.geturl()
                except Exception:
                    pass
            return j
        with cf.ThreadPoolExecutor(8) as ex:
            survivors = list(ex.map(resolve, survivors))
        kept, sigs = [], {}
        for j in survivors:
            k = canon_url(j["url"])
            if k in seen or k in sigs.values():
                seen[j["key"]] = {"status": "duplicate", "reason": "same posting as an earlier link", "date": TODAY.isoformat(), "sig": j["sig"]}
                continue
            if j["sig"] in sigs:
                seen[j["key"]] = {"status": "duplicate", "reason": f"same job as {sigs[j['sig']]}", "date": TODAY.isoformat(), "sig": j["sig"]}
                continue
            sigs[j["sig"]] = k
            kept.append(j)
        survivors = kept
        print(f"passed list filters: {len(survivors)}", file=sys.stderr)

        # jobright answers bursts with a bot check: read its pages slowly, newest first, capped per run, and leave the
        # rest unseen for the next run if the check appears. Never try to get around it.
        slow = sorted((j for j in survivors if "jobright.ai" in j["url"]), key=lambda j: age_days(j["age"]) or 0)
        later, slow = slow[JOBRIGHT_MAX_PER_RUN:], slow[:JOBRIGHT_MAX_PER_RUN]
        fast = [j for j in survivors if "jobright.ai" not in j["url"]]
        with cf.ThreadPoolExecutor(12) as ex:
            for j, jd in zip(fast, ex.map(lambda x: fetch_jd(x["url"]), fast)):
                j["jd"] = jd
        deferred = later
        for n, j in enumerate(slow):
            j["jd"] = fetch_jd(j["url"])
            if j["jd"]["method"] == "challenged":
                deferred = slow[n:] + later
                print(f"jobright asked us to slow down; leaving {len(deferred)} jobs for the next run", file=sys.stderr)
                break
            time.sleep(JOBRIGHT_DELAY_S)
        survivors = [j for j in survivors if j not in deferred]

        candidates = []
        for j in survivors:
            jd = j.pop("jd")
            if jd.get("closed"):
                reject(j, "posting closed")
                continue
            text = jd.get("text", "")
            all_loc = "; ".join(x for x in (j["location"], jd.get("location", "")) if x)
            regions = regions_for(all_loc, wanted_regions())
            if j["hidden_locations"] and not regions and text:
                regions = regions_for(text[:3000], wanted_regions()) - {"Remote"}
            if not location_ok(regions, j):
                reject(j, f"locations '{all_loc[:60]}' not in your areas")
                continue
            pay_max = parse_salary(jd.get("pay", "")) or parse_salary(" ".join(re.findall(r"\$\s?[\d,]{5,}(?:\.\d+)?|\$\s?\d{2,3}k", text)))
            best_pay = max(x for x in (pay_max, parse_salary(j.get("salary_text")), 0) if x is not None) or None
            if best_pay and best_pay < pay_floor(regions):
                reject(j, f"pay {best_pay} below your floor")
                continue
            if not best_pay and not PROFILE.get("keep_unlisted_pay", True):
                reject(j, "pay not listed")
                continue
            y = yoe_min(text)
            if y is not None and y >= 3:
                reject(j, f"requires {y}+ years experience")
                continue
            ordered = sorted(regions, key=wanted_regions().index)
            candidates.append({
                "key": j["key"], "sig": j["sig"], "url": j["url"], "source": j["source"], "company": j["company"],
                "title": j["title"], "tier": j.get("tier") or ("Top company" if is_top_tier(j) else ""),
                "location": all_loc[:300], "regions": ordered, "pay": jd.get("pay") or j.get("salary_text", ""),
                "pay_max": best_pay, "age": j["age"], "jd_method": jd["method"], "min_yoe": y,
                "snippets": snippets(text) if text else [], "text": text[:8000],
            })
    save_seen(seen)
    json.dump(candidates, open(CANDIDATES_PATH, "w"), indent=1)
    unread = sum(1 for c in candidates if not c["text"])
    print(f"candidates to review: {len(candidates)} ({unread} couldn't be read without a browser)", file=sys.stderr)
    return candidates


def show(a=0, b=None):
    c = json.load(open(CANDIDATES_PATH))
    for i, x in enumerate(c[a:b], a):
        print(f"#{i} {x['company']} | {x['title']} | {x['location'][:80]} | {','.join(x['regions']) or '-'} | {x['tier'] or '-'} | "
              f"pay:{(x['pay'] or '?')[:40]} | yoe:{x['min_yoe']} | {x['jd_method']} | {x['age']}")
        for s in x["snippets"][:6]:
            print("    -", s[:220])


# ---------------------------------------------------------------- publish

NG_LABEL = {"C": "Confirmed new grad", "L": "Likely entry-level", "U": "Unverified"}
FIT_LABEL = {"S": "Strong", "G": "Good", "P": "Partial"}


def load_decisions(path):
    """.json from review.py, or agent .txt lines:  idx|L|C|S|reason   /   idx|S|reason"""
    if path.endswith(".json"):
        return json.load(open(path))
    out = []
    for line in open(path):
        p = line.rstrip("\n").split("|")
        if not p[0].strip().isdigit():
            continue
        if p[1] == "L":
            out.append({"idx": int(p[0]), "decision": "log", "ng": p[2], "fit": p[3], "reason": "|".join(p[4:])})
        else:
            out.append({"idx": int(p[0]), "decision": "skip", "reason": "|".join(p[2:])})
    return out


def area_rank(regions):
    if any(r in PROFILE["main_regions"] for r in regions):
        return 1
    return 2 if any(r in PROFILE["secondary_regions"] for r in regions) else 3


def publish(path):
    cands = json.load(open(CANDIDATES_PATH))
    decisions = load_decisions(path)
    rows = []
    for d in decisions:
        if d["decision"] != "log":
            continue
        x = cands[d["idx"]]
        age = age_days(x["age"])
        rows.append({
            "best_match": d["fit"] == "S" and area_rank(x["regions"]) == 1,
            "role": x["title"], "company": x["company"], "new_grad_check": NG_LABEL[d["ng"]], "fit": FIT_LABEL[d["fit"]],
            "why": d["reason"], "regions": x["regions"] or ["Other"], "tier": x["tier"] or "Other",
            "location": x["location"][:200], "pay": (x["pay"] or "Not listed")[:120], "pay_max": x.get("pay_max"),
            "apply_link": x["url"], "source": x["source"],
            "posted": (TODAY - dt.timedelta(days=age)).isoformat() if age is not None else "",
            "added": dt.datetime.now().isoformat(timespec="minutes"),
        })
    rows.sort(key=lambda r: (not r["best_match"], area_rank(r["regions"]), ["Strong", "Good", "Partial"].index(r["fit"]), r["posted"] == "", r["posted"]), reverse=False)
    os.makedirs(OUTPUT, exist_ok=True)
    write_csv(rows)
    write_markdown(rows)
    if PROFILE.get("notion", {}).get("enabled") and rows:
        push_notion(rows)
    seen = load_seen()
    for d in decisions:
        x = cands[d["idx"]]
        seen[x["key"]] = {"status": "logged" if d["decision"] == "log" else "skipped", "reason": d["reason"],
                          "date": TODAY.isoformat(), "sig": x.get("sig", "")}
    save_seen(seen)
    best = [r for r in rows if r["best_match"]]
    print(f"logged {len(rows)} new jobs ({len(best)} best matches); {len(decisions) - len(rows)} skipped", file=sys.stderr)
    for r in best:
        print(f"  ★ {r['company']} — {r['role']} ({', '.join(r['regions'])})", file=sys.stderr)


CSV_FIELDS = ["added", "best_match", "company", "role", "fit", "new_grad_check", "regions", "location", "pay", "posted", "why",
              "apply_link", "tier", "source", "status"]


def write_csv(rows):
    path = os.path.join(OUTPUT, "jobs.csv")
    new = not os.path.exists(path)
    with open(path, "a", newline="") as f:
        w = csv.DictWriter(f, fieldnames=CSV_FIELDS, extrasaction="ignore")
        if new:
            w.writeheader()
        for r in rows:
            w.writerow({**r, "regions": ", ".join(r["regions"]), "best_match": "yes" if r["best_match"] else "", "status": "To apply"})


def write_markdown(rows):
    path = os.path.join(OUTPUT, "LATEST.md")
    lines = [f"# New matches — {dt.datetime.now():%Y-%m-%d %H:%M}", "", "★ = best match (strong fit in one of your main areas)", "",
             "| | Role | Company | Fit | New grad | Area | Pay | Posted | Why |", "|---|---|---|---|---|---|---|---|---|"]
    for r in rows:
        role = f"[**{r['role']}**]({r['apply_link']})" if r["best_match"] else f"[{r['role']}]({r['apply_link']})"
        cells = ["★" if r["best_match"] else "", role, r["company"], r["fit"], r["new_grad_check"], ", ".join(r["regions"]),
                 r["pay"], r["posted"] or "?", r["why"]]
        lines.append("| " + " | ".join(str(c).replace("|", "/") for c in cells) + " |")
    if not rows:
        lines.append("| | No new matches this run | | | | | | | |")
    open(path, "w").write("\n".join(lines) + "\n")


# ---------------------------------------------------------------- Notion (optional)

NOTION_VERSION = "2022-06-28"


def notion(method, path, body=None):
    token = os.environ.get("NOTION_TOKEN")
    if not token:
        sys.exit("Set NOTION_TOKEN to your Notion integration secret (see README).")
    req = urllib.request.Request(f"https://api.notion.com/v1/{path}", method=method, data=json.dumps(body).encode() if body else None,
                                 headers={"Authorization": f"Bearer {token}", "Notion-Version": NOTION_VERSION, "Content-Type": "application/json"})
    for attempt in range(4):
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                return json.loads(r.read())
        except urllib.error.HTTPError as e:
            if e.code == 429 and attempt < 3:
                time.sleep(int(e.headers.get("Retry-After", "2")))
                continue
            raise SystemExit(f"Notion API error {e.code}: {e.read().decode()[:300]}")


def notion_id(s):
    m = re.findall(r"[0-9a-f]{32}|[0-9a-f-]{36}", s.replace("-", "") if len(s) > 36 else s)
    return (m[-1] if m else s).replace("-", "")


def notion_setup(page):
    sel = lambda *names: {"select": {"options": [{"name": n} for n in names]}}
    props = {
        "Role": {"title": {}}, "Company": {"rich_text": {}},
        "Status": sel("To apply", "Applied", "Interviewing", "Offer", "Rejected", "Skipped"),
        "Best match": {"checkbox": {}}, "Fit": sel("Strong", "Good", "Partial"),
        "New grad check": sel("Confirmed new grad", "Likely entry-level", "Unverified"),
        "Region": {"multi_select": {"options": [{"name": r} for r in wanted_regions() + ["Other"]]}},
        "Tier": sel("FAANG+", "Quant", "Top company", "Other"), "Location": {"rich_text": {}}, "Pay": {"rich_text": {}},
        "Pay max": {"number": {"format": "dollar"}}, "Why": {"rich_text": {}}, "Apply link": {"url": {}},
        "Source": {"select": {}}, "Posted": {"date": {}}, "Added": {"created_time": {}},
    }
    db = notion("POST", "databases", {"parent": {"type": "page_id", "page_id": notion_id(page)},
                                      "title": [{"type": "text", "text": {"content": "New Grad Job Radar"}}], "properties": props})
    PROFILE.setdefault("notion", {})
    PROFILE["notion"].update({"enabled": True, "database_id": db["id"]})
    json.dump(PROFILE, open(PROFILE_PATH, "w"), indent=2)
    print(f"Created database {db.get('url', db['id'])} and saved its id to profile.json.")
    print("Tip: in Notion, add a view filtered to Posted within the past week and sorted by Added (newest first).")


def push_notion(rows):
    db = PROFILE["notion"].get("database_id")
    if not db:
        print("Notion enabled but no database_id; run: python3 radar.py notion-setup <page>", file=sys.stderr)
        return
    text = lambda s: [{"type": "text", "text": {"content": str(s)[:1900]}}]
    for r in rows:
        props = {
            "Role": {"title": [{"type": "text", "text": {"content": r["role"][:1900]}, "annotations": {"bold": r["best_match"]}}]},
            "Company": {"rich_text": text(r["company"])}, "Status": {"select": {"name": "To apply"}},
            "Best match": {"checkbox": r["best_match"]}, "Fit": {"select": {"name": r["fit"]}},
            "New grad check": {"select": {"name": r["new_grad_check"]}},
            "Region": {"multi_select": [{"name": g} for g in r["regions"]]}, "Tier": {"select": {"name": r["tier"]}},
            "Location": {"rich_text": text(r["location"])}, "Pay": {"rich_text": text(r["pay"])},
            "Why": {"rich_text": text(r["why"])}, "Apply link": {"url": r["apply_link"]}, "Source": {"select": {"name": r["source"]}},
        }
        if r.get("pay_max"):
            props["Pay max"] = {"number": r["pay_max"]}
        if r.get("posted"):
            props["Posted"] = {"date": {"start": r["posted"]}}
        notion("POST", "pages", {"parent": {"database_id": db}, "properties": props})
        time.sleep(0.35)  # stay under Notion's ~3 requests/second
    print(f"sent {len(rows)} rows to Notion", file=sys.stderr)


# ---------------------------------------------------------------- cli

def stats():
    from collections import Counter
    print(Counter(v["status"] for v in load_seen().values()))


def main():
    global PROFILE
    cmd = sys.argv[1] if len(sys.argv) > 1 else "run"
    PROFILE = load_profile()
    if cmd == "collect":
        collect()
    elif cmd == "show":
        show(*(int(a) for a in sys.argv[2:4]))
    elif cmd == "review":
        import review
        print(review.review_file(PROFILE))
    elif cmd == "publish":
        publish(sys.argv[2])
    elif cmd == "run":
        import review
        collect()
        publish(review.review_file(PROFILE))
    elif cmd == "notion-setup":
        notion_setup(sys.argv[2])
    elif cmd == "stats":
        stats()
    else:
        sys.exit(__doc__)


if __name__ == "__main__":
    main()
