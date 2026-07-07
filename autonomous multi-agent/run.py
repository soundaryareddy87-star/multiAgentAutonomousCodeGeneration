"""
run.py — CLI entrypoint for the OneTrust Agentic Engineering Factory PoC.

Usage:
    python run.py --requirement "Write a Python function that validates email addresses"
    python run.py --requirement "Build a rate limiter class" --max-retries 2

Environment variables required:
    AWS_REGION          (default: us-east-1)
    AWS_ACCESS_KEY_ID   or AWS profile with Bedrock access
    AWS_SECRET_ACCESS_KEY
    BEDROCK_MODEL_ID    (default: anthropic.claude-3-sonnet-20240229-v1:0)
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()  # Load .env if present

from graph import build_graph
from observability import init_trace
from state import AgentState

# ---------------------------------------------------------------------------
# Output helpers
# ---------------------------------------------------------------------------

_DIVIDER = "=" * 70


def _print_header(title: str) -> None:
    print(f"\n{_DIVIDER}")
    print(f"  {title}")
    print(_DIVIDER)


def _print_audit_trail(audit_trail: list[dict]) -> None:
    _print_header("AUDIT TRAIL")
    for i, entry in enumerate(audit_trail, 1):
        print(f"\n[{i}] Node: {entry.get('node', 'unknown')}")
        print(f"    Span : {entry.get('span_id', '')[:16]}...")
        print(f"    Time : {entry.get('timestamp', '')}")
        print(f"    ▶    : {entry.get('summary', '')}")
        details = entry.get("details", {})
        if details:
            for k, v in details.items():
                print(f"         {k}: {v}")


def _print_governance_report(report: dict) -> None:
    _print_header("OUTPUT GOVERNANCE REPORT")
    print(f"  PII / Secret scan : {'✅ PASS' if report.get('pii_passed') else '❌ FAIL — findings below'}")
    print(f"  SAST scan         : {'✅ PASS' if report.get('sast_passed') else '❌ FAIL — findings below'}")

    pii = report.get("pii_findings", [])
    if pii:
        print(f"\n  PII Findings ({len(pii)}):")
        for f in pii:
            print(f"    [{f.get('entity_type')}] score={f.get('score', '?')} → '{f.get('text_excerpt', '')[:40]}'")

    sast = report.get("sast_findings", [])
    if sast:
        print(f"\n  SAST Findings ({len(sast)}):")
        for f in sast:
            print(f"    [{f.get('severity')}] {f.get('rule_id')} line={f.get('line')} — {f.get('message', '')[:80]}")


def _print_final_code(final_code: str, sandbox_path: str) -> None:
    _print_header("FINAL CLEAN CODE")
    if final_code:
        print(f"  Sandbox path: {sandbox_path}")
        print(f"  Code length : {len(final_code)} chars / {len(final_code.splitlines())} lines")
        print(f"\n--- code preview (first 40 lines) ---")
        for line in final_code.splitlines()[:40]:
            print(f"  {line}")
        if len(final_code.splitlines()) > 40:
            print(f"  ... ({len(final_code.splitlines()) - 40} more lines)")
    else:
        print("  ❌ No clean code produced — governance gate blocked egress.")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="OneTrust Agentic Engineering Factory — PoC CLI"
    )
    parser.add_argument(
        "--requirement",
        required=True,
        help="The engineering requirement to process (natural language string)",
    )
    parser.add_argument(
        "--max-retries",
        type=int,
        default=1,
        help="Maximum Dev→QA retry cycles (default: 1)",
    )
    args = parser.parse_args()

    trace_id = init_trace()
    thread_id = f"run-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S')}"

    print(f"\n🚀 Agentic Engineering Factory — OneTrust PoC")
    print(f"   Trace ID  : {trace_id}")
    print(f"   Thread ID : {thread_id}")
    print(f"   Requirement: {args.requirement[:120]}")

    # ── Build initial state ──────────────────────────────────────────────────
    initial_state: AgentState = {
        "trace_id": trace_id,
        "span_ids": {},
        "raw_requirement": args.requirement,
        "retry_count": 0,
        "max_retries": args.max_retries,
        "audit_trail": [],
    }

    # ── Compile and run graph ────────────────────────────────────────────────
    graph = build_graph()
    config = {"configurable": {"thread_id": thread_id}}

    try:
        final_state: AgentState = graph.invoke(initial_state, config=config)
    except Exception as exc:
        print(f"\n❌ Graph execution failed: {exc}", file=sys.stderr)
        raise

    # ── Print results ────────────────────────────────────────────────────────

    # Injection blocked?
    if final_state.get("injection_detected"):
        _print_header("⛔ BLOCKED — PROMPT INJECTION DETECTED")
        print("  The input requirement was blocked by the Input Guardrail.")
        print("  No LLM call was made. No code was generated.")
        _print_audit_trail(final_state.get("audit_trail", []))
        sys.exit(1)

    # Governance report
    gov_report = final_state.get("governance_report")
    if gov_report:
        _print_governance_report(gov_report)

    # Final code
    final_code = final_state.get("final_code", "")
    sandbox_path = final_state.get("sandbox_path", "tmp_sandbox/generated_code.py")
    _print_final_code(final_code, sandbox_path)

    # Audit trail
    _print_audit_trail(final_state.get("audit_trail", []))

    # ── Summary ──────────────────────────────────────────────────────────────
    _print_header("RUN SUMMARY")
    is_approved = final_state.get("is_approved", False)
    retries = final_state.get("retry_count", 0)
    pii_ok = gov_report.get("pii_passed", False) if gov_report else False
    sast_ok = gov_report.get("sast_passed", False) if gov_report else False

    print(f"  QA Approved   : {'✅' if is_approved else '❌'}")
    print(f"  PII Scan      : {'✅' if pii_ok else '❌'}")
    print(f"  SAST Scan     : {'✅' if sast_ok else '❌'}")
    print(f"  Retry Cycles  : {retries}")
    print(f"  Code Ready    : {'✅ Yes — ready for Git egress' if final_code else '❌ No — blocked by governance'}")
    print(f"\n  Trace ID : {trace_id}")
    print(f"  Thread   : {thread_id}")
    print(_DIVIDER)

    if not (is_approved and pii_ok and sast_ok):
        sys.exit(1)


if __name__ == "__main__":
    main()
