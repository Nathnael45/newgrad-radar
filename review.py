"""Rule-based review of state/candidates.json against profile.json.

Each candidate gets: log or skip, a new-grad check (C = confirmed, L = likely entry-level, U = unverified)
and a fit rating (S = strong, G = good, P = partial), plus a one-line reason.
It is deliberately conservative about skipping: when the description can't be read, a title that says
"new grad" is logged as Unverified instead of being thrown away.
"""
import datetime as dt
import json
import os
import re

HERE = os.path.dirname(os.path.abspath(__file__))
CANDIDATES_PATH = os.path.join(HERE, "state", "candidates.json")

NEW_GRAD_RX = re.compile(r"(?i)\bnew[- ]grad|new college grad|recent grad|university grad|college grad|early[- ]career|entry[- ]level|"
                         r"campus hire|graduate (software|engineer|program)|\bgrad(uate)?s? 20\d\d|class of 20\d\d|0\s*[-–]\s*[12]\+? years|"
                         r"no (prior )?experience required|new grads (ok|welcome)")
GRAD_YEAR_CONTEXT_RX = re.compile(r"(?i)(graduat\w*|degree|class of|grads?)[^.\n]{0,80}?\b(20\d\d)\b|\b(20\d\d)\b[^.\n]{0,25}?(graduat\w*|grads?)")
START_BEFORE_RX = re.compile(r"(?i)(start|begin)\w*[^.\n]{0,40}?\bbefore\b[^.\n]{0,30}?\b(january|february|march|april|may|june|july|august|september|october|november|december)\b[^.\n]{0,6}?\b(20\d\d)")
ADV_DEGREE_RX = re.compile(r"(?i)(master'?s|m\.s\.|\bms\b|ph\.?d)\s*(degree)?\s*(is\s+)?(required|needed)|requires? (an? )?(master'?s|ph\.?d)|"
                           r"(currently )?pursuing (a )?(master'?s|ph\.?d)(?! or)")
NON_INTERNSHIP_RX = re.compile(r"(?i)non[- ]internship|excluding internships|not including internships|post[- ]graduat\w* experience|"
                               r"full[- ]time (professional|industry) experience")
INTERNSHIPS_COUNT_RX = re.compile(r"(?i)internships? (and|or|/)? ?(projects? )?(count|included|welcome|accepted)|including internships|"
                                  r"internship,? (research|academic|or project) experience|new grads ok")
ACTIVE_CLEARANCE_RX = re.compile(r"(?i)(active|current|existing)\s+(ts/sci|top secret|secret|security)\s*(/sci)?\s*clearance|"
                                 r"must (currently )?(possess|hold|have)[^.\n]{0,40}clearance|clearance required (to|for|at) start|"
                                 r"ts/sci (with|w/) (full[- ]scope )?poly(graph)? (required|is required)")
ANY_CLEARANCE_RX = re.compile(r"(?i)clearance|ts/sci|top secret|polygraph")
NO_SPONSOR_RX = re.compile(r"(?i)(not|unable to|will not|won't|cannot|does not) (be able to )?(sponsor|provide sponsorship)|"
                           r"no (visa )?sponsorship|sponsorship (is )?not (available|provided)|u\.?s\.? citizen(ship)? (is )?required|"
                           r"must be a u\.?s\.? citizen")
CITIZEN_ONLY_RX = re.compile(r"(?i)u\.?s\.? citizen(ship)? (is )?required|must be a u\.?s\.? citizen|us citizens? only")
ENTRY_TITLE_RX = re.compile(r"(?i)\b(i|1|0)\b\s*($|[-–,(])|\bjunior\b|\bjr\.?\b|\bassociate\b|entry|founding engineer|new grad|early career")
TEMP_RX = re.compile(r"(?i)\b(internship|intern|co-?op|temporary|fixed[- ]term|contract(or)? role|12[- ]week|summer 20\d\d)\b")

FAMILY_TEXT_RX = {
    "backend": r"back[- ]?end|\bapis?\b|distributed systems|microservices|services at scale|databases?|server[- ]side",
    "fullstack": r"full[- ]?stack|front[- ]?end and back[- ]?end|react[^.]{0,40}(node|python|java|go)|product engineer",
    "frontend": r"front[- ]?end|react|typescript|\bui\b|web app",
    "infra": r"infrastructure|platform|kubernetes|devops|\bsre\b|reliability|cloud|networking",
    "data": r"data (engineering|pipelines?|platform)|\betl\b|spark|warehouse",
    "ml": r"machine learning|\bml\b|\bllms?\b|deep learning|model (training|serving|inference)|computer vision",
    "mobile": r"\bios\b|android|swift|kotlin|mobile",
    "security": r"security|vulnerab|threat|cryptograph",
    "embedded": r"firmware|embedded|microcontroller|\brtos\b|fpga|hardware",
    "quant": r"trading|quant|low[- ]latency|market data",
    "testing": r"test automation|\bqa\b|quality assurance|\bsdet\b",
    "fde": r"forward[- ]deployed|customer[- ]facing engineer|solutions engineer",
    "robotics": r"robotics|simulation|autonomy|autonomous vehicles?|\bros\b",
}


def _families(title, text):
    from radar import title_families
    fams = title_families(title)
    if fams:
        return fams
    scores = {f: len(re.findall(rx, text, re.I)) for f, rx in FAMILY_TEXT_RX.items()}
    best = [f for f, n in sorted(scores.items(), key=lambda kv: -kv[1]) if n >= 2][:2]
    return best


GRAD_WORD_RX = re.compile(r"(?i)graduat\w*|class of|\bgrads?\b|degree (completion|conferred|by)|expected (graduation|to graduate)")


