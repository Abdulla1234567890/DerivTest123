"""
Deriv Support Pipeline — core pipeline logic.

Stages: INIT -> INPUTS_LOADED -> TICKETS_PARSED -> KB_INDEXED ->
        TICKET_TRIAGED -> EVIDENCE_RETRIEVED -> RESPONSE_DRAFTED ->
        RESPONSE_REVIEWED -> RESPONSE_CHECKED -> RESPONSE_FINALISED
"""

import json
import hashlib
import os
import re
import time
import threading
from datetime import datetime, timezone
from pathlib import Path

from google import genai
from google.genai import types as gentypes
from groq import Groq

BASE_DIR = Path(__file__).parent

STAGE_ORDER = [
    "INIT", "INPUTS_LOADED", "TICKETS_PARSED", "KB_INDEXED",
    "TICKET_TRIAGED", "EVIDENCE_RETRIEVED", "RESPONSE_DRAFTED",
    "RESPONSE_REVIEWED", "RESPONSE_CHECKED", "RESPONSE_FINALISED",
]

_STOP_WORDS = {
    "the","a","an","and","or","but","in","on","at","to","for","of","with","by",
    "is","are","was","were","be","been","has","have","had","do","does","did",
    "will","would","could","should","may","might","can","that","this","it","i",
    "my","your","we","you","me","hi","hello","please","after","still","days",
    "see","one","two","three","today","now","want","need","what","how","why",
    "am","not","if","as","from","so","just","more","then",
}


# ── Utilities ─────────────────────────────────────────────────────────────────

def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _load_config() -> dict:
    with open(BASE_DIR / "config.json") as f:
        return json.load(f)


def _render(template: str, **kwargs) -> str:
    """Replace ${variable} placeholders in a template string."""
    result = template
    for key, value in sorted(kwargs.items(), key=lambda x: -len(x[0])):
        result = result.replace(f"${{{key}}}", str(value))
    return result


def _schema_hint(schema: dict) -> str:
    """Produce a compact field list from a JSON Schema object for the prompt."""
    props = schema.get("properties", {})
    req   = set(schema.get("required", []))
    lines = []
    for name, spec in props.items():
        typ  = spec.get("type", "any")
        if isinstance(typ, list):
            typ = " | ".join(typ)
        enum = f" [one of: {', '.join(spec['enum'])}]" if "enum" in spec else ""
        desc = f" — {spec['description']}" if "description" in spec else ""
        mark = "" if name in req else " (optional)"
        lines.append(f"  {name} ({typ}{enum}){mark}{desc}")
    return "\n".join(lines)


# ── LLM Call Logger ───────────────────────────────────────────────────────────

class LLMLogger:
    def __init__(self):
        self.path = BASE_DIR / "llm_calls.jsonl"
        self._lock = threading.Lock()

    def reset(self):
        with open(self.path, "w"):
            pass

    def log(self, *, stage, ticket_id, provider, model, prompt_text,
            input_artifacts, output_artifact):
        record = {
            "stage": stage,
            "ticket_id": ticket_id,
            "timestamp": _now(),
            "provider": provider,
            "model": model,
            "prompt_hash": hashlib.sha256(prompt_text.encode()).hexdigest(),
            "input_artifacts": input_artifacts,
            "output_artifact": output_artifact,
        }
        with self._lock:
            with open(self.path, "a") as f:
                f.write(json.dumps(record) + "\n")


# ── Deterministic Policy Retrieval ────────────────────────────────────────────

def _tokenise(text: str) -> set:
    return set(re.findall(r"\w+", text.lower())) - _STOP_WORDS


def _score_policy(ticket_text: str, policy: dict) -> float:
    t = _tokenise(ticket_text)
    tag_score   = sum(2.0 for tag in policy["tags"] if tag in t)
    title_score = len(t & _tokenise(policy["title"])) * 1.5
    body_score  = len(t & _tokenise(policy["content"])) * 0.5
    return tag_score + title_score + body_score


