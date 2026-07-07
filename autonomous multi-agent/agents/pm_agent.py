"""
agents/pm_agent.py — Product Manager Agent node.

Responsibility:
  - Receives the sanitized raw requirement from state.
  - Calls AWS Bedrock (Claude 3 Sonnet) to produce:
      • A structured requirements document (JSON)
      • A list of concrete test scenarios the Dev agent must satisfy
  - Updates state with pm_requirements + test_scenarios.

Uses the same _bedrock_text() helper pattern as the WES llm.py,
keeping Bedrock calls consistent across both PoCs.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import TYPE_CHECKING, Any

import boto3

from observability import build_audit_entry, end_span, start_span

if TYPE_CHECKING:
    from state import AgentState

SANDBOX_DIR = Path(__file__).parent.parent / "tmp_sandbox"
PM_SPEC_FILE = SANDBOX_DIR / "pm_spec.json"

# ---------------------------------------------------------------------------
# Bedrock client helpers (consistent with WES llm.py pattern)
# ---------------------------------------------------------------------------

def _bedrock_text(system_text: str, user_text: str, max_tokens: int = 2048) -> str:
    region = (os.environ.get("AWS_REGION") or "us-east-1").strip()
    # Default: Claude 3 Haiku (active on Bedrock as of 2026).
    # Override via BEDROCK_MODEL_ID env var.
    model_id = (
        os.environ.get("BEDROCK_MODEL_ID")
        or "anthropic.claude-3-haiku-20240307-v1:0"
    ).strip()
    runtime = boto3.client("bedrock-runtime", region_name=region)
    response = runtime.converse(
        modelId=model_id,
        messages=[{"role": "user", "content": [{"text": user_text}]}],
        system=[{"text": system_text}],
        inferenceConfig={"temperature": 0.2, "maxTokens": max_tokens},
    )
    content = response.get("output", {}).get("message", {}).get("content", [])
    return "".join(part.get("text", "") for part in content if isinstance(part, dict))


def _extract_json(text: str) -> dict[str, Any] | None:
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end <= start:
        return None
    try:
        return json.loads(text[start : end + 1])
    except Exception:
        return None


# ---------------------------------------------------------------------------
# PM Agent — LangGraph node
# ---------------------------------------------------------------------------

_PM_SYSTEM_PROMPT = """\
You are an expert Senior Product Manager and Software Architect.
Your role is to receive a software engineering requirement and convert it into 
a precise, implementation-ready specification document.

You MUST respond with valid JSON only — no prose, no markdown fences.
JSON schema:
{
  "title": "<concise title>",
  "description": "<1-2 sentence description>",
  "functional_requirements": ["<requirement 1>", ...],
  "non_functional_requirements": ["<requirement 1>", ...],
  "input_spec": {"<param_name>": "<type + description>"},
  "output_spec": {"<field_name>": "<type + description>"},
  "edge_cases": ["<edge case 1>", ...],
  "test_scenarios": [
    "test_<name>: Assert that <function_name>(<python_arg_literal>) returns True",
    "test_<name>: Assert that <function_name>(<python_arg_literal>) returns False"
  ]
}

Each test_scenarios entry MUST use this exact pattern:
  test_<snake_case_name>: Assert that <function_name>(<python_arg>) returns True
  test_<snake_case_name>: Assert that <function_name>(<python_arg>) returns False
Use valid Python literals for args (e.g. 'user@example.com', '', None).
Do NOT use alternate phrasing like "Validates that ...".
"""


def pm_agent(state: "AgentState") -> "AgentState":
    """
    LangGraph node — structures the sanitized requirement into a formal spec.
    """
    node_name = "pm_agent"
    span_id = start_span(state["trace_id"], node_name)

    requirement = state.get("sanitized_requirement") or state.get("raw_requirement", "")

    user_text = (
        f"Engineering Requirement:\n\n{requirement}\n\n"
        "Convert this into the structured JSON specification. "
        "Include at least 3 test_scenarios as concrete pytest-style descriptions."
    )

    raw_response = _bedrock_text(_PM_SYSTEM_PROMPT, user_text)
    parsed = _extract_json(raw_response)

    if parsed is None:
        # Fallback: minimal structure so pipeline can continue
        parsed = {
            "title": "Unstructured Requirement",
            "description": requirement,
            "functional_requirements": [requirement],
            "non_functional_requirements": [],
            "input_spec": {},
            "output_spec": {},
            "edge_cases": [],
            "test_scenarios": [
                "test_happy_path: basic input produces expected output",
                "test_edge_case: empty or null input handled gracefully",
                "test_type_error: wrong type raises TypeError",
            ],
        }

    test_scenarios: list[str] = parsed.get("test_scenarios", [])

    SANDBOX_DIR.mkdir(parents=True, exist_ok=True)
    PM_SPEC_FILE.write_text(json.dumps(parsed, indent=2), encoding="utf-8")

    audit = build_audit_entry(
        node=node_name,
        trace_id=state["trace_id"],
        span_id=span_id,
        summary=f"PM Agent produced spec: '{parsed.get('title', 'unknown')}' with {len(test_scenarios)} test scenarios",
        details={
            "title": parsed.get("title"),
            "functional_req_count": len(parsed.get("functional_requirements", [])),
            "test_scenario_count": len(test_scenarios),
            "llm_raw_response_len": len(raw_response),
        },
    )

    end_span(span_id, node_name=node_name, status="ok", details={"title": parsed.get("title")})

    return {
        **state,
        "pm_requirements": parsed,
        "test_scenarios": test_scenarios,
        "audit_trail": state.get("audit_trail", []) + [audit],
        "span_ids": {**state.get("span_ids", {}), node_name: span_id},
    }
