"""Tests for ai_evaluate.py's claude-cli backend (AI_BACKEND=claude-cli),
with subprocess.run mocked out — nothing here calls the real CLI or uses
any of your plan's usage. Run with: python -m pytest app/tests/test_ai_backend.py
"""
import json
import subprocess
from unittest.mock import MagicMock, patch

import pytest

from app import ai_evaluate

JOB = {
    "company": "Acme",
    "title": "Junior DevOps Engineer",
    "location": "Baltimore, MD",
    "url": "https://example.com/jobs/1",
    "description": "Linux, Docker and AWS. " * 30,
}
PROFILE = {"identity": {"name": "Test Candidate"}}
EVALUATION = {
    "match_score": 74,
    "recommendation": "apply",
    "genuine_gaps": "No production Kubernetes.",
    "transferable_strengths": "Home-lab Linux and Docker administration.",
    "risk_factors": "None significant.",
}


def _completed(stdout: str, returncode: int = 0, stderr: str = "") -> MagicMock:
    proc = MagicMock()
    proc.stdout, proc.returncode, proc.stderr = stdout, returncode, stderr
    return proc


def _cli_output(structured_output, is_error=False, result="") -> str:
    return json.dumps({"type": "result", "is_error": is_error, "result": result,
                       "structured_output": structured_output})


def test_command_is_locked_down():
    """No tools, no user customizations, and never --bare (bare mode
    ignores subscription login)."""
    cmd = ai_evaluate.ClaudeCliClient("claude", "claude-haiku-4-5").build_command()
    assert cmd[:2] == ["claude", "-p"]
    assert cmd[cmd.index("--tools") + 1] == ""
    assert "--safe-mode" in cmd
    assert "--no-session-persistence" in cmd
    assert "--bare" not in cmd
    assert cmd[cmd.index("--model") + 1] == "claude-haiku-4-5"
    assert cmd[cmd.index("--output-format") + 1] == "json"
    schema = json.loads(cmd[cmd.index("--json-schema") + 1])
    assert schema["required"] == ai_evaluate.REQUIRED_EVAL_FIELDS


def test_evaluate_one_via_cli_returns_structured_output():
    client = ai_evaluate.ClaudeCliClient("claude", "claude-haiku-4-5")
    with patch("subprocess.run", return_value=_completed(_cli_output(EVALUATION))) as mock_run:
        result = ai_evaluate.evaluate_one(client, PROFILE, JOB)
    assert result == EVALUATION
    # The job and profile go in on stdin, not on the command line.
    assert "Junior DevOps Engineer" in mock_run.call_args.kwargs["input"]
    assert "Junior DevOps Engineer" not in " ".join(mock_run.call_args.args[0])


def test_missing_fields_retry_then_succeed():
    client = ai_evaluate.ClaudeCliClient("claude", "claude-haiku-4-5")
    partial = {"match_score": 50}
    with patch("subprocess.run", side_effect=[_completed(_cli_output(partial)),
                                              _completed(_cli_output(EVALUATION))]) as mock_run:
        result = ai_evaluate.evaluate_one(client, PROFILE, JOB)
    assert result == EVALUATION
    assert mock_run.call_count == 2


def test_cli_error_is_raised_with_its_message():
    client = ai_evaluate.ClaudeCliClient("claude", "claude-haiku-4-5")
    out = _cli_output(None, is_error=True, result="Not logged in · Please run /login")
    with patch("subprocess.run", return_value=_completed(out, returncode=1)):
        with pytest.raises(RuntimeError, match="Not logged in"):
            ai_evaluate.evaluate_one(client, PROFILE, JOB)


def test_non_json_output_and_timeout_raise():
    client = ai_evaluate.ClaudeCliClient("claude", "claude-haiku-4-5", timeout=1)
    with patch("subprocess.run", return_value=_completed("", returncode=1, stderr="boom")):
        with pytest.raises(RuntimeError, match="boom"):
            client.evaluate("prompt")
    with patch("subprocess.run", side_effect=subprocess.TimeoutExpired("claude", 1)):
        with pytest.raises(RuntimeError, match="timed out"):
            client.evaluate("prompt")


def test_make_client_picks_backend():
    with patch("shutil.which", return_value="/usr/bin/claude"), \
         patch.dict("os.environ", {}, clear=False) as env:
        env.pop("CLAUDE_CLI_PATH", None)
        client = ai_evaluate.make_client("claude-cli")
    assert isinstance(client, ai_evaluate.ClaudeCliClient)
    assert client.cli_path == "/usr/bin/claude"

    with patch("shutil.which", return_value=None), patch.dict("os.environ", {}, clear=False) as env:
        env.pop("CLAUDE_CLI_PATH", None)
        with pytest.raises(SystemExit):
            ai_evaluate.make_client("claude-cli")

    with pytest.raises(SystemExit):
        ai_evaluate.make_client("something-else")