def _retrieve(ticket: dict, policies: list, config: dict) -> dict:
    text = f"{ticket['subject']} {ticket['message']}"
    scored = sorted(
        [(policy, _score_policy(text, policy)) for policy in policies],
        key=lambda x: -x[1],
    )

    top_n = config["retrieval"]["top_n"]
    top   = scored[:top_n]
    top_ids = {p["policy_id"] for p, _ in top}

    # Always include at least one safety/tone policy
    safety_tag = config["retrieval"].get("safety_policy_tag", "safety")
    if not any(safety_tag in p["tags"] for p, _ in top):
        for policy, score in scored:
            if safety_tag in policy["tags"] and policy["policy_id"] not in top_ids:
                top.append((policy, score))
                break

    top.sort(key=lambda x: -x[1])

    return {
        "ticket_id": ticket["ticket_id"],
        "retrieved_policy_ids": [p["policy_id"] for p, _ in top],
        "ranking_explanation": "; ".join(
            f"{p['policy_id']} score={s:.2f}" for p, s in top
        ),
    }


# ── LLM Caller (Google Gemini) ────────────────────────────────────────────────

_TOOL_KEY = {
    "triage_ticket":   "triage",
    "draft_response":  "drafting",
    "review_response": "review",
}


def _mock_response(tool_name: str, ticket_id: str, prompt: str) -> dict:
    """
    Deterministic stand-in used when mock_mode=true in config.json.
    Derives plausible values from the prompt text so the full pipeline
    flow (retrieval, checks, finalisation) can be tested without API calls.
    """
    p = prompt.lower()

    if tool_name == "triage_ticket":
        if "withdrawal" in p:
            cat, prio, esc = "withdrawal_issue", "medium", False
        elif "charge" in p or "deposit" in p or "refund" in p:
            cat, prio, esc = "payment_issue", "high", True
        elif "verif" in p or "kyc" in p or "id" in p or "approve" in p:
            cat, prio, esc = "verification_issue", "medium", True
        elif "clos" in p or "delete" in p:
            cat, prio, esc = "account_closure", "low", False
        else:
            cat, prio, esc = "other", "low", False
        return {
            "ticket_id": ticket_id,
            "category": cat,
            "priority": prio,
            "should_escalate": esc,
            "reason": f"[MOCK] Classified as {cat} based on keyword matching.",
            "missing_information": [],
        }

    if tool_name == "draft_response":
        # Extract customer name from prompt
        name_m = re.search(r"Customer:\s*(\w+)", prompt)
        name   = name_m.group(1) if name_m else "Customer"
        # Extract cited policy IDs from the prompt's policy snippets section
        pids   = re.findall(r"\[(P\d+)\]", prompt)
        return {
            "ticket_id": ticket_id,
            "subject": "Re: Your Support Request",
            "reply": (
                f"Dear {name},\n\n"
                "Thank you for reaching out to Deriv Support. "
                "We have received your request and our team is reviewing it carefully. "
                "Please allow 1–3 business days for processing. "
                "We will keep you updated at every step. "
                "If you have any additional questions, feel free to reply to this message.\n\n"
                "Kind regards,\nDeriv Support Team"
            ),
            "cited_policy_ids": pids[:2] if pids else [],
            "escalation_note": "[MOCK] Flagged for team review." if "escalat" in p else None,
        }

    if tool_name == "review_response":
        return {
            "ticket_id": ticket_id,
            "approved": True,
            "issues": [],
            "suggested_fix": "",
        }

    return {}


def _call_llm(client, config, *, tool_name, prompt, stage, ticket_id,
              input_artifacts, output_artifact, llm_logger) -> dict:
    key    = _TOOL_KEY[tool_name]
    schema = config["tools"][key]["input_schema"]
    hint   = _schema_hint(schema)

    full_prompt = (
        f"{prompt}\n\n"
        f"Respond with ONLY a valid JSON object containing exactly these fields:\n{hint}"
    )

    # ── Mock mode — skip the API call entirely ────────────────────────────────
    if config.get("mock_mode"):
        result = _mock_response(tool_name, ticket_id, prompt)
        llm_logger.log(
            stage=stage,
            ticket_id=ticket_id,
            provider="mock",
            model="mock",
            prompt_text=full_prompt,
            input_artifacts=input_artifacts,
            output_artifact=output_artifact,
        )
        return result

    # ── Live API call ─────────────────────────────────────────────────────────
    provider = config["provider"]

    if provider == "groq":
        response = client.chat.completions.create(
            model=config["model"],
            messages=[{"role": "user", "content": full_prompt}],
            response_format={"type": "json_object"},
            temperature=0.2,
        )
        text = response.choices[0].message.content.strip()

    elif provider == "google":
        response = client.models.generate_content(
            model=config["model"],
            contents=full_prompt,
            config=gentypes.GenerateContentConfig(
                response_mime_type="application/json",
                temperature=0.2,
            ),
        )
        text = response.text.strip()

    else:
        raise ValueError(f"Unknown provider '{provider}'. Use 'groq' or 'google'.")

    llm_logger.log(
        stage=stage,
        ticket_id=ticket_id,
        provider=provider,
        model=config["model"],
        prompt_text=full_prompt,
        input_artifacts=input_artifacts,
        output_artifact=output_artifact,
    )

    # Fallback: pull JSON object out if model wrapped it in markdown
    if not text.startswith("{"):
        m = re.search(r"\{.*\}", text, re.DOTALL)
        if m:
            text = m.group(0)

    return json.loads(text)