def _grad_years_mentioned(text):
    """Every year that appears near graduation wording, e.g. 'graduating in December 2026 or Spring 2027' -> {2026, 2027}."""
    years = set()
    for m in GRAD_WORD_RX.finditer(text):
        window = text[max(0, m.start() - 30): m.end() + 110]
        years |= {int(y) for y in re.findall(r"\b(20[2-3]\d)\b", window)}
    return years


def _short_pay(c):
    if not c.get("pay_max"):
        return "pay not listed"
    lo = re.findall(r"\$\s?([\d,.]+)\s*(k)?", c.get("pay") or "")
    if lo:
        v = float(lo[0][0].replace(",", "")) * (1000 if lo[0][1] else 1)
        if v < 500:
            v *= 2080
        if 20000 <= v < c["pay_max"]:
            return f"${round(v / 1000)}-{round(c['pay_max'] / 1000)}k"
    return f"up to ${round(c['pay_max'] / 1000)}k"


def review_one(c, p):
    title, text = c["title"], c.get("text") or ""
    blob = f"{title}\n{text}"
    gy, gm = p["grad_year"], p.get("grad_month", 5)
    notes = []

    # --- hard stops
    if TEMP_RX.search(title):
        return "skip", "internship / temporary role"
    if p.get("degree", "bachelors") == "bachelors" and ADV_DEGREE_RX.search(blob):
        return "skip", "requires a Master's or PhD"
    years = _grad_years_mentioned(blob)
    if years and gy not in years and max(years) < gy:
        return "skip", f"only for {'/'.join(map(str, sorted(years)))} graduates"
    m = START_BEFORE_RX.search(text)
    if m:
        try:
            start_by = dt.datetime.strptime(f"1 {m.group(2)} {m.group(3)}", "%d %B %Y").date()
            if start_by < dt.date(gy, gm, 1):
                return "skip", f"must start before {m.group(2).title()} {m.group(3)}, before you graduate"
        except ValueError:
            pass
    if ANY_CLEARANCE_RX.search(blob):
        if p.get("clearance") == "no":
            return "skip", "involves a security clearance"
        if ACTIVE_CLEARANCE_RX.search(blob) and p.get("clearance") != "active":
            return "skip", "requires an active security clearance"
        notes.append("clearance needed (can be obtained)" if p.get("clearance") != "active" else "clearance role")
    if p.get("needs_sponsorship") and NO_SPONSOR_RX.search(blob):
        return "skip", "no visa sponsorship / citizens only"
    if not p.get("us_citizen", True) and CITIZEN_ONLY_RX.search(blob):
        return "skip", "US citizens only"
    yoe = c.get("min_yoe")
    if yoe is not None and yoe >= 2:
        return "skip", f"asks for {yoe}+ years of experience"
    if yoe == 1 and NON_INTERNSHIP_RX.search(text) and not NEW_GRAD_RX.search(title):
        return "skip", "asks for 1+ year of non-internship experience"
    if yoe == 1 and not p.get("internships_count_as_experience", True) and not NEW_GRAD_RX.search(blob):
        return "skip", "asks for 1+ year of experience"

    # --- new-grad check
    if not text:
        if NEW_GRAD_RX.search(title) or ENTRY_TITLE_RX.search(title + " "):
            ng = "U"
            notes.append("couldn't read posting automatically; check it yourself")
        else:
            return "skip", "couldn't read posting and title doesn't look entry-level"
    elif NEW_GRAD_RX.search(blob) or (years and gy in years):
        ng = "C"
    elif yoe in (None, 0, 1) and ENTRY_TITLE_RX.search(title + " "):
        ng = "L"
    elif yoe in (None, 0):
        ng = "L"
        notes.append("no experience requirement found; confirm level")
    else:
        ng = "L"
    if yoe == 1:
        notes.append("asks 1+ yr" + (" (internships count)" if INTERNSHIPS_COUNT_RX.search(text) else " (internships may count)"))

    # --- fit
    fams = _families(title, text)
    if set(fams) & set(p.get("never_roles", [])) and not set(fams) & set(p["primary_roles"]):
        return "skip", f"{', '.join(fams)} role (you excluded it)"
    if set(fams) & set(p["primary_roles"]):
        fit = "S"
    elif not fams or set(fams) & set(p.get("ok_roles", [])):
        fit = "G"
    else:
        fit = "P"
    skills = [s for s in p.get("skills", []) if re.search(rf"(?i)(?<![\w+#]){re.escape(s)}(?![\w+#])", text)]

    label = (", ".join(fams) or "general SWE")
    reason = [("new grad" if ng == "C" else "entry level" if ng == "L" else "title-only") + f" {label}"]
    reason += notes
    if skills:
        reason.append("mentions " + ", ".join(skills[:4]))
    reason.append(_short_pay(c))
    return ("log", ng, fit, "; ".join(reason))


def review_file(profile):
    cands = json.load(open(CANDIDATES_PATH))
    out = []
    for i, c in enumerate(cands):
        r = review_one(c, profile)
        if r[0] == "log":
            out.append({"idx": i, "decision": "log", "ng": r[1], "fit": r[2], "reason": r[3]})
        else:
            out.append({"idx": i, "decision": "skip", "reason": r[1]})
    os.makedirs(os.path.join(HERE, "state", "review"), exist_ok=True)
    path = os.path.join(HERE, "state", "review", f"auto-{dt.datetime.now():%Y%m%d-%H%M%S}.json")
    json.dump(out, open(path, "w"), indent=1)
    return path
