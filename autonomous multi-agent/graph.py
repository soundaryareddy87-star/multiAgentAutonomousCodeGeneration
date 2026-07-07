"""
graph.py — LangGraph StateGraph assembly for the Agentic Engineering Factory.

Topology:
  input_guardrail → pm_agent → dev_agent → qa_agent → output_governance → END
                                    ↑           │
                                    └─(retry)───┘  (conditional edge)

Key design decisions:
  - MemorySaver checkpointer: state is durable across restarts.
  - Conditional edge from qa_agent: retry Dev up to max_retries times on failure.
  - injection_detected guard: graph terminates early if guardrail fires.
  - All nodes are pure functions returning partial state updates.
"""

from __future__ import annotations

from typing import Any

from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import END, StateGraph

from agents.dev_agent import dev_agent
from agents.pm_agent import pm_agent
from agents.qa_agent import qa_agent
from guardrails import input_guardrail, output_governance_gate
from state import AgentState

# ---------------------------------------------------------------------------
# Conditional routing functions
# ---------------------------------------------------------------------------

def _route_after_guardrail(state: AgentState) -> str:
    """
    If the input guardrail detected injection → terminate immediately.
    Otherwise → proceed to PM Agent.
    """
    if state.get("injection_detected", False):
        return END  # type: ignore[return-value]
    return "pm_agent"


def _route_after_qa(state: AgentState) -> str:
    """
    After QA review:
      - PASS → output governance gate
      - FAIL + retries remaining → back to Dev Agent
      - FAIL + retries exhausted → output governance (will flag in report)
    """
    if state.get("is_approved", False):
        return "output_governance"

    retry_count = state.get("retry_count", 0)
    max_retries = state.get("max_retries", 3)

    if retry_count < max_retries:
        return "dev_agent"

    # Retries exhausted — push to governance gate so a partial report is produced
    return "output_governance"


# ---------------------------------------------------------------------------
# Graph builder
# ---------------------------------------------------------------------------

def build_graph() -> Any:
    """
    Compile and return the LangGraph CompiledGraph with MemorySaver checkpointing.

    Returns:
        compiled_graph — call with .invoke(state, config={"configurable": {"thread_id": "..."}})
    """
    builder = StateGraph(AgentState)

    # ── Register nodes ────────────────────────────────────────────────────────
    builder.add_node("input_guardrail", input_guardrail)
    builder.add_node("pm_agent", pm_agent)
    builder.add_node("dev_agent", dev_agent)
    builder.add_node("qa_agent", qa_agent)
    builder.add_node("output_governance", output_governance_gate)

    # ── Entry point ───────────────────────────────────────────────────────────
    builder.set_entry_point("input_guardrail")

    # ── Edges ─────────────────────────────────────────────────────────────────
    # Guardrail → conditional (block or proceed)
    builder.add_conditional_edges(
        "input_guardrail",
        _route_after_guardrail,
        {
            "pm_agent": "pm_agent",
            END: END,
        },
    )

    # PM → Dev (always)
    builder.add_edge("pm_agent", "dev_agent")

    # Dev → QA (always — QA is the independent judge)
    builder.add_edge("dev_agent", "qa_agent")

    # QA → conditional (retry Dev or proceed to governance)
    builder.add_conditional_edges(
        "qa_agent",
        _route_after_qa,
        {
            "dev_agent": "dev_agent",
            "output_governance": "output_governance",
        },
    )

    # Governance → END
    builder.add_edge("output_governance", END)

    # ── Compile with durable checkpointing ───────────────────────────────────
    checkpointer = MemorySaver()
    return builder.compile(checkpointer=checkpointer)
