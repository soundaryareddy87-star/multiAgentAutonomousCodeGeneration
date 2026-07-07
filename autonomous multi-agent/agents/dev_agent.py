"""
agents/dev_agent.py — Developer Agent node.

Responsibility:
  - Receives the structured PM requirements + test scenarios.
  - Calls AWS Bedrock to generate Python source code that satisfies the spec.
  - Writes the code to tmp_sandbox/generated_code.py.
  - Runs pytest via subprocess (self-healing loop: error logs fed back to LLM).
  - Increments retry_count on failure; sets test_passed=False to trigger re-routing.

Production note: the subprocess execution happens locally in this PoC.
In production, swap the subprocess call with an AWS Fargate ephemeral task
or a Docker container launch via the MCP Server.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import textwrap
from pathlib import Path
from typing import TYPE_CHECKING, Any

import boto3

from observability import build_audit_entry, end_span, start_span

if TYPE_CHECKING:
    from state import AgentState

# ---------------------------------------------------------------------------
# Sandbox directory — local for PoC, Fargate/Docker in production
# NOTE: Production swaps this with ephemeral Docker/AWS Fargate containers
#       for strict security isolation.
# ---------------------------------------------------------------------------
SANDBOX_DIR = Path(__file__).parent.parent / "tmp_sandbox"
PM_SPEC_FILE = SANDBOX_DIR / "pm_spec.json"

_SCENARIO_ASSERT_RE = re.compile(
    r"^(test_\w+):\s*Assert that (\w+)\((.*)\)\s+returns\s+(True|False)\b",
    re.IGNORECASE,
)
_SCENARIO_VALIDATES_QUOTED_RE = re.compile(
    r"^(test_\w+):\s*Validates that '([^']*)' returns (True|False)\b",
    re.IGNORECASE,
)
_SCENARIO_VALIDATES_EMPTY_RE = re.compile(
    r"^(test_\w+):\s*Validates that empty string '' returns (True|False)\b",
    re.IGNORECASE,
)
_SCENARIO_VALIDATES_NONE_RE = re.compile(
    r"^(test_\w+):\s*Validates that None input returns (True|False)\b",
    re.IGNORECASE,
)
_SCENARIO_VALIDATES_TOO_LONG_RE = re.compile(
    r"^(test_\w+):\s*Validates that an email with 255\+ characters returns (True|False)\b",
    re.IGNORECASE,
)
_SCENARIO_VALIDATES_MAX_LEN_RE = re.compile(
    r"^(test_\w+):\s*Validates that an email with exactly 254 characters returns (True|False)\b",
    re.IGNORECASE,
)


def _bedrock_text(system_text: str, user_text: str, max_tokens: int = 4096) -> str:
    region = (os.environ.get("AWS_REGION") or "us-east-1").strip()
    model_id = (os.environ.get("BEDROCK_MODEL_ID") or "anthropic.claude-3-sonnet-20240229-v1:0").strip()
    runtime = boto3.client("bedrock-runtime", region_name=region)
    response = runtime.converse(
        modelId=model_id,
        messages=[{"role": "user", "content": [{"text": user_text}]}],
        system=[{"text": system_text}],
        inferenceConfig={"temperature": 0.2, "maxTokens": max_tokens},
    )
    content = response.get("output", {}).get("message", {}).get("content", [])
    return "".join(part.get("text", "") for part in content if isinstance(part, dict))


def _extract_code_block(text: str) -> str:
    """Extract content between ```python ... ``` fences, or return raw text."""
    import re
    pattern = re.compile(r"```(?:python)?\s*(.*?)```", re.DOTALL)
    match = pattern.search(text)
    if match:
        return match.group(1).strip()
    return text.strip()


# ---------------------------------------------------------------------------
# System prompt
# ---------------------------------------------------------------------------

_DEV_SYSTEM_PROMPT = """\
You are an expert Python software engineer.
Your task is to write production-quality Python code that satisfies a given specification.

Rules:
1. Output ONLY the Python source code — no prose, no explanations.
2. Wrap code in ```python ... ``` fences.
3. Include all imports at the top.
4. Write clean, idiomatic Python 3.11+.
5. Do NOT include hardcoded secrets, passwords, or credentials.
6. Add brief docstrings and inline comments for clarity.
7. If prior error logs are provided, fix the specific issues described.
"""

_DEV_RETRY_SYSTEM_PROMPT = """\
You are an expert Python software engineer performing a bug fix.
You will be given:
  - The original specification
  - Your previous code attempt
  - The exact error/failure log from running pytest

