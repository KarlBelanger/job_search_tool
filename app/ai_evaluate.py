"""Stage 2: AI evaluation via Claude Haiku.

Reads every job that passed the deterministic filters (filters.py) but
hasn't been scored yet (dedup.get_unevaluated_candidates), sends it to
Claude Haiku alongside your profile.yaml, and asks for a FUNCTIONAL FIT
judgment — not a title match. Results are stored in the ai_evaluations
table (see dedup.py) and written out to data/scored_candidates.csv, best
match first.

This is the ONLY part of the pipeline that uses Claude — everything
upstream (fetch, filter, dedup) is free. That's the whole point of doing
filtering deterministically first: by the time a job reaches this script,
it's already passed title/location/stack screening, so the AI-scored
volume should be small.

Two backends, picked with AI_BACKEND in .env:

    AI_BACKEND=api         (default) Anthropic API via the `anthropic`
                           package. Needs ANTHROPIC_API_KEY; billed per call.
    AI_BACKEND=claude-cli  Runs each evaluation through your locally
                           installed, signed-in Claude Code CLI (`claude -p`),
                           so it uses your own Claude subscription instead of
                           API credits and counts against your plan's usage
                           limits. Personal use on your own machine only —
                           see the README's "Using your Claude subscription"
                           section. Needs `claude` on PATH (or CLAUDE_CLI_PATH)
                           and a prior `claude` login.

Usage:
    python app/ai_evaluate.py              # evaluate everything unscored
    python app/ai_evaluate.py --limit 20   # cap this run (e.g. to control cost)
    python app/ai_evaluate.py --dry-run    # show what WOULD be sent, call nothing
"""
import argparse
import csv
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import yaml
from dotenv import load_dotenv

from app import dedup
from app import filters

load_dotenv()

MODEL = os.environ.get("ANTHROPIC_MODEL", "claude-haiku-4-5")
AI_BACKEND = os.environ.get("AI_BACKEND", "api").strip().lower()
CLI_TIMEOUT_SECONDS = 300
OUTPUT_CSV = Path("data/scored_candidates.csv")

REQUIRED_EVAL_FIELDS = ["match_score", "recommendation", "genuine_gaps", "transferable_strengths", "risk_factors"]

# Field order matters here beyond documentation: Claude tends to emit tool
# JSON in roughly declaration order, and with max_tokens capped, a run of
# long free-text fields can eat the budget before later fields get
# written — which is exactly what caused a real KeyError on 'recommendation'
# in production (2026-08-11, see evaluate_one's retry logic below for the
# other half of the fix). Putting the two short/critical fields
# (match_score, recommendation) FIRST means they're very unlikely to be the
# ones lost to truncation even if a long-text field still gets cut off.
EVALUATION_SCHEMA = {
    "name": "submit_evaluation",
    "description": "Submit a structured fit evaluation for this job posting.",
    "input_schema": {
        "type": "object",
        "properties": {
            "match_score": {
                "type": "integer",
                "minimum": 0,
                "maximum": 100,
                "description": "Overall functional fit score, 0-100.",
            },
            "recommendation": {
                "type": "string",
                "enum": ["apply", "consider", "skip"],
                "description": "apply = strong functional fit, worth the effort. consider = plausible but real gaps/risk. skip = not a genuine fit despite passing keyword filters.",
            },
            "genuine_gaps": {
                "type": "string",
                "description": "Real, specific gaps between the candidate's experience and this role's requirements. Be honest — don't invent gaps to seem balanced, and don't paper over real ones. Keep to 2-3 sentences.",
            },
            "transferable_strengths": {
                "type": "string",
                "description": "Which of the candidate's competencies/evidence genuinely transfer to this role, and why — cite specifics from their profile, not generic claims. Keep to 2-3 sentences.",
            },
            "risk_factors": {
                "type": "string",
                "description": "Non-skill risks: seniority mismatch, domain mismatch, likely comp mismatch, stack dealbreakers the deterministic filter might have missed, company-stage risk given the candidate's stated preferences, etc. Keep to 2-3 sentences.",
            },
        },
        "required": REQUIRED_EVAL_FIELDS,
    },
}

SYSTEM_PROMPT = """You are evaluating job postings for FUNCTIONAL FIT against a candidate's real \
experience — not title matching, not keyword matching. The candidate's profile is organized by \
competency (what they've actually done), not by job title, specifically so you judge whether their \
demonstrated capabilities transfer to this role's actual responsibilities.

Be honest and specific, not diplomatic. A generic "great candidate!" evaluation is useless — the \
candidate needs real signal on whether to spend an application on this. If the role is a stretch, \
say so and say why. If there's a real gap, name it precisely rather than softening it. Cite \
specific evidence from their profile when claiming a strength transfers; don't just assert \
seniority-level fit in the abstract."""


