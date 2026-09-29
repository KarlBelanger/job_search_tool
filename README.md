# AI-Powered Job Search Automation


An automated pipeline for a targeted job search: fetch open roles directly
from companies' ATS APIs and job aggregators, filter out anything
irrelevant, deduplicate against everything already seen, optionally score
each candidate's fit against your own experience with Claude, and review
the results in a local web board.

Nothing here talks to any service other than the job sources you configure
and (for the optional AI step) Claude, via either the Anthropic API or
your own signed-in Claude Code CLI. All state — scraped
jobs, scores, your own notes — lives in a local SQLite database; nothing
is sent to a third party beyond fetching the postings themselves.

<img width="2532" height="1215" alt="image" src="https://github.com/user-attachments/assets/679cea89-3156-4e46-af3c-74d3e0d9b30c" />

## How it works

1. **Fetch** (`app/main.py`) — pulls open roles from:
   - `companies.yaml` — known companies queried directly via their ATS
     (Greenhouse, Ashby, Workable, Lever, SmartRecruiters) — precise, low
     noise.
   - `aggregators.yaml` — broad keyword+location search across many
     employers at once (Adzuna, Remotive) — wider reach, more noise.
2. **Filter** (`app/filters.py`) — drops anything that isn't an
   engineering-shaped title, isn't in an allowed location, or whose JD
   requires a stack you've excluded.
3. **Dedup** (`app/dedup.py`) — everything fetched is stored in a local
   SQLite database (`data/seen_jobs.sqlite3`), keyed by URL and by
   normalized company+title, so re-runs only ever surface genuinely new
   postings.