Fix ALL issues shown in the error log. Output ONLY the corrected Python source code.
Wrap code in ```python ... ``` fences. Do NOT repeat the error — just fix it.
"""


# ---------------------------------------------------------------------------
# Dev Agent — LangGraph node
# ---------------------------------------------------------------------------

def dev_agent(state: "AgentState") -> "AgentState":
    """
    LangGraph node — generates Python code from PM spec, writes to sandbox,
    runs pytest, and reports pass/fail to trigger conditional routing.
    """
    node_name = "dev_agent"
    span_id = start_span(state["trace_id"], node_name)

    pm_req = state.get("pm_requirements", {})
    test_scenarios = state.get("test_scenarios", [])
    retry_count = state.get("retry_count", 0)
    prior_code = state.get("generated_code", "")
    prior_test_results = state.get("test_results", "")

    # ── Build prompt ─────────────────────────────────────────────────────────
    spec_text = json.dumps(pm_req, indent=2)
    scenarios_text = "\n".join(f"  - {s}" for s in test_scenarios)

    if retry_count == 0 or not prior_code:
        system_prompt = _DEV_SYSTEM_PROMPT
        user_text = (
            f"Specification:\n{spec_text}\n\n"
            f"Test scenarios to satisfy:\n{scenarios_text}\n\n"
            "Write the complete Python implementation."
        )
    else:
        system_prompt = _DEV_RETRY_SYSTEM_PROMPT
        user_text = (
            f"Specification:\n{spec_text}\n\n"
            f"Test scenarios:\n{scenarios_text}\n\n"
            f"Previous code:\n```python\n{prior_code}\n```\n\n"
            f"Error/failure log from pytest:\n{prior_test_results}\n\n"
            "Fix ALL issues above and return corrected code."
        )

    raw_response = _bedrock_text(system_prompt, user_text)
    generated_code = _extract_code_block(raw_response)

    # ── Write to sandbox ─────────────────────────────────────────────────────
    SANDBOX_DIR.mkdir(parents=True, exist_ok=True)
    code_file = SANDBOX_DIR / "generated_code.py"
    code_file.write_text(generated_code, encoding="utf-8")
    sandbox_path = str(code_file.resolve())

    # Also write a pytest file based on test scenarios
    _write_test_file(pm_req, test_scenarios)

    # ── Run pytest via subprocess ─────────────────────────────────────────────
    # NOTE: Production swaps subprocess with ephemeral Fargate/Docker container.
    test_result = _run_pytest()
    test_passed = test_result["passed"]

    audit = build_audit_entry(
        node=node_name,
        trace_id=state["trace_id"],
        span_id=span_id,
        summary=(
            f"Dev Agent {'succeeded' if test_passed else 'FAILED'} "
            f"(retry {retry_count}, {test_result['total_tests']} tests)"
        ),
        details={
            "retry_count": retry_count,
            "code_lines": len(generated_code.splitlines()),
            "test_passed": test_passed,
            "test_summary": test_result.get("summary", ""),
        },
    )

    end_span(
        span_id,
        node_name=node_name,
        status="ok" if test_passed else "error",
        details={"test_passed": test_passed, "retry": retry_count},
    )

    return {
        **state,
        "generated_code": generated_code,
        "sandbox_path": sandbox_path,
        "test_results": test_result["output"],
        "test_passed": test_passed,
        "retry_count": retry_count + (0 if test_passed else 1),
        "audit_trail": state.get("audit_trail", []) + [audit],
        "span_ids": {**state.get("span_ids", {}), node_name: span_id},
    }


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _build_max_length_valid_email() -> str:
    local = "a" * 64
    domain = f"{'b' * 63}.{'c' * 63}.{'d' * 57}.com"
    email = f"{local}@{domain}"
    if len(email) != 254:
        raise ValueError(f"expected 254-char email, got {len(email)}")
    return email


def _build_too_long_email() -> str:
    return "a" * 255 + "@example.com"


def _infer_target_function(pm_req: dict[str, Any], test_scenarios: list[str]) -> str:
    for scenario in test_scenarios:
        match = _SCENARIO_ASSERT_RE.match(scenario.strip())
        if match:
            return match.group(2)
    title = (pm_req.get("title") or "").lower()
    if "email" in title:
        return "validate_email"
    return "main"


def _scenario_to_test(scenario: str, fn_name: str) -> str | None:
    text = scenario.strip()

    match = _SCENARIO_ASSERT_RE.match(text)
    if match:
        name, fn, arg, expected = match.groups()
        return f"def {name}():\n    assert generated_code.{fn}({arg}) is {expected}"

    match = _SCENARIO_VALIDATES_QUOTED_RE.match(text)
    if match:
        name, arg, expected = match.groups()
        return f"def {name}():\n    assert generated_code.{fn_name}({arg!r}) is {expected}"

    match = _SCENARIO_VALIDATES_EMPTY_RE.match(text)
    if match:
        name, expected = match.groups()
        return f"def {name}():\n    assert generated_code.{fn_name}('') is {expected}"

    match = _SCENARIO_VALIDATES_NONE_RE.match(text)
    if match:
        name, expected = match.groups()
        return f"def {name}():\n    assert generated_code.{fn_name}(None) is {expected}"

    match = _SCENARIO_VALIDATES_TOO_LONG_RE.match(text)
    if match:
        name, expected = match.groups()
        long_email = _build_too_long_email()
        return (
            f"def {name}():\n"
            f"    email = {long_email!r}\n"
            f"    assert len(email) > 254\n"
            f"    assert generated_code.{fn_name}(email) is {expected}"
        )

    match = _SCENARIO_VALIDATES_MAX_LEN_RE.match(text)
    if match:
        name, expected = match.groups()
        max_email = _build_max_length_valid_email()
        return (
            f"def {name}():\n"
            f"    email = {max_email!r}\n"
            f"    assert len(email) == 254\n"
            f"    assert generated_code.{fn_name}(email) is {expected}"
        )

    return None


def _write_test_file(pm_req: dict[str, Any], test_scenarios: list[str]) -> None:
    if PM_SPEC_FILE.exists():
        try:
            saved = json.loads(PM_SPEC_FILE.read_text(encoding="utf-8"))
            test_scenarios = saved.get("test_scenarios") or test_scenarios
        except Exception:
            pass

    fn_name = _infer_target_function(pm_req, test_scenarios)

    test_lines = [
        "\"\"\"Auto-generated test file from PM Agent spec.\"\"\"",
        "import sys",
        "import os",
        "sys.path.insert(0, os.path.dirname(__file__))",
        "import generated_code  # noqa: E402",
        "",
    ]

    parsed_count = 0
    for scenario in test_scenarios:
        body = _scenario_to_test(scenario, fn_name)
        if body:
            test_lines.append(body)
            test_lines.append("")
            parsed_count += 1
        else:
            test_lines.append(f"# UNPARSED: {scenario}")
            test_lines.append("")

    if parsed_count == 0:
        test_lines += [
            "def test_module_importable():",
            "    assert generated_code is not None",
            "",
            "def test_has_callable():",
            "    callables = [v for v in vars(generated_code).values() if callable(v) and not v.__name__.startswith('_')]",
            "    assert len(callables) >= 1",
        ]

    test_file = SANDBOX_DIR / "test_generated_code.py"
    test_file.write_text("\n".join(test_lines), encoding="utf-8")


def _run_pytest() -> dict[str, Any]:
    """
    Run pytest against the sandbox test file.
    Returns a dict with: passed (bool), output (str), summary (str), total_tests (int).

    NOTE: Production swaps this with an ephemeral Docker/AWS Fargate container.
    """
    result = subprocess.run(
        [sys.executable, "-m", "pytest", str(SANDBOX_DIR / "test_generated_code.py"), "-v", "--tb=short"],
        capture_output=True,
        text=True,
        cwd=str(SANDBOX_DIR),
        timeout=120,
    )
    output = result.stdout + result.stderr
    passed = result.returncode == 0

    # Parse summary line (e.g. "2 passed, 1 failed")
    summary = ""
    total = 0
    for line in output.splitlines():
        if "passed" in line or "failed" in line or "error" in line:
            summary = line.strip()
            break
    try:
        import re
        nums = re.findall(r"(\d+)\s+(passed|failed|error)", output)
        total = sum(int(n) for n, _ in nums)
    except Exception:
        pass

    return {"passed": passed, "output": output, "summary": summary, "total_tests": total}