def load_profile(path: str = "profile.yaml") -> dict:
    with open(path) as f:
        return yaml.safe_load(f)


# aggregator_clients.fetch_adzuna now tries to fetch the FULL job posting
# (see fetch_full_description there) instead of settling for Adzuna's short
# API snippet — that's the real fix for 2026-08-11's "Adzuna rows scoring
# low because the description was cut off mid-sentence" problem. This flag
# is the fallback for the cases that full-JD fetch still can't recover
# (site blocked scraping, dead redirect, genuinely short posting): tell the
# model explicitly rather than let a thin description read as "no
# responsibilities listed" and quietly tank match_score.
def build_user_prompt(profile: dict, job: dict) -> str:
    raw_description = job.get("description", "")
    description = filters.strip_html(raw_description).strip() or "(no JD text available)"
    partial_note = ""
    if raw_description and filters.looks_truncated(raw_description):
        partial_note = (
            "\nNOTE: this description looks like a short snippet, not the full posting — it may "
            "have been cut off mid-sentence. Do NOT lower match_score or invent genuine_gaps just "
            "because a responsibility/requirement isn't mentioned here; judge fit on title, "
            "company, location, and whatever specifics ARE present. If the snippet is too thin to "
            "say anything meaningful about stack or seniority, say so in genuine_gaps rather than "
            "guessing.\n"
        )
    return f"""CANDIDATE PROFILE:
{yaml.dump(profile, sort_keys=False, allow_unicode=True)}

---

JOB POSTING TO EVALUATE:
Company: {job['company']}
Title: {job['title']}
Location: {job['location']}
URL: {job['url']}
{partial_note}
Description:
{description}

---

Call submit_evaluation with your structured assessment."""


def _extract_tool_input(resp) -> dict | None:
    for block in resp.content:
        if block.type == "tool_use" and block.name == "submit_evaluation":
            return block.input
    return None


def _missing_fields(evaluation: dict) -> list[str]:
    return [f for f in REQUIRED_EVAL_FIELDS if f not in evaluation]


class ClaudeCliClient:
    """Stand-in for anthropic.Anthropic that runs each evaluation through
    the Claude Code CLI in non-interactive mode (`claude -p`), which
    authenticates with whatever the user is signed in to — their own
    Claude subscription, if they logged in with a claude.ai account.

    The call is locked down to a plain question-and-answer: --tools ""
    gives the model no tools at all (it can't read or change files),
    --safe-mode skips CLAUDE.md, hooks, skills, plugins and MCP servers so
    nothing from the user's own Claude Code setup leaks into the prompt,
    and it runs from a temp directory. --json-schema makes the CLI return
    the evaluation in `structured_output`.

    Deliberately NOT --bare: bare mode ignores subscription login and only
    accepts an API key, which defeats the point of this backend."""

    def __init__(self, cli_path: str, model: str, timeout: int = CLI_TIMEOUT_SECONDS):
        self.cli_path = cli_path
        self.model = model
        self.timeout = timeout

    def build_command(self) -> list[str]:
        return [
            self.cli_path, "-p",
            "--model", self.model,
            "--system-prompt", SYSTEM_PROMPT,
            "--tools", "",
            "--safe-mode",
            "--no-session-persistence",
            "--output-format", "json",
            "--json-schema", json.dumps(EVALUATION_SCHEMA["input_schema"]),
        ]

    def evaluate(self, user_content: str) -> dict | None:
        """Returns the structured evaluation, or None if the CLI answered
        without one. Raises RuntimeError if the CLI itself failed (not
        signed in, usage limit reached, timeout...)."""
        try:
            proc = subprocess.run(
                self.build_command(),
                input=user_content,
                capture_output=True,
                text=True,
                encoding="utf-8",
                timeout=self.timeout,
                cwd=tempfile.gettempdir(),
            )
        except subprocess.TimeoutExpired:
            raise RuntimeError(f"claude CLI timed out after {self.timeout}s")
        try:
            data = json.loads(proc.stdout)
        except json.JSONDecodeError:
            detail = (proc.stderr or proc.stdout or "").strip()[:500]
            raise RuntimeError(f"claude CLI exited {proc.returncode} without JSON output: {detail}")
        if data.get("is_error") or proc.returncode != 0:
            raise RuntimeError(f"claude CLI reported an error: {str(data.get('result', data))[:500]}")
        return data.get("structured_output")


