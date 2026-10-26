# Agent runbook

Use this when an AI agent (e.g. a Claude Code scheduled task) reviews jobs instead of the built-in rule-based
reviewer. The agent's judgment replaces only the review step; collecting and publishing stay the same.

Never apply to jobs or submit any form.

## Steps

1. `cd` to this folder. Read `profile.json` (run `python3 survey.py --show`). It holds the person's graduation date,
   role preferences (primary / ok / never), skills, main and secondary areas, pay floors, sponsorship needs and
   clearance stance. Judge everything below against it.
2. `python3 radar.py collect`. If it reports 0 candidates, stop and say "no new jobs".
3. `python3 radar.py show` to see each candidate with key lines from its job description. For candidates with no
   lines (the page needs a browser), open the link in a browser tool if you have one and read the posting. Don't
   try to get past bot checks, CAPTCHAs or logins; treat those as unreadable.
4. Decide each candidate:
   - **Log** if the posting explicitly targets new grads / the person's graduation year (`C`), or is entry-level and any
     experience ask could be met with internships (`L`). Use `U` if you couldn't read it but the title is clearly
     entry-level.
   - **Skip** if it needs 2+ years, or 1+ year that explicitly excludes internships; needs a Master's/PhD the person
     won't have; is only for earlier graduation years or must start before they graduate; needs an *active* clearance
     they don't have (or any clearance if they said no); won't sponsor when they need sponsorship; is an internship or
     temporary role; is a role type in their "never" list; is below their pay floor; or is a duplicate of one already
     handled under another link.
   - **Fit**: `S` = one of their primary role types, `G` = acceptable/general SWE, `P` = adjacent.
5. Write decisions to `state/review/<YYYY-MM-DD-HHMM>.txt`, one line per candidate index:
   ```
   12|L|C|S|new grad backend; 0-2 yrs; NYC hybrid; $140-160k
   13|S|requires 3+ years
   ```
   Keep reasons short and concrete: pay range, caveats like clearance or start date.
6. `python3 radar.py publish state/review/<file>.txt`. That writes `output/LATEST.md` and `output/jobs.csv`, sends rows to
   Notion if enabled, and marks every candidate as seen.
7. Summarize: how many jobs were logged, the ★ best matches (company, role, area), and any deadlines in the next week.