# ── Pipeline ──────────────────────────────────────────────────────────────────

def run_pipeline(progress_queue=None):
    """
    Run the full pipeline. Pass a queue.Queue to receive progress events,
    or leave None to print progress to stdout.
    """

    def emit(event: dict):
        if progress_queue:
            progress_queue.put(event)
        label = event.get("message") or event.get("stage") or str(event)
        print(f"[{event.get('type','log').upper()}] {label}")

    def log(msg: str):
        emit({"type": "log", "message": msg})

    def stage_start(s: str):
        emit({"type": "stage_start", "stage": s})

    def stage_done(s: str):
        emit({"type": "stage_done", "stage": s})

    try:
        # ── INIT ──────────────────────────────────────────────────────────────
        stage_start("INIT")
        config     = _load_config()
        llm_logger = LLMLogger()
        llm_logger.reset()

        if config.get("mock_mode"):
            client = None
            log("Running in MOCK mode — no API calls will be made")
        elif config["provider"] == "groq":
            api_key = os.environ.get("GROQ_API_KEY")
            if not api_key:
                raise EnvironmentError(
                    "GROQ_API_KEY environment variable is not set. "
                    "Add it to your .env file: GROQ_API_KEY=gsk_..."
                )
            client = Groq(api_key=api_key)
            log(f"Using Groq model: {config['model']}")
        elif config["provider"] == "google":
            api_key = os.environ.get("GOOGLE_API_KEY")
            if not api_key:
                raise EnvironmentError(
                    "GOOGLE_API_KEY environment variable is not set. "
                    "Add it to your .env file: GOOGLE_API_KEY=AIza..."
                )
            client = genai.Client(api_key=api_key)
            log(f"Using Google model: {config['model']}")
        else:
            raise ValueError(f"Unknown provider: {config['provider']}")
        stage_done("INIT")

        # ── INPUTS_LOADED ─────────────────────────────────────────────────────
        stage_start("INPUTS_LOADED")
        with open(BASE_DIR / "tickets.json") as f:
            tickets = json.load(f)
        with open(BASE_DIR / "policy_kb.json") as f:
            policies = json.load(f)
        log(f"Loaded {len(tickets)} tickets and {len(policies)} policies from disk")
        stage_done("INPUTS_LOADED")

        # ── TICKETS_PARSED ────────────────────────────────────────────────────
        stage_start("TICKETS_PARSED")
        required_fields = {"ticket_id", "customer_name", "subject", "message"}
        for t in tickets:
            missing = required_fields - set(t.keys())
            if missing:
                raise ValueError(
                    f"Ticket {t.get('ticket_id','?')} missing fields: {missing}"
                )
        log(f"All {len(tickets)} tickets validated")
        stage_done("TICKETS_PARSED")

        # ── KB_INDEXED ────────────────────────────────────────────────────────
        stage_start("KB_INDEXED")
        policy_index = {p["policy_id"]: p for p in policies}
        log(f"Indexed {len(policy_index)} policies")
        stage_done("KB_INDEXED")

        # ── TICKET_TRIAGED ────────────────────────────────────────────────────
        stage_start("TICKET_TRIAGED")
        triage_results = []
        categories_list = "\n".join(f"- {c}" for c in config["categories"])

        for ticket in tickets:
            tid = ticket["ticket_id"]
            log(f"Triaging {tid}: {ticket['subject']}")
            prompt = _render(
                config["prompts"]["triage"],
                categories_list=categories_list,
                ticket_id=tid,
                customer_name=ticket["customer_name"],
                subject=ticket["subject"],
                message=ticket["message"],
            )
            result = _call_llm(
                client, config,
                tool_name="triage_ticket",
                prompt=prompt,
                stage="triage",
                ticket_id=tid,
                input_artifacts=["tickets.json"],
                output_artifact="triage.json",
                llm_logger=llm_logger,
            )
            result["ticket_id"] = tid
            triage_results.append(result)
            log(f"  → {result.get('category')} | {result.get('priority')} | "
                f"escalate={result.get('should_escalate')}")

        (BASE_DIR / "triage.json").write_text(
            json.dumps(triage_results, indent=2)
        )
        stage_done("TICKET_TRIAGED")

        # ── EVIDENCE_RETRIEVED ────────────────────────────────────────────────
        stage_start("EVIDENCE_RETRIEVED")
        retrieval_results = []
        for ticket in tickets:
            result = _retrieve(ticket, policies, config)
            retrieval_results.append(result)
            log(f"  {ticket['ticket_id']}: {result['retrieved_policy_ids']}")

        (BASE_DIR / "retrieval_results.json").write_text(
            json.dumps(retrieval_results, indent=2)
        )
        stage_done("EVIDENCE_RETRIEVED")

        # ── RESPONSE_DRAFTED ──────────────────────────────────────────────────
        stage_start("RESPONSE_DRAFTED")
        triage_by_id    = {t["ticket_id"]: t for t in triage_results}
        retrieval_by_id = {r["ticket_id"]: r for r in retrieval_results}
        draft_responses = []

        for ticket in tickets:
            tid       = ticket["ticket_id"]
            triage    = triage_by_id[tid]
            retrieval = retrieval_by_id[tid]
            retrieved_policies = [
                policy_index[pid]
                for pid in retrieval["retrieved_policy_ids"]
                if pid in policy_index
            ]
            policies_text = "\n\n".join(
                f"[{p['policy_id']}] {p['title']}\n{p['content']}"
                for p in retrieved_policies
            )

            log(f"Drafting reply for {tid}")
            prompt = _render(
                config["prompts"]["drafting"],
                ticket_id=tid,
                subject=ticket["subject"],
                customer_name=ticket["customer_name"],
                message=ticket["message"],
                category=triage["category"],
                priority=triage["priority"],
                should_escalate=triage["should_escalate"],
                reason=triage["reason"],
                policies_text=policies_text,
            )
            result = _call_llm(
                client, config,
                tool_name="draft_response",
                prompt=prompt,
                stage="drafting",
                ticket_id=tid,
                input_artifacts=["tickets.json", "triage.json", "retrieval_results.json"],
                output_artifact="draft_responses.json",
                llm_logger=llm_logger,
            )
            result["ticket_id"] = tid
            draft_responses.append(result)
            log(f"  → cited: {result.get('cited_policy_ids', [])}")

        (BASE_DIR / "draft_responses.json").write_text(
            json.dumps(draft_responses, indent=2)
        )
        stage_done("RESPONSE_DRAFTED")

        # ── RESPONSE_REVIEWED ─────────────────────────────────────────────────
        stage_start("RESPONSE_REVIEWED")
        draft_by_id    = {d["ticket_id"]: d for d in draft_responses}
        review_results = []

        for ticket in tickets:
            tid       = ticket["ticket_id"]
            draft     = draft_by_id[tid]
            triage    = triage_by_id[tid]
            retrieval = retrieval_by_id[tid]
            retrieved_policies = [
                policy_index[pid]
                for pid in retrieval["retrieved_policy_ids"]
                if pid in policy_index
            ]
            policies_text = "\n\n".join(
                f"[{p['policy_id']}] {p['title']}\n{p['content']}"
                for p in retrieved_policies
            )

            log(f"Reviewing draft for {tid}")
            prompt = _render(
                config["prompts"]["review"],
                ticket_id=tid,
                customer_name=ticket["customer_name"],
                subject=ticket["subject"],
                ticket_message=ticket["message"],
                policies_text=policies_text,
                reply=draft["reply"],
                category=triage["category"],
                should_escalate=triage["should_escalate"],
            )
            result = _call_llm(
                client, config,
                tool_name="review_response",
                prompt=prompt,
                stage="review",
                ticket_id=tid,
                input_artifacts=[
                    "draft_responses.json", "triage.json", "retrieval_results.json"
                ],
                output_artifact="review_results.json",
                llm_logger=llm_logger,
            )
            result["ticket_id"] = tid
            review_results.append(result)
            status = "Approved" if result.get("approved") else "Issues found"
            log(f"  → {status}: {result.get('issues', [])}")

        (BASE_DIR / "review_results.json").write_text(
            json.dumps(review_results, indent=2)
        )
        stage_done("RESPONSE_REVIEWED")

        # ── RESPONSE_CHECKED ──────────────────────────────────────────────────
        stage_start("RESPONSE_CHECKED")
        banned_phrases  = config["banned_phrases"]
        min_reply_len   = config.get("min_reply_length", 50)
        response_checks = []
        _ESCALATION_KW  = {
            "escalat", "team", "investigat", "review", "contact",
            "specialist", "follow up", "get back", "look into", "pass",
            "refer", "forward", "colleague",
        }

        for ticket in tickets:
            tid       = ticket["ticket_id"]
            draft     = draft_by_id[tid]
            triage    = triage_by_id[tid]
            retrieval = retrieval_by_id[tid]
            issues    = []

            reply         = draft.get("reply", "")
            cited         = draft.get("cited_policy_ids", [])
            retrieved_ids = set(retrieval["retrieved_policy_ids"])
            reply_lower   = reply.lower()

            # Check 1 — missing citations
            if not cited:
                issues.append("No policy citations in reply")

            # Check 2 — citations not in retrieved set
            orphans = [pid for pid in cited if pid not in retrieved_ids]
            if orphans:
                issues.append(f"Cited policies not in retrieved set: {orphans}")

            # Check 3 — banned phrases
            for phrase in banned_phrases:
                if phrase.lower() in reply_lower:
                    issues.append(f"Banned phrase found: '{phrase}'")

            # Check 4 — reply too short
            if len(reply.strip()) < min_reply_len:
                issues.append(
                    f"Reply too short ({len(reply.strip())} chars, "
                    f"minimum is {min_reply_len})"
                )

            # Check 5 — escalation mismatch
            if triage.get("should_escalate"):
                has_escalation = any(kw in reply_lower for kw in _ESCALATION_KW)
                if not has_escalation:
                    issues.append(
                        "Triage requires escalation but reply lacks "
                        "escalation language"
                    )

            response_checks.append({
                "ticket_id": tid,
                "passed": len(issues) == 0,
                "issues": issues,
            })
            status = "PASS" if not issues else f"FAIL ({len(issues)} issue(s))"
            log(f"  {tid}: {status}")

        (BASE_DIR / "response_checks.json").write_text(
            json.dumps(response_checks, indent=2)
        )
        stage_done("RESPONSE_CHECKED")

        # ── RESPONSE_FINALISED ────────────────────────────────────────────────
        stage_start("RESPONSE_FINALISED")
        checks_by_id  = {c["ticket_id"]: c for c in response_checks}
        reviews_by_id = {r["ticket_id"]: r for r in review_results}
        final_responses = []

        for ticket in tickets:
            tid    = ticket["ticket_id"]
            triage = triage_by_id[tid]
            draft  = draft_by_id[tid]
            check  = checks_by_id[tid]
            review = reviews_by_id[tid]

            notes = list(check["issues"])
            if not review.get("approved"):
                notes.extend(review.get("issues", []))
            if review.get("suggested_fix"):
                notes.append(f"Reviewer suggestion: {review['suggested_fix']}")

            escalation_ok = (
                not triage.get("should_escalate")
                or any(kw in draft["reply"].lower() for kw in _ESCALATION_KW)
            )
            final_status = (
                "ready"
                if check["passed"] and review.get("approved") and escalation_ok
                else "needs_human_review"
            )

            final_responses.append({
                "ticket_id": tid,
                "category": triage["category"],
                "priority": triage["priority"],
                "final_status": final_status,
                "reply": draft["reply"],
                "supporting_policy_ids": draft.get("cited_policy_ids", []),
                "notes": notes,
            })
            log(f"  {tid}: {final_status}")

        (BASE_DIR / "final_responses.json").write_text(
            json.dumps(final_responses, indent=2)
        )
        stage_done("RESPONSE_FINALISED")

        emit({"type": "done", "message": "Pipeline completed successfully"})

    except Exception as exc:
        emit({"type": "error", "message": str(exc)})
        raise


# ── CLI entry point ───────────────────────────────────────────────────────────

if __name__ == "__main__":
    run_pipeline()
