"""
state.py — Shared AgentState TypedDict for the OneTrust Agentic Engineering Factory.

Every LangGraph node receives and returns a mutation of this state object.
The full object is persisted to the MemorySaver checkpointer after every node,
enabling durable pause/resume (e.g., for HITL approval or crash recovery).
"""

from __future__ import annotations

from typing import Any, NotRequired, TypedDict


# ---------------------------------------------------------------------------
# Audit entry — one structured record per node execution
# ---------------------------------------------------------------------------

class AuditEntry(TypedDict):
    node: str          # node name that produced this entry
    trace_id: str      # top-level trace ID (from run.py)
    span_id: str       # child span ID for this node
    timestamp: str     # ISO-8601 UTC
    summary: str       # human-readable summary of what happened
    details: dict[str, Any]   # arbitrary node-specific payload


# ---------------------------------------------------------------------------
# Governance report — output of the Output Governance Gate
# ---------------------------------------------------------------------------

class GovernanceReport(TypedDict):
    pii_findings: list[dict[str, Any]]   # Presidio entity hits
    sast_findings: list[dict[str, Any]]  # Semgrep rule hits
    redacted_code: str                   # code after PII scrubbing
    sast_passed: bool                    # True = no blocking SAST findings
    pii_passed: bool                     # True = no PII leakage found


# ---------------------------------------------------------------------------
# Primary state object
# ---------------------------------------------------------------------------

class AgentState(TypedDict):
    # ── Provenance ──────────────────────────────────────────────────────────
    trace_id: str                          # unique per invocation (UUID4)
    span_ids: dict[str, str]               # node_name → span_id

    # ── Input ───────────────────────────────────────────────────────────────
    raw_requirement: str                   # original user/webhook payload
    sanitized_requirement: NotRequired[str]  # after input guardrail

    # ── PM Agent outputs ────────────────────────────────────────────────────
    pm_requirements: NotRequired[dict[str, Any]]   # structured requirement doc
    test_scenarios: NotRequired[list[str]]          # expected test cases

    # ── Dev Agent outputs ───────────────────────────────────────────────────
    generated_code: NotRequired[str]       # latest code produced by Dev node
    sandbox_path: NotRequired[str]         # absolute path to tmp_sandbox file

    # ── QA Agent outputs ────────────────────────────────────────────────────
    test_results: NotRequired[str]         # pytest stdout/stderr
    test_passed: NotRequired[bool]         # True = all tests green

    # ── Control flow ────────────────────────────────────────────────────────
    retry_count: int                       # number of Dev→QA retry cycles
    max_retries: int                       # hard ceiling (default 3)
    is_approved: NotRequired[bool]         # set True when QA passes

    # ── Governance ──────────────────────────────────────────────────────────
    governance_report: NotRequired[GovernanceReport]
    final_code: NotRequired[str]           # clean code ready for Git egress

    # ── Observability ───────────────────────────────────────────────────────
    audit_trail: list[AuditEntry]          # append-only log across all nodes

    # ── Input guardrail flag ────────────────────────────────────────────────
    injection_detected: NotRequired[bool]  # True = input blocked
