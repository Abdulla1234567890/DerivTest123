# Support Inbox Pipeline — Understanding & Task Plan

## What This Is

An **offline, replayable AI-assisted support workflow**. Given a folder of customer support tickets and a policy knowledge base (both JSON files on disk), the pipeline classifies each ticket, retrieves relevant policy snippets, drafts a polished customer reply, runs deterministic safety checks, and produces a final reviewed response pack — all without touching any live system.

The evaluator will delete generated artifacts, swap in different fixture files, and re-run from scratch. Every output must be regenerated programmatically; nothing can be precomputed.

---

## Architecture Overview

```
tickets.json  +  policy_kb.json
        │
        ▼
  ┌─────────────┐
  │  INIT        │  Load & validate inputs from disk
  └──────┬──────┘
         │
  ┌──────▼──────┐
  │  TRIAGE      │  1 LLM call per ticket → triage.json
  └──────┬──────┘
         │
  ┌──────▼──────┐
  │  RETRIEVAL   │  Deterministic code (keyword scoring) → retrieval_results.json
  └──────┬──────┘
         │
  ┌──────▼──────┐
  │  DRAFTING    │  1 LLM call per ticket → draft_responses.json
  └──────┬──────┘
         │
  ┌──────▼──────┐
  │  REVIEW      │  1 LLM call per ticket (Stage 3 reviewer) → review_results.json
  └──────┬──────┘
         │
  ┌──────▼──────┐
  │  CHECKS      │  Deterministic code checks → response_checks.json
  └──────┬──────┘
         │
  ┌──────▼──────┐
  │  FINALISE    │  Merge everything → final_responses.json
  └─────────────┘

  All LLM calls logged → llm_calls.jsonl
```

---

## Key Constraints (Non-Negotiable)

| Constraint | Detail |
|---|---|
| Read inputs from disk | `tickets.json` and `policy_kb.json` — no hardcoded content |
| One LLM call per ticket per stage | Triage and Drafting must NOT be merged |
| Deterministic retrieval | Keyword/token overlap in code — runs before any LLM drafting call |
| Deterministic checks | Code-only safety checks before final output |
| No live actions | No emails, no API calls to external business systems |
| Structured JSON outputs | Every artifact must be machine-readable |
| Reproducible | Swap fixtures → pipeline still works correctly |

---

## Stages & What Each Produces

### Stage 0 — Init
- Read `tickets.json` and `policy_kb.json` from disk
- Validate schema (required fields present, non-empty arrays)
- State transitions: `INIT → INPUTS_LOADED → TICKETS_PARSED → KB_INDEXED`

### Stage 1 — Triage (LLM, per ticket)
- One structured LLM call per ticket
- Input: ticket subject + message + allowed categories + escalation rules
- Output per ticket:
  ```json
  { "ticket_id", "category", "priority", "should_escalate", "reason", "missing_information" }
  ```
- Artifact: `triage.json`
- Allowed categories: `withdrawal_issue | payment_issue | verification_issue | account_closure | other`

### Stage 2 — Policy Retrieval (deterministic code)
- For each ticket: score all policy entries by keyword overlap with the ticket text
- Select top 2–3 entries; always include a safety/tone policy when relevant
- Record scores and ranking reasons
- Artifact: `retrieval_results.json`

### Stage 3 — Response Drafting (LLM, per ticket)
- One structured LLM call per ticket
- Input: original ticket + triage result + retrieved policy snippets only
- Rules: polite, no unsupported promises, cite at least one policy ID, escalation note if needed
- Artifact: `draft_responses.json`

### Stage 4 — LLM Reviewer (LLM, per ticket — "Should Attempt")
- One reviewer LLM call per ticket
- Judges: groundedness, safety, clarity, escalation appropriateness
- Artifact: `review_results.json`

