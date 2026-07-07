"""
agents/qa_agent.py — Quality Assurance Agent node.

Responsibility:
  - Independently validates the generated code against the PM requirements.
  - Runs the full test suite in a subprocess (separate from Dev Agent's run).
  - Grades results against original test scenarios.
  - Sets state['is_approved'] based on outcome.
  - If failed and retries remain, routing logic (in graph.py) sends back to Dev.
  - If passed, routes forward to output_governance_gate.

This is a deliberate second validation pass — the QA agent runs independently
from the Dev agent's self-healing loop to catch any regressions or test scaffold issues.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Any

import boto3

from observability import build_audit_entry, end_span, start_span

if TYPE_CHECKING:
    from state import AgentState

SANDBOX_DIR = Path(__file__).parent.parent / "tmp_sandbox"


def _bedrock_text(system_text: str, user_text: str, max_tokens: int = 1024) -> str:
    region = (os.environ.get("AWS_REGION") or "us-east-1").strip()
    model_id = (os.environ.get("BEDROCK_MODEL_ID") or "anthropic.claude-3-sonnet-20240229-v1:0").strip()
    runtime = boto3.client("bedrock-runtime", region_name=region)
    response = runtime.converse(
        modelId=model_id,
        messages=[{"role": "user", "content": [{"text": user_text}]}],
        system=[{"text": system_text}],
        inferenceConfig={"temperature": 0.0, "maxTokens": max_tokens},
    )
    content = response.get("output", {}).get("message", {}).get("content", [])
    return "".join(part.get("text", "") for part in content if isinstance(part, dict))


def _extract_json(text: str) -> dict[str, Any] | None:
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end <= start:
        return None
    try:
        return json.loads(text[start: end + 1])
    except Exception:
        return None


# ---------------------------------------------------------------------------
# QA Agent — LangGraph node
# ---------------------------------------------------------------------------

_QA_SYSTEM_PROMPT = """\
You are a strict Senior QA Engineer performing code review.
Given:
  - A PM requirements specification (JSON)
  - The generated Python code
  - The pytest output log

Your job is to assess whether the code FULLY satisfies the specification.
Be strict. Respond with JSON only:
{
  "verdict": "PASS" | "FAIL",
  "coverage_score": 0-100,
  "missing_requirements": ["<req 1>", ...],
  "quality_issues": ["<issue 1>", ...],
  "summary": "<1 sentence verdict>"
}
"""


def qa_agent(state: "AgentState") -> "AgentState":
    """
    LangGraph node — independently validates generated code + runs pytest again.
    Sets is_approved based on test results AND LLM-graded spec compliance.
    """
    node_name = "qa_agent"
    span_id = start_span(state["trace_id"], node_name)

    pm_req = state.get("pm_requirements", {})
    generated_code = state.get("generated_code", "")
    retry_count = state.get("retry_count", 0)
    max_retries = state.get("max_retries", 3)

    # ── Step 1: Re-run pytest independently ──────────────────────────────────
    pytest_result = _run_pytest_independently()
    tests_green = pytest_result["passed"]

    # ── Step 2: LLM-graded spec compliance review ────────────────────────────
    user_text = (
        f"PM Specification:\n{json.dumps(pm_req, indent=2)}\n\n"
        f"Generated Code:\n```python\n{generated_code[:6000]}\n```\n\n"
        f"Pytest Output:\n{pytest_result['output'][:2000]}\n\n"
        "Grade this code against the specification."
    )
    raw = _bedrock_text(_QA_SYSTEM_PROMPT, user_text)
    review = _extract_json(raw) or {
        "verdict": "FAIL" if not tests_green else "PASS",
        "coverage_score": 100 if tests_green else 0,
        "missing_requirements": [],
        "quality_issues": [],
        "summary": "Auto-graded from pytest result only (LLM parse failed)",
    }

    # ── Step 3: Determine approval ───────────────────────────────────────────
    llm_pass = review.get("verdict", "FAIL").upper() == "PASS"
    is_approved = tests_green and llm_pass

    # If retries exhausted, approve whatever we have to avoid infinite loops
    if not is_approved and retry_count >= max_retries:
        is_approved = False  # explicit rejection after exhausting retries

    audit = build_audit_entry(
        node=node_name,
        trace_id=state["trace_id"],
        span_id=span_id,
        summary=(
            f"QA {'APPROVED' if is_approved else 'REJECTED'}: "
            f"pytest={'PASS' if tests_green else 'FAIL'}, "
            f"llm_verdict={review.get('verdict')}, "
            f"coverage={review.get('coverage_score')}%"
        ),
        details={
            "tests_green": tests_green,
            "llm_verdict": review.get("verdict"),
            "coverage_score": review.get("coverage_score"),
            "missing_requirements": review.get("missing_requirements", []),
            "quality_issues": review.get("quality_issues", []),
            "retry_count": retry_count,
            "max_retries": max_retries,
        },
    )

    end_span(
        span_id,
        node_name=node_name,
        status="ok" if is_approved else "error",
        details={"is_approved": is_approved},
    )

    # Append QA test results to state so Dev can see them on retry
    full_results = (
        f"=== QA Agent Review ===\n"
        f"Pytest: {'PASS' if tests_green else 'FAIL'}\n"
        f"LLM Verdict: {review.get('verdict')}\n"
        f"Coverage Score: {review.get('coverage_score')}%\n"
        f"Missing: {review.get('missing_requirements', [])}\n"
        f"Quality Issues: {review.get('quality_issues', [])}\n"
        f"Summary: {review.get('summary')}\n\n"
        f"=== Pytest Output ===\n{pytest_result['output']}"
    )

    return {
        **state,
        "test_results": full_results,
        "test_passed": tests_green,
        "is_approved": is_approved,
        "retry_count": retry_count if is_approved else retry_count + 1,
        "audit_trail": state.get("audit_trail", []) + [audit],
        "span_ids": {**state.get("span_ids", {}), node_name: span_id},
    }


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _run_pytest_independently() -> dict[str, Any]:
    """
    Run pytest in an independent subprocess.
    NOTE: In production, this runs inside a separate test container (Fargate/Docker).
    """
    if not (SANDBOX_DIR / "test_generated_code.py").exists():
        return {"passed": False, "output": "No test file found in sandbox.", "summary": "no tests"}

    result = subprocess.run(
        [sys.executable, "-m", "pytest", str(SANDBOX_DIR / "test_generated_code.py"), "-v", "--tb=long"],
        capture_output=True,
        text=True,
        cwd=str(SANDBOX_DIR),
        timeout=120,
    )
    output = result.stdout + result.stderr
    passed = result.returncode == 0
    summary = next(
        (line.strip() for line in output.splitlines() if "passed" in line or "failed" in line),
        "no summary"
    )
    return {"passed": passed, "output": output, "summary": summary}