def _call_model(client, user_content: str, max_tokens: int) -> tuple[dict | None, str]:
    """One model call via whichever backend `client` is. Returns the
    evaluation dict (or None if the model didn't produce one) plus a
    stop_reason for error messages."""
    if isinstance(client, ClaudeCliClient):
        return client.evaluate(user_content), "no structured_output"
    resp = client.messages.create(
        model=MODEL,
        max_tokens=max_tokens,
        system=SYSTEM_PROMPT,
        tools=[EVALUATION_SCHEMA],
        tool_choice={"type": "tool", "name": "submit_evaluation"},
        messages=[{"role": "user", "content": user_content}],
    )
    return _extract_tool_input(resp), getattr(resp, "stop_reason", "unknown")


def evaluate_one(client, profile: dict, job: dict, max_retries: int = 1) -> dict:
    """Calls the model and validates the tool response has every required
    field. A forced tool_choice on a smaller model can still emit a
    truncated/incomplete JSON object (this happened in production on
    2026-08-11 — see EVALUATION_SCHEMA's comment) if max_tokens is hit
    mid-generation; retry once with a bump to max_tokens before giving up,
    rather than crashing the whole run on one bad response. `client` is an
    anthropic.Anthropic or a ClaudeCliClient (see make_client)."""
    user_content = build_user_prompt(profile, job)
    max_tokens = 1536

    for attempt in range(max_retries + 1):
        evaluation, stop_reason = _call_model(client, user_content, max_tokens)
        if evaluation is None:
            if attempt < max_retries:
                max_tokens += 512  # give the retry more room in case it was truncation
                continue
            raise RuntimeError(f"Model didn't call submit_evaluation for {job['url']} "
                                f"(stop_reason={stop_reason})")

        missing = _missing_fields(evaluation)
        if not missing:
            return evaluation
        if attempt < max_retries:
            max_tokens += 512
            continue
        raise RuntimeError(f"Model's response for {job['url']} is missing required field(s) "
                            f"{missing} after {max_retries + 1} attempt(s): {evaluation}")


def make_client(backend: str = AI_BACKEND):
    """Builds the client for the configured backend, or exits with a
    setup message if it can't."""
    if backend == "claude-cli":
        cli_path = os.environ.get("CLAUDE_CLI_PATH") or shutil.which("claude")
        if not cli_path:
            sys.exit("AI_BACKEND=claude-cli but the `claude` command wasn't found. Install Claude Code "
                     "and run `claude` once to sign in, or set CLAUDE_CLI_PATH.")
        return ClaudeCliClient(cli_path, MODEL)
    if backend != "api":
        sys.exit(f"Unknown AI_BACKEND '{backend}' — use 'api' or 'claude-cli'.")

    try:
        import anthropic
    except ImportError:
        sys.exit("Missing dependency: pip install anthropic")
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        sys.exit("ANTHROPIC_API_KEY env var not set (or set AI_BACKEND=claude-cli to use your "
                 "Claude subscription instead).")
    return anthropic.Anthropic(api_key=api_key)


def write_csv(conn, path: Path = OUTPUT_CSV) -> int:
    path.parent.mkdir(parents=True, exist_ok=True)
    rows = list(dedup.iter_scored_candidates(conn))
    with open(path, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=[
            "match_score", "recommendation", "company", "title", "location",
            "transferable_strengths", "genuine_gaps", "risk_factors", "url", "posted_at",
        ])
        writer.writeheader()
        for row in rows:
            writer.writerow(row)
    return len(rows)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, default=None, help="Max number of jobs to evaluate this run")
    parser.add_argument("--dry-run", action="store_true", help="Show what would be evaluated, call no API")
    args = parser.parse_args()

    profile = load_profile()

    with dedup.connect() as conn:
        queue = dedup.get_unevaluated_candidates(conn)
        if args.limit:
            queue = queue[: args.limit]

        print(f"{len(queue)} candidate(s) queued for AI evaluation.")
        if not queue:
            sys.exit(0)

        if args.dry_run:
            for job in queue:
                print(f"WOULD EVALUATE | {job['company']:20s} | {job['title']}")
            sys.exit(0)

        client = make_client()
        print(f"Scoring with {MODEL} via the {AI_BACKEND} backend.")

        for i, job in enumerate(queue, 1):
            try:
                evaluation = evaluate_one(client, profile, job)
            except Exception as e:
                print(f"[WARN] {job['company']} — {job['title']}: evaluation failed — {e}", file=sys.stderr)
                continue
            dedup.save_evaluation(conn, job["url"], evaluation, MODEL)
            conn.commit()  # commit per-job so a crash mid-run doesn't lose completed evaluations
            print(f"[{i}/{len(queue)}] {evaluation['match_score']:3d} {evaluation['recommendation']:9s} | "
                  f"{job['company']:20s} | {job['title']}")

        total = write_csv(conn)
        print(f"\nWrote {total} scored candidates to {OUTPUT_CSV} (sorted by match_score desc).")