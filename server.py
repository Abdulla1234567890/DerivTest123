"""Flask web server — serves the UI and exposes pipeline control endpoints."""

import json
import queue
import threading
from pathlib import Path

from dotenv import load_dotenv
load_dotenv()  # loads GOOGLE_API_KEY from .env if present

from flask import Flask, Response, jsonify, send_file

import pipeline
import validate as _validate

BASE_DIR = Path(__file__).parent
app = Flask(__name__)

_pipeline_lock = threading.Lock()

ARTIFACT_FILES = [
    "triage.json",
    "retrieval_results.json",
    "draft_responses.json",
    "review_results.json",
    "response_checks.json",
    "final_responses.json",
    "llm_calls.jsonl",
]


# ── Routes ────────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    return send_file(BASE_DIR / "index.html")


@app.route("/api/run")
def run():
    """SSE stream that runs the pipeline in a background thread."""
    if not _pipeline_lock.acquire(blocking=False):
        def _busy():
            yield f"data: {json.dumps({'type': 'error', 'message': 'Pipeline already running'})}\n\n"
        return Response(_busy(), mimetype="text/event-stream")

    q: queue.Queue = queue.Queue()

    def _worker():
        try:
            pipeline.run_pipeline(q)
        except Exception as exc:
            q.put({"type": "error", "message": str(exc)})
        finally:
            q.put({"type": "done"})
            _pipeline_lock.release()

    threading.Thread(target=_worker, daemon=True).start()

    def _stream():
        while True:
            try:
                event = q.get(timeout=180)
                yield f"data: {json.dumps(event)}\n\n"
                if event.get("type") in ("done", "error"):
                    break
            except queue.Empty:
                yield f"data: {json.dumps({'type': 'heartbeat'})}\n\n"

    return Response(
        _stream(),
        mimetype="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.route("/api/artifacts")
def artifacts():
    """Return all generated artifact files as a single JSON object."""
    result = {}
    for fname in ARTIFACT_FILES:
        path = BASE_DIR / fname
        if not path.exists():
            continue
        try:
            with open(path) as f:
                if fname.endswith(".jsonl"):
                    result[fname] = [
                        json.loads(line) for line in f if line.strip()
                    ]
                else:
                    result[fname] = json.load(f)
        except Exception as exc:
            result[fname] = {"error": str(exc)}
    return jsonify(result)


@app.route("/api/validate")
def validate():
    """Run deterministic validation checks and return results."""
    try:
        results = _validate.run_validation()
        return jsonify(results)
    except Exception as exc:
        return jsonify({"error": str(exc), "checks": []}), 500


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    print("Starting Deriv Support Pipeline server at http://localhost:5001")
    app.run(debug=False, port=5001, threaded=True)
