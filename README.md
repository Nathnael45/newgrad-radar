# newgrad-radar

Finds **fresh new-grad software engineering jobs that actually fit you**, and checks each posting's
description to confirm it's really new-grad before showing it to you.

The big GitHub job lists are great, but they mix true new-grad roles with "Software Engineer" postings that quietly
want 3 years of experience, roles only open to last year's grads, Master's-only roles, jobs that need an active
security clearance, and duplicates of the same job from four different lists. newgrad-radar reads the actual job
descriptions and filters all of that out based on a short survey about you.

```
python3 survey.py        # 2 minutes of questions -> profile.json
python3 radar.py run     # -> output/LATEST.md, output/jobs.csv (and optionally Notion)
```

No dependencies beyond Python 3.9+.

## What it does

1. **Collects** listings from:
   - [speedyapply](https://github.com/speedyapply), [SimplifyJobs](https://github.com/SimplifyJobs/New-Grad-Positions),
     [vanshb03](https://github.com/vanshb03) and (optional) jobright new-grad lists
   - ~90 companies' own job boards via public Greenhouse / Lever / Ashby feeds (`data/companies.json`), so new
     postings show up before the lists pick them up
2. **Filters** on list data: posting age, your locations, seniority words in the title, roles you never want, grad
   year in the title, pay floors, non-US locations, and duplicates (same job, different link or list).
3. **Reads each job description** (Greenhouse, Lever, Ashby, Workday, SmartRecruiters, Workable, schema.org JobPosting,
   and plain pages) and pulls out pay, locations, experience requirements and grad-date wording.
4. **Reviews** every remaining job against your profile and either logs it with:
   - **New grad check**: *Confirmed new grad* (the posting says so), *Likely entry-level* (no real experience bar),
     or *Unverified* (the page couldn't be read automatically, but the title looks entry-level)
   - **Fit**: *Strong* (one of your top role types), *Good*, or *Partial*
   - **Why**: e.g. `new grad backend; asks 1+ yr (internships count); mentions python, aws; $130-180k`

   or skips it with a reason, such as *only for 2025/2026 graduates*, *asks for 2+ years*, *requires a Master's*,
   *requires an active clearance*, *must start before you graduate*, or *no visa sponsorship*.
5. **Publishes** new matches. ★ **Best matches** (strong fit in one of your main areas) are bolded and listed first.

Everything it has seen is remembered in `state/seen.json`, so each run only shows **new** jobs.
`state/rejected.csv` explains every job it filtered out, if you want to check its work.

## The survey

`python3 survey.py` asks about:

| Topic | Examples |
|---|---|
| You | graduation year/month, degree, whether internships should count toward "1 year of experience" |
| Roles | most wanted (e.g. backend, full-stack), OK (infra, data, ML…), never (embedded, QA, mobile…), your skills |
| Locations | main areas, lower-priority areas, and whether to show top-tier companies elsewhere (23 US metros + Remote) |
| Pay | minimum base salary, with optional per-area minimums |
| Work authorization | visa sponsorship needs, US citizenship, security clearance (have / willing / no) |
| Freshness & sources | only jobs posted in the last N days; which lists and boards to use |
| Output | CSV + Markdown always; Notion optional |

Re-run it any time; it starts from your current answers. `python3 survey.py --show` prints your profile.
Your profile stays local (`profile.json` is git-ignored).

## Output

- `output/LATEST.md`: this run's new matches, best matches first, with apply links
- `output/jobs.csv`: every match ever logged (open in Excel or Google Sheets and add your own columns)

### Notion (optional)

1. Create an internal integration at <https://www.notion.so/my-integrations> and copy its secret.
2. `export NOTION_TOKEN=secret_...` (put it in your shell profile, never in the repo).
3. In Notion, open the page that should hold the tracker, then **••• → Connections → add your integration**.
4. `python3 radar.py notion-setup <that page's URL>`, which creates the database and saves its id to `profile.json`.

Rows come in with Status = *To apply*, a ★ *Best match* checkbox, a bold title for best matches, and a *Posted*
date. Suggested views: "Posted within past week, sorted by Added (newest first)", plus a board grouped by Status.

## Running it automatically

**cron (macOS/Linux)**, e.g. every 2 hours from 9 to 5 on weekdays:

```
0 9-17/2 * * 1-5  cd /path/to/newgrad-radar && /usr/bin/python3 radar.py run >> state/cron.log 2>&1
```

On macOS, cron needs Full Disk Access for the folder you use; a launchd job or any scheduler works too.
Each run takes about 1–3 minutes.

**With an AI agent:** the rule-based reviewer is good, but an agent judges borderline postings better and can open
pages that need a real browser. See [AGENT_RUNBOOK.md](AGENT_RUNBOOK.md); it works with Claude Code scheduled tasks or
any agent that can run shell commands.

## Commands

| Command | What it does |
|---|---|
| `radar.py run` | collect → review → publish |
| `radar.py collect` | scrape, filter and read job descriptions into `state/candidates.json` |
| `radar.py show [start end]` | print candidates compactly |
| `radar.py review` | rule-based review → `state/review/auto-*.json` |
| `radar.py publish FILE` | publish a decisions file (`.json` from `review`, or an agent's `.txt`) |
| `radar.py notion-setup PAGE` | create the Notion database |
| `radar.py stats` | counts of logged / skipped / rejected / duplicate jobs |

## Customizing

- `data/regions.json`: metro areas (cities per state). Add your own area and it shows up in the survey.
- `data/companies.json`: the "top-tier" list (used for jobs outside your areas) and the company job-board watchlist.
  To watch a company, add `{"company": "Name", "ats": "greenhouse|lever|ashby", "board": "<slug>"}`, where the slug is
  the part after `job-boards.greenhouse.io/`, `jobs.lever.co/` or `jobs.ashbyhq.com/` in its job links.
- `data/sources.json`: list sources; `{year}` is filled with your graduation year.

## Being a good citizen

- It only reads public pages and public job-board feeds; nothing behind a login.
- Job-board APIs are queried once per company per run. jobright (off by default) is read one page every 8 seconds,
  capped at 20 pages per run, and the run backs off on its own if the site asks it to slow down.
- Please don't crank the schedule to every few minutes. Hourly is plenty; new postings don't appear that fast.
- It never applies to anything. Applying is up to you.

## Limitations

- The rule-based reviewer reads wording, so unusual postings can slip through either way. Each match says *why*
  it was logged, and `state/rejected.csv` / `state/review/` say why things were dropped.
- Pages that need JavaScript (some Workday, Oracle, Tesla, Apple pages) can't be read; those are logged as
  *Unverified* when the title looks entry-level.
- Pay floors only filter jobs that list pay; unlisted pay is kept unless you turn that off in the survey.

## License

MIT