### Stage 5 — Deterministic Checks (code)
- Missing citations check
- Citation-not-in-retrieved-set check
- Banned language check: "guarantee", "definitely", "will be approved today", "refund today"
- Empty/too-short reply check
- Escalation mismatch check (triage says escalate but draft doesn't mention it)
- Artifact: `response_checks.json`

### Stage 6 — Finalise
- Merge triage + checks + reviewer results
- `final_status = ready` only when checks pass AND escalation is handled correctly
- `final_status = needs_human_review` when checks fail
- Artifact: `final_responses.json`

### Cross-Cutting — LLM Call Log
- Every LLM call appended to `llm_calls.jsonl`
- Fields: `stage`, `ticket_id`, `timestamp`, `provider`, `model`, `prompt_hash`, `input_artifacts`, `output_artifact`

---

## Configuration & Externalisation

All prompts, banned phrases, categories, and pipeline settings stored in `config.json` (or `config/` directory).  
Changing a banned phrase or prompt template requires editing only that config file, not the pipeline code.

---

## Validation Command

```bash
python validate.py
```

Checks:
- All required artifact files exist
- All JSON files are valid and correctly structured
- Each ticket has a separate triage record
- Each ticket has a separate draft record
- Retrieval records exist before drafts reference them
- Drafts cite only policies from their retrieval set
- Failed checks cause `needs_human_review` in final output
- `llm_calls.jsonl` has the expected number and stage of records

---

## Required Output Files

| File | Produced by |
|---|---|
| `tickets.json` | Input (provided) |
| `policy_kb.json` | Input (provided) |
| `triage.json` | Stage 1 LLM |
| `retrieval_results.json` | Stage 2 deterministic |
| `draft_responses.json` | Stage 3 LLM |
| `review_results.json` | Stage 4 LLM reviewer |
| `response_checks.json` | Stage 5 deterministic checks |
| `final_responses.json` | Stage 6 merge |
| `llm_calls.jsonl` | All LLM stages |
| `validate.py` | Validation tool |
| `README.md` | Documentation |

---

## Task List

### Setup
- [ ] Create project structure (`pipeline.py`, `config.json`, `validate.py`, `README.md`)
- [ ] Create sample `tickets.json` and `policy_kb.json` fixture files
- [ ] Set up Anthropic SDK dependency (`pip install anthropic`)
- [ ] Create `config.json` with prompts, categories, banned phrases, and model settings

### Pipeline Core
- [ ] Implement state machine / stage tracker with state transitions
- [ ] Implement disk loader for `tickets.json` and `policy_kb.json` with schema validation
- [ ] Implement LLM client wrapper that logs every call to `llm_calls.jsonl` (with prompt hash, timestamps, artifacts)

### Stage 1 — Triage
- [ ] Write triage prompt template (externalised in config)
- [ ] Implement per-ticket triage call with structured JSON output enforcement
- [ ] Save results to `triage.json`

### Stage 2 — Retrieval
- [ ] Implement keyword/token overlap scorer between ticket text and policy tags + content
- [ ] Implement top-N selection with safety policy inclusion rule
- [ ] Save results to `retrieval_results.json`

### Stage 3 — Drafting
- [ ] Write drafting prompt template (externalised in config)
- [ ] Implement per-ticket draft call using only that ticket's triage + retrieved policies
- [ ] Save results to `draft_responses.json`

### Stage 4 — Reviewer
- [ ] Write reviewer prompt template (externalised in config)
- [ ] Implement per-ticket reviewer call judging groundedness, safety, clarity, escalation
- [ ] Save results to `review_results.json`

### Stage 5 — Deterministic Checks
- [ ] Implement missing citation check
- [ ] Implement cited-but-not-retrieved policy check
- [ ] Implement banned phrase scanner
- [ ] Implement reply length / empty check
- [ ] Implement escalation mismatch check
- [ ] Save results to `response_checks.json`

### Stage 6 — Finalise
- [ ] Implement merge logic combining triage, checks, and reviewer results
- [ ] Apply `ready` vs `needs_human_review` decision rules
- [ ] Save results to `final_responses.json`

### Validation
- [ ] Implement `validate.py` with all artifact existence checks
- [ ] Add JSON schema validation for each artifact
- [ ] Add per-ticket call count checks against `llm_calls.jsonl`
- [ ] Add retrieval-before-drafting ordering check
- [ ] Add citation integrity check (drafts only cite retrieved policies)
- [ ] Add failed-checks-affect-status assertion

### Documentation
- [ ] Write `README.md` with setup instructions, how to run, how to change config, and artifact descriptions
