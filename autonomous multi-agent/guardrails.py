"""
guardrails.py — Enterprise governance layer for the Agentic Engineering Factory.

Two independent guardrail nodes:

1. input_guardrail(state)  — detects prompt injection in the raw requirement
                             BEFORE any LLM call is made.

2. output_governance_gate(state) — runs on approved Dev output BEFORE Git egress:
   a) Microsoft Presidio:  PII + secret entity detection & redaction
   b) Semgrep (subprocess): SAST scan for OWASP Top 10 patterns

Design principle: governance is a fully decoupled software layer.
It never calls an LLM and never trusts model output.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile
from typing import TYPE_CHECKING, Any

# Presidio — installed via requirements.txt
try:
    from presidio_analyzer import AnalyzerEngine
    from presidio_analyzer.nlp_engine import NlpEngineProvider
    from presidio_anonymizer import AnonymizerEngine

    # Explicitly configure the spaCy model so Presidio uses en_core_web_md
    # (avoids the default en_core_web_lg download on every cold start).
    # Switch to en_core_web_lg in production for higher PII recall.
    _nlp_config = {
        "nlp_engine_name": "spacy",
        "models": [{"lang_code": "en", "model_name": "en_core_web_md"}],
    }
    _nlp_provider = NlpEngineProvider(nlp_configuration=_nlp_config)
    _nlp_engine = _nlp_provider.create_engine()
    _analyzer = AnalyzerEngine(nlp_engine=_nlp_engine, supported_languages=["en"])
    _anonymizer = AnonymizerEngine()
    _PRESIDIO_AVAILABLE = True
except ImportError:  # pragma: no cover
    _PRESIDIO_AVAILABLE = False
    _analyzer = None  # type: ignore[assignment]
    _anonymizer = None  # type: ignore[assignment]

from observability import build_audit_entry, end_span, start_span

if TYPE_CHECKING:
    from state import AgentState

# ---------------------------------------------------------------------------
# Known prompt-injection patterns (extend as needed)
# ---------------------------------------------------------------------------
_INJECTION_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r"ignore\s+(all\s+)?previous\s+instructions?", re.IGNORECASE),
    re.compile(r"you\s+are\s+now\s+(a\s+)?DAN", re.IGNORECASE),
    re.compile(r"disregard\s+(your\s+)?(system\s+)?prompt", re.IGNORECASE),
    re.compile(r"act\s+as\s+(if\s+you\s+are\s+)?a\s+(new|different|evil)", re.IGNORECASE),
    re.compile(r"jailbreak", re.IGNORECASE),
    re.compile(r"<\s*script", re.IGNORECASE),              # HTML/JS injection
    re.compile(r"UNION\s+SELECT", re.IGNORECASE),          # SQL injection
    re.compile(r"eval\s*\(\s*compile", re.IGNORECASE),     # code injection
]

# Hardcoded secret patterns (supplement Presidio)
_PII_SCORE_THRESHOLD = float(os.environ.get("PII_SCORE_THRESHOLD", "0.85"))
_BLOCKING_PII_ENTITIES = [
    "CREDIT_CARD",
    "CRYPTO",
    "IBAN_CODE",
    "IP_ADDRESS",
    "MEDICAL_LICENSE",
    "PHONE_NUMBER",
    "US_BANK_NUMBER",
    "US_DRIVER_LICENSE",
    "US_ITIN",
    "US_PASSPORT",
    "US_SSN",
]
_EXAMPLE_EMAIL_RE = re.compile(
    r"(?i)(example\.(com|org|net)|test@|user@|@example|@test|@domain|@mail\.)"
)
_CODE_ARTIFACT_RE = re.compile(
    r"(?i)(r['\"]\^|re\.(match|compile|search)|\[a-zA-Z|\\\\|\.startswith|\.endswith|def |import )"
)
_SECRET_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("AWS_KEY",      re.compile(r"AKIA[0-9A-Z]{16}")),
    ("GENERIC_PASS", re.compile(r'(?i)(password|passwd|pwd)\s*[=:]\s*["\']?[^\s"\']{6,}')),
    ("PRIVATE_KEY",  re.compile(r"-----BEGIN (RSA |EC )?PRIVATE KEY-----")),
    ("DB_CONN_STR",  re.compile(r"(?i)(postgres|mysql|mongodb)://[^\s]+")),
    ("BEARER_TOKEN", re.compile(r"(?i)bearer\s+[a-z0-9\-_\.]{20,}")),
    ("GITHUB_PAT",   re.compile(r"ghp_[a-zA-Z0-9]{36}")),
]


# ===========================================================================
# NODE 1 — Input Guardrail
# ===========================================================================

def input_guardrail(state: "AgentState") -> "AgentState":
    """
    LangGraph node — scans raw_requirement for prompt injection patterns.
    Sets state['injection_detected'] = True and blocks forward execution
    if any pattern fires. Safe requirements pass through unchanged.
    """
    node_name = "input_guardrail"
    span_id = start_span(state["trace_id"], node_name)

    raw = state.get("raw_requirement", "")
    findings: list[str] = []

    for pattern in _INJECTION_PATTERNS:
        if pattern.search(raw):
            findings.append(pattern.pattern)

    audit = build_audit_entry(
        node=node_name,
        trace_id=state["trace_id"],
        span_id=span_id,
        summary="BLOCKED — injection detected" if findings else "PASS — no injection patterns found",
        details={"injection_patterns_hit": findings, "raw_length": len(raw)},
    )

    end_span(
        span_id,
        node_name=node_name,
        status="blocked" if findings else "ok",
        details={"findings": findings},
    )

    updates: dict[str, Any] = {
        "audit_trail": state.get("audit_trail", []) + [audit],
        "span_ids": {**state.get("span_ids", {}), node_name: span_id},
    }

    if findings:
        updates["injection_detected"] = True
        # Leave sanitized_requirement unset — downstream nodes will check this flag
    else:
        updates["injection_detected"] = False
        updates["sanitized_requirement"] = raw

    return {**state, **updates}


# ===========================================================================
# NODE 2 — Output Governance Gate
# ===========================================================================

def output_governance_gate(state: "AgentState") -> "AgentState":
    """
    LangGraph node — runs after QA approval, before Git egress.
    Applies:
      • Presidio PII / secret entity detection + redaction
      • Semgrep SAST scan (subprocess)
    Writes governance_report and final_code to state.
    """
    node_name = "output_governance"
    span_id = start_span(state["trace_id"], node_name)

    code = state.get("generated_code", "")

    # ── Step 1: PII + Secret Redaction ──────────────────────────────────────
    pii_findings, redacted_code = _run_presidio(code)
    regex_findings = _run_secret_regex(redacted_code)
    pii_all = pii_findings + regex_findings
    pii_passed = len(pii_all) == 0

    # ── Step 2: SAST via Semgrep ─────────────────────────────────────────────
    sast_findings, sast_passed = _run_semgrep(redacted_code)

    governance_report: dict[str, Any] = {
        "pii_findings": pii_all,
        "sast_findings": sast_findings,
        "redacted_code": redacted_code,
        "sast_passed": sast_passed,
        "pii_passed": pii_passed,
    }

    overall_status = "ok" if (pii_passed and sast_passed) else "blocked"

    audit = build_audit_entry(
        node=node_name,
        trace_id=state["trace_id"],
        span_id=span_id,
        summary=(
            f"Governance: pii={'PASS' if pii_passed else 'FAIL'}, "
            f"sast={'PASS' if sast_passed else 'FAIL'}"
        ),
        details={
            "pii_finding_count": len(pii_all),
            "sast_finding_count": len(sast_findings),
            "sast_passed": sast_passed,
            "pii_passed": pii_passed,
        },
    )

    end_span(span_id, node_name=node_name, status=overall_status)

    return {
        **state,
        "governance_report": governance_report,
        "final_code": redacted_code if (pii_passed and sast_passed) else "",
        "audit_trail": state.get("audit_trail", []) + [audit],
        "span_ids": {**state.get("span_ids", {}), node_name: span_id},
    }


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _is_actionable_pii_finding(finding: dict[str, Any]) -> bool:
    if finding.get("score", 0) < _PII_SCORE_THRESHOLD:
        return False
    entity_type = finding.get("entity_type", "")
    if entity_type in _BLOCKING_PII_ENTITIES:
        return True
    if entity_type == "EMAIL_ADDRESS":
        excerpt = finding.get("text_excerpt", "")
        return not _EXAMPLE_EMAIL_RE.search(excerpt)
    if entity_type == "PERSON":
        excerpt = finding.get("text_excerpt", "")
        if _CODE_ARTIFACT_RE.search(excerpt):
            return False
        return False
    return False


def _run_presidio(code: str) -> tuple[list[dict[str, Any]], str]:
    """Run Presidio analyzer + anonymizer. Returns (actionable findings, redacted_code)."""
    if not _PRESIDIO_AVAILABLE:
        return [], code

    results = _analyzer.analyze(
        text=code,
        language="en",
        entities=_BLOCKING_PII_ENTITIES + ["EMAIL_ADDRESS"],
        score_threshold=_PII_SCORE_THRESHOLD,
    )
    all_findings = [
        {
            "entity_type": r.entity_type,
            "start": r.start,
            "end": r.end,
            "score": round(r.score, 3),
            "text_excerpt": code[r.start : r.end],
        }
        for r in results
    ]
    findings = [f for f in all_findings if _is_actionable_pii_finding(f)]

    if all_findings:
        anonymized = _anonymizer.anonymize(text=code, analyzer_results=results)
        redacted = anonymized.text
    else:
        redacted = code

    return findings, redacted


def _run_secret_regex(code: str) -> list[dict[str, Any]]:
    """Run hardcoded secret regex patterns as a supplement to Presidio."""
    findings: list[dict[str, Any]] = []
    for label, pattern in _SECRET_PATTERNS:
        for match in pattern.finditer(code):
            findings.append({
                "entity_type": label,
                "start": match.start(),
                "end": match.end(),
                "text_excerpt": match.group()[:40] + ("..." if len(match.group()) > 40 else ""),
            })
    return findings


def _run_semgrep(code: str) -> tuple[list[dict[str, Any]], bool]:
    """
    Write code to a temp file and run Semgrep with the python security ruleset.
    Returns (findings, passed).
    Falls back gracefully if semgrep is not installed.

    Production note: swap this with a dedicated Semgrep/SonarQube task pod.
    """
    try:
        version_check = subprocess.run(["semgrep", "--version"], capture_output=True)
    except FileNotFoundError:
        print("[GOVERNANCE] WARNING: semgrep not found — skipping SAST scan.")
        return [], True
    if version_check.returncode != 0:
        print("[GOVERNANCE] WARNING: semgrep not found — skipping SAST scan.")
        return [], True

    with tempfile.NamedTemporaryFile(suffix=".py", mode="w", encoding="utf-8", delete=False) as f:
        f.write(code)
        tmp_path = f.name

    try:
        result = subprocess.run(
            [
                "semgrep",
                "--config", "p/python",   # OWASP / Python security ruleset
                "--json",
                tmp_path,
            ],
            capture_output=True,
            text=True,
            timeout=60,
        )
        try:
            data = json.loads(result.stdout)
            raw_findings = data.get("results", [])
        except json.JSONDecodeError:
            raw_findings = []

        findings = [
            {
                "rule_id": f.get("check_id", "unknown"),
                "severity": f.get("extra", {}).get("severity", "unknown"),
                "message": f.get("extra", {}).get("message", ""),
                "line": f.get("start", {}).get("line"),
            }
            for f in raw_findings
        ]
        # Block on ERROR or WARNING severity findings
        blocking = [f for f in findings if f["severity"].upper() in {"ERROR", "WARNING"}]
        return findings, len(blocking) == 0

    except subprocess.TimeoutExpired:
        return [{"rule_id": "TIMEOUT", "severity": "ERROR", "message": "Semgrep timed out", "line": None}], False
    finally:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
