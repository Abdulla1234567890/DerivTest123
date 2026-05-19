# Deriv Support Pipeline

An offline, replayable AI-assisted customer support workflow built for the Deriv technical assessment.

Given a set of support tickets and a policy knowledge base, the pipeline classifies each ticket, retrieves relevant policies, drafts a reply, runs an LLM reviewer, applies deterministic safety checks, and produces a final response pack — all traceable and reproducible.

---

## Replacing the Input Files

The pipeline reads `tickets.json` and `policy_kb.json` from disk at runtime. **You can replace both files with your own data** — the pipeline does not depend on specific ticket IDs, wording, or policy titles.

**`tickets.json`** — array of ticket objects, each with:
```json
{
  "ticket_id": "T1",
  "customer_name": "Amina",
  "subject": "Withdrawal still pending after 3 days",
  "message": "Hi, my withdrawal has been...",
  "language": "en"
}
```

**`policy_kb.json`** — array of policy objects, each with:
```json
{
  "policy_id": "P1",
  "title": "Withdrawal processing times",
  "content": "Withdrawals are reviewed...",
  "tags": ["withdrawal", "processing", "timing"]
}
```

Just drop your files in the project root and run the pipeline — everything regenerates automatically.

---

## Quick Start

```bash
# 1. Install dependencies
pip install -r requirements.txt

# 2. Add your Google API key
echo "GOOGLE_API_KEY=AIza..." > .env

# 3. Start the web interface
python server.py

# 4. Open in browser
open http://localhost:5001
```

Click **▶ Run Pipeline** to process all tickets end-to-end.

---

## Pipeline Stages

```
INIT → INPUTS_LOADED → TICKETS_PARSED → KB_INDEXED
     → TICKET_TRIAGED → EVIDENCE_RETRIEVED → RESPONSE_DRAFTED
     → RESPONSE_REVIEWED → RESPONSE_CHECKED → RESPONSE_FINALISED
```

| Stage | Type | Output |
|---|---|---|
| Ticket Triage | LLM (1 call/ticket) | `triage.json` |
| Policy Retrieval | Deterministic keyword scoring | `retrieval_results.json` |
| Response Drafting | LLM (1 call/ticket) | `draft_responses.json` |
| LLM Review | LLM (1 call/ticket) | `review_results.json` |
| Deterministic Checks | Code only | `response_checks.json` |
| Finalise | Merge all results | `final_responses.json` |

Every LLM call is logged to `llm_calls.jsonl` with stage, timestamp, model, and a prompt hash.

---

## Deterministic Safety Checks

Before any ticket is finalised, five code-only checks run:

1. **Missing citations** — reply must cite at least one policy ID
2. **Orphan citations** — cited policies must come from the retrieved set only
3. **Banned phrases** — configurable list (`guarantee`, `refund today`, etc.)
4. **Reply length** — must exceed minimum character count
5. **Escalation mismatch** — if triage flags escalation, the reply must reflect it

A ticket that fails any check gets `final_status: needs_human_review`.

---

## Configuration

All prompts, banned phrases, categories, and model settings live in `config.json` — no hardcoded behaviour in the pipeline code.

| Key | Purpose |
|---|---|
| `model` | Gemini model to use |
| `mock_mode` | `true` = skip API calls, run full flow with deterministic stubs |
| `categories` | Allowed triage categories |
| `banned_phrases` | Triggers a check failure if found in a reply |
| `retrieval.top_n` | Policies retrieved per ticket |
| `prompts.*` | Prompt templates using `${variable}` substitution |

**To switch off mock mode**:
```json
"mock_mode": false
```

---

## Running from CLI

```bash
# Run the pipeline directly (no web server needed)
python pipeline.py

# Validate all output artifacts
python validate.py
```

## Web Interface

The web UI at `http://localhost:5001` provides:
- Real-time stage progress via Server-Sent Events
- Tabbed artifact viewer (Triage, Retrieval, Drafts, Reviews, Checks, Final, LLM Log)
- One-click validation report
- Ticket cards with status badges and policy citations

---

## Project Structure

```
tickets.json          Input: customer support tickets
policy_kb.json        Input: policy knowledge base
config.json           All prompts, settings, tool schemas
pipeline.py           Core pipeline — all 10 stages
server.py             Flask web server + SSE streaming
index.html            Single-page web UI
validate.py           26-point output validation script
requirements.txt      Python dependencies
.env                  API key (not committed)
```

---

## Tech Stack

- **LLM** — Google Gemini via `google-genai` SDK
- **Web server** — Flask with Server-Sent Events for live streaming
- **Retrieval** — Deterministic keyword/token overlap scoring (no embeddings needed)
- **Validation** — Pure Python, 26 automated checks