4. **AI evaluation** (`app/ai_evaluate.py`, optional; API credits or your
   Claude subscription's usage) — sends
   each filtered candidate plus your `profile.yaml` to Claude, which scores
   fit (0-100) and returns concrete gaps, transferable strengths, risk
   factors, and an apply/consider/skip recommendation.
5. **Review** (`web/`) — a local Next.js app reading/writing the same
   SQLite database directly, for browsing results and tracking your own
   `applied / interview / rejected / skipped / silence` status and notes.

## Setup

```bash
pip install -r requirements.txt
cp .env.example .env
# fill in .env with the keys you need — see the comments in that file
```

Create your experience profile from the template (this file is gitignored
— it's never committed, since it holds your personal experience):

```bash
cp profile.example.yaml profile.yaml
# then edit profile.yaml with your own background
```

`profile.example.yaml` is a blank template — it tells you the shape but
not the bar. For a fully worked (fictional) example showing the level of
specificity/quantification each evidence bullet should actually have, see
[`profile.sample.yaml`](profile.sample.yaml).

`ANTHROPIC_API_KEY` is only needed for the AI evaluation step, and only
with the default `AI_BACKEND=api` (see "Using your Claude subscription"
below for the alternative). Adzuna
(`ADZUNA_APP_ID`/`ADZUNA_APP_KEY`) is only needed if you keep an Adzuna
entry in `aggregators.yaml` — register a free key at
[developer.adzuna.com](https://developer.adzuna.com). Remotive needs no
auth but only covers remote roles.

### Customize for your own search

This fork is configured for a **junior / entry-level DevOps, Linux and
cloud infrastructure search** in the Baltimore / Washington DC / Northern
Virginia area plus US-remote, with a narrow accessibility-testing track.
To retarget it, edit:

1. **`filters.yaml`** — target titles (`priority_title_keywords`,
   `title_allow_keywords`), the junior-only `seniority_exclude_keywords`,
   `location_allow_patterns`, the infra `stack_core`, and the JD checks:
   `clearance_patterns` (drops any role mentioning a security clearance),
   `required_cert_patterns` (drops roles that *require* an accessibility
   certification; "preferred" is fine) and `max_required_years_experience`
   (drops roles requiring 5+ years). If nothing you fetch ever matches
   `location_allow_patterns`, every job gets rejected.
2. **`aggregators.yaml`** — Adzuna searches (`country: us`, keywords,
   `where` + `distance`) and Remotive categories.
3. **`companies.yaml`** — the company registry. Still the original
   author's list of Canadian/remote tech employers; the location filter
   keeps only their US-remote and DC-area roles. Add companies you're
   actually targeting (see the file's header comment for the format).

## Usage

### 1. Fetch + filter (free)

```bash
python -m app.main                    # all companies + aggregators
python -m app.main --company affirm   # just one company, for debugging a fetcher
python -m app.main --skip-aggregators # companies.yaml only
python -m app.main --skip-companies   # aggregators.yaml only

make run                              # same thing, interactive prompts instead of flags
```

Output: `data/candidates.csv`, appended to on every run.

### 2. AI evaluation (optional)

```bash
python -m app.ai_evaluate --dry-run   # see what's queued, no API calls, no cost
python -m app.ai_evaluate --limit 20  # score just 20, to sanity-check quality/cost first
python -m app.ai_evaluate             # score everything unscored

make evaluate                         # interactive prompts instead of flags
```

Results are stored in SQLite (so re-running never re-pays for a job
already scored) and written to `data/scored_candidates.csv`, sorted
best-match-first.

### 3. Review results

```bash
cd web && npm install    # first time only
cd ..
make web                              # starts the board at http://localhost:3000
```

Reads/writes `data/seen_jobs.sqlite3` directly — no export/import step.
See [`web/README.md`](web/README.md) for details (custom `DB_PATH`,
production build, etc).

## Makefile commands

Thin wrappers over the commands above — run from the repo root:

| Command        | Equivalent to                    | What it does |
|-----------------|----------------------------------|--------------|
| `make run`      | `python -m app.main`             | Fetch + filter (step 1). Interactively asks whether to skip companies/aggregators/discovery and whether to limit to one company slug, instead of you remembering the flags. |
| `make evaluate` | `python -m app.ai_evaluate`       | AI evaluation (step 2). Interactively asks for `--dry-run` and an optional `--limit`. |
| `make web`      | `npm --prefix web run dev`       | Starts the Next.js review board, with `DB_PATH` already pointed at `data/seen_jobs.sqlite3`. |
| `make test`     | `pytest app/` + the standalone sanity-check scripts | Runs the full test suite (see the Tests section below). |

`make` with no target runs `make run` (the default goal).

### Using your Claude subscription instead of API credits

Set `AI_BACKEND=claude-cli` in `.env` to score jobs through your locally
installed Claude Code CLI (`claude -p`) instead of the Anthropic API. It
uses whatever account `claude` is signed in to, so with a Pro/Max login
there are no per-call API charges; each job scored counts against your
plan's usage limits instead, so keep runs to tens of jobs with `--limit`.

Setup: install Claude Code, run `claude` once and sign in with your
claude.ai account. `claude auth status` should show `"authMethod":
"claude.ai"`. If `claude` isn't on your PATH, set `CLAUDE_CLI_PATH`.

Each call runs with no tools (`--tools ""`), without your CLAUDE.md,
hooks, skills, plugins or MCP servers (`--safe-mode`), and with the same
system prompt and output schema as the API backend. It deliberately does
not use `--bare`, which only accepts an API key.

This is meant for personal use on your own machine. Anthropic's
[Claude Code legal and compliance page](https://code.claude.com/docs/en/legal-and-compliance)
says subscription login is for ordinary use of Claude Code and that
third-party developers may not route requests through Free/Pro/Max
credentials on behalf of their users; it also says that doesn't prevent
an end user from signing in to the unmodified Claude Code binary with
their own subscription. Don't host this for other people on your login.

## Configuration

- **`companies.yaml`** — the company registry (name, ATS type, slug). Add
  a company here once you've identified its ATS.
- **`aggregators.yaml`** — aggregator search config (keywords, location,
  pagination limits).
- **`filters.yaml`** — title allowlist/exclusion keywords, location
  allowlist patterns, and JD stack-dealbreaker/core-stack keywords. Edit
  this directly as you refine what counts as in-scope for you — no code
  changes needed (matching logic lives in `app/filters.py`).
- **`profile.yaml`** — your experience profile fed to the AI evaluation
  step (see `profile.example.yaml` for the blank template and
  `profile.sample.yaml` for a fully worked example).

## Project layout

```
app/
  main.py                 orchestrates fetch -> filter -> dedup -> candidates.csv
  ats_clients.py           one fetch function per ATS
  aggregator_clients.py    one fetch function per aggregator
  filters.py               loads and applies filters.yaml's rules
  dedup.py                 SQLite store (seen_jobs, job_details)
  discover_companies.py    auto-appends newly-resolved companies to companies.yaml
  ai_evaluate.py           stage 2: Claude-based fit scoring
  inspect_job.py           CLI to look up a stored job or list recent rejections
  refilter.py              re-runs current filters.py against already-fetched jobs
  scripts/                 one-off diagnostic/maintenance scripts, not part of the pipeline
  tests/                   pytest + standalone sanity-check scripts
web/                       Next.js review board (see web/README.md)
companies.yaml             company -> ATS registry
aggregators.yaml           aggregator search config
filters.yaml               title/location/stack filter rules
profile.example.yaml       blank template for profile.yaml (your real profile, gitignored)
profile.sample.yaml        fully worked (fictional) example of a filled-in profile
```

## Tests

```bash
make test
```

Runs the pytest suite plus the standalone sanity-check scripts
(`test_pipeline.py`, `test_discover_companies.py`, `test_ai_evaluate.py`).
None of them call live external APIs.

## Notes on scope

- ATS fetchers for Greenhouse, Ashby, Workable, and Lever were each
  validated against real live responses. SmartRecruiters support is built
  from documentation and third-party corroboration only — verify a new
  SmartRecruiters company with `python -m app.main --company <slug>`
  before trusting it in a real run.
- If a particular company's fetch fails, `main.py` logs a warning and
  continues with the rest rather than crashing the whole run.

## Possible next steps

- Workday support (`clio.wd3.myworkdayjobs.com`-style boards) — its
  job-search API is POST-based with a different pagination shape than the
  ATSes currently supported.
- Company-name normalization for aggregator-sourced dedup (the same
  posting sometimes comes back under slightly different company name
  strings, e.g. "Acme Corp" vs. "Acme").
- A digest output (email/Sheet) beyond the current CSV/SQLite/web-board
  review flow.
- Scheduling: once you're happy with a full local run, a daily cron entry
  like `0 7 * * * cd /path/to/job_search_pipeline && python -m app.main && python -m app.ai_evaluate`.

## License

[MIT](LICENSE)
