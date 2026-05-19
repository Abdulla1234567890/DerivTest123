"""
Validation script — checks that all pipeline artifacts are present,
structurally correct, and internally consistent.

Usage:
    python validate.py          # print report, exit 1 if any check fails
"""

import json
import sys
from pathlib import Path

BASE_DIR = Path(__file__).parent

REQUIRED_FILES = [
    "tickets.json",
    "policy_kb.json",
    "triage.json",
    "retrieval_results.json",
    "draft_responses.json",
    "review_results.json",
    "response_checks.json",
    "final_responses.json",
    "llm_calls.jsonl",
]


def run_validation() -> dict:
    checks = []

    def ok(name: str, passed: bool, detail: str = ""):
        checks.append({"name": name, "passed": bool(passed), "detail": detail})
        return passed

    # ── Load inputs ───────────────────────────────────────────────────────────
    ticket_ids: list = []
    try:
        with open(BASE_DIR / "tickets.json") as f:
            tickets = json.load(f)
        ticket_ids = [t["ticket_id"] for t in tickets]
        ok("tickets.json is valid JSON", True, f"{len(tickets)} tickets found")
    except Exception as exc:
        ok("tickets.json is valid JSON", False, str(exc))

    try:
        with open(BASE_DIR / "policy_kb.json") as f:
            policy_kb = json.load(f)
        policy_ids = {p["policy_id"] for p in policy_kb}
        ok("policy_kb.json is valid JSON", True, f"{len(policy_kb)} policies found")
    except Exception as exc:
        ok("policy_kb.json is valid JSON", False, str(exc))
        policy_ids = set()

    # ── File existence ────────────────────────────────────────────────────────
    for fname in REQUIRED_FILES:
        path = BASE_DIR / fname
        ok(f"{fname} exists", path.exists())

    # ── JSON validity for each artifact ───────────────────────────────────────
    artifacts: dict = {}
    for fname in REQUIRED_FILES:
        path = BASE_DIR / fname
        if not path.exists():
            continue
        try:
            with open(path) as f:
                if fname.endswith(".jsonl"):
                    artifacts[fname] = [json.loads(line) for line in f if line.strip()]
                else:
                    artifacts[fname] = json.load(f)
            ok(f"{fname} parses as valid JSON", True)
        except Exception as exc:
            ok(f"{fname} parses as valid JSON", False, str(exc))

    triage       = artifacts.get("triage.json", [])
    retrieval    = artifacts.get("retrieval_results.json", [])
    drafts       = artifacts.get("draft_responses.json", [])
    reviews      = artifacts.get("review_results.json", [])
    checks_data  = artifacts.get("response_checks.json", [])
    final        = artifacts.get("final_responses.json", [])
    llm_calls    = artifacts.get("llm_calls.jsonl", [])

    # ── Per-ticket coverage ───────────────────────────────────────────────────
    triage_ids   = {t["ticket_id"] for t in triage}
    draft_ids    = {d["ticket_id"] for d in drafts}
    review_ids   = {r["ticket_id"] for r in reviews}
    check_ids    = {c["ticket_id"] for c in checks_data}
    final_ids    = {f["ticket_id"] for f in final}
    ticket_set   = set(ticket_ids)

    ok("All tickets have triage records",
       ticket_set == triage_ids,
       f"Missing: {ticket_set - triage_ids}" if ticket_set != triage_ids else "")

    ok("All tickets have draft responses",
       ticket_set == draft_ids,
       f"Missing: {ticket_set - draft_ids}" if ticket_set != draft_ids else "")

    ok("All tickets have review results",
       ticket_set == review_ids,
       f"Missing: {ticket_set - review_ids}" if ticket_set != review_ids else "")

    ok("All tickets have deterministic check records",
       ticket_set == check_ids,
       f"Missing: {ticket_set - check_ids}" if ticket_set != check_ids else "")

    ok("All tickets have final responses",
       ticket_set == final_ids,
       f"Missing: {ticket_set - final_ids}" if ticket_set != final_ids else "")

    # ── Separate LLM calls per ticket ─────────────────────────────────────────
    triage_calls  = [c for c in llm_calls if c.get("stage") == "triage"]
    draft_calls   = [c for c in llm_calls if c.get("stage") == "drafting"]
    review_calls  = [c for c in llm_calls if c.get("stage") == "review"]

    ok("One triage LLM call per ticket",
       len(triage_calls) == len(ticket_ids),
       f"Expected {len(ticket_ids)}, found {len(triage_calls)}")

    ok("One drafting LLM call per ticket",
       len(draft_calls) == len(ticket_ids),
       f"Expected {len(ticket_ids)}, found {len(draft_calls)}")

    ok("One review LLM call per ticket",
       len(review_calls) == len(ticket_ids),
       f"Expected {len(ticket_ids)}, found {len(review_calls)}")

    # ── Retrieval exists before drafting (record count) ───────────────────────
    ok("Retrieval results cover all tickets",
       len(retrieval) == len(ticket_ids),
       f"retrieval_results has {len(retrieval)} records for {len(ticket_ids)} tickets")

    # ── Drafts only cite retrieved policies ───────────────────────────────────
    retrieval_by_id = {r["ticket_id"]: set(r.get("retrieved_policy_ids", [])) for r in retrieval}
    citation_violations: list = []
    for draft in drafts:
        tid    = draft["ticket_id"]
        cited  = set(draft.get("cited_policy_ids", []))
        retrieved = retrieval_by_id.get(tid, set())
        orphans = cited - retrieved
        if orphans:
            citation_violations.append(f"{tid}: {sorted(orphans)}")

    ok("Drafts only cite retrieved policies",
       not citation_violations,
       "; ".join(citation_violations))

    # ── Failed checks cause needs_human_review ────────────────────────────────
    final_by_id   = {f["ticket_id"]: f for f in final}
    status_violations: list = []
    for c in checks_data:
        tid = c["ticket_id"]
        if not c.get("passed"):
            f = final_by_id.get(tid, {})
            if f.get("final_status") == "ready":
                status_violations.append(
                    f"{tid}: check failed but final_status is 'ready'"
                )

    ok("Failed checks cause needs_human_review",
       not status_violations,
       "; ".join(status_violations))

    # ── LLM call records have required fields ─────────────────────────────────
    required_call_fields = {
        "stage", "ticket_id", "timestamp", "provider",
        "model", "prompt_hash", "input_artifacts", "output_artifact",
    }
    bad_calls = [
        i for i, c in enumerate(llm_calls)
        if not required_call_fields.issubset(set(c.keys()))
    ]
    ok("All LLM call records have required fields",
       not bad_calls,
       f"Records at indices {bad_calls} are incomplete" if bad_calls else "")

    # ── Triage and drafting are separate call types ───────────────────────────
    ok("Triage and drafting are separate LLM calls",
       len(triage_calls) > 0 and len(draft_calls) > 0
       and len(triage_calls) + len(draft_calls) <= len(llm_calls),
       "")

    return {"checks": checks}


if __name__ == "__main__":
    results = run_validation()
    checks = results["checks"]
    passed = sum(1 for c in checks if c["passed"])
    total  = len(checks)

    print(f"\n{'='*55}")
    print(f"  Deriv Support Pipeline — Validation Report")
    print(f"{'='*55}")
    print(f"  Result: {passed}/{total} checks passed\n")

    for c in checks:
        icon = "✓" if c["passed"] else "✗"
        line = f"  {icon}  {c['name']}"
        print(line)
        if c.get("detail") and not c["passed"]:
            print(f"       → {c['detail']}")

    print(f"\n{'='*55}\n")
    sys.exit(0 if passed == total else 1)
