import os
import time
import hashlib
import threading
import json
import uuid

# Optional Redis + RQ (recommended for production)
try:
    import redis  # type: ignore
    from rq import Queue  # type: ignore

    _redis_ok = True
except Exception:
    redis = None  # type: ignore
    Queue = None  # type: ignore
    _redis_ok = False

# If TensorFlow is partially installed (common on Windows), Transformers may
# detect it and crash at import time. Force TF off since this app uses PyTorch.
os.environ.setdefault("USE_TF", "0")
os.environ.setdefault("TRANSFORMERS_NO_TF", "1")

from flask import Flask, Response, jsonify, render_template, request
import google.generativeai as genai
from google.api_core import exceptions as gexc
import fitz  # PyMuPDF
from sentence_transformers import SentenceTransformer, util
from dotenv import load_dotenv
import re
import tempfile

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
load_dotenv(os.path.join(BASE_DIR, ".env"))
load_dotenv()


class ServiceError(Exception):
    def __init__(self, message: str, status_code: int = 503):
        super().__init__(message)
        self.message = message
        self.status_code = status_code


def get_gemini_api_key() -> str:
    return (os.environ.get("GEMINI_API_KEY") or "").strip()


if not get_gemini_api_key():
    print("WARNING: GEMINI_API_KEY not set. Set it in env or .env.")

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 10 * 1024 * 1024  # 10MB upload limit

_embedder = None
_gemini_cache: dict[str, dict] = {}
_gemini_cache_order: list[str] = []
_GEMINI_CACHE_MAX = 32
_gemini_lock = threading.Lock()
_gemini_last_call_ts = 0.0
_gemini_min_interval_s = 3.0

_ip_hits: dict[str, list[float]] = {}
_ip_lock = threading.Lock()
_ip_window_s = 60.0
_ip_max_requests = 20

_jobs: dict[str, dict] = {}
_jobs_lock = threading.Lock()
_jobs_ttl_s = 30 * 60  # 30 minutes


def _client_ip() -> str:
    forwarded = (request.headers.get("X-Forwarded-For") or "").split(",")[0].strip()
    return forwarded or (request.remote_addr or "unknown")

def _redis_url() -> str:
    return (os.environ.get("REDIS_URL") or "").strip()


def get_redis():
    if not _redis_ok:
        return None
    url = _redis_url()
    if not url:
        return None
    try:
        return redis.from_url(url)  # type: ignore[union-attr]
    except Exception:
        return None


def get_queue():
    conn = get_redis()
    if conn is None or Queue is None:
        return None
    name = (os.environ.get("RQ_QUEUE") or "default").strip() or "default"
    return Queue(name, connection=conn)  # type: ignore[misc]


def enforce_ip_rate_limit() -> None:
    ip = _client_ip()
    now = time.time()
    conn = get_redis()
    if conn is not None:
        window = float(os.environ.get("RATE_LIMIT_WINDOW_S") or _ip_window_s)
        max_req = int(os.environ.get("RATE_LIMIT_MAX") or _ip_max_requests)
        bucket = int(now // window)
        key = f"rl:{ip}:{bucket}"
        try:
            count = conn.incr(key)
            if count == 1:
                conn.expire(key, int(window))
            if int(count) > max_req:
                ttl = int(conn.ttl(key)) or int(window)
                raise ServiceError(f"Too many requests. Please wait {ttl}s and try again.", status_code=429)
            return
        except ServiceError:
            raise
        except Exception:
            # Fall back to in-memory if Redis is unreachable.
            pass

    with _ip_lock:
        hits = _ip_hits.get(ip, [])
        hits = [t for t in hits if now - t < _ip_window_s]
        if len(hits) >= _ip_max_requests:
            raise ServiceError("Too many requests. Please wait a minute and try again.", status_code=429)
        hits.append(now)
        _ip_hits[ip] = hits


def _cleanup_jobs() -> None:
    if get_redis() is not None:
        return
    now = time.time()
    with _jobs_lock:
        expired = [job_id for job_id, job in _jobs.items() if now - job.get("created_at", now) > _jobs_ttl_s]
        for job_id in expired:
            job = _jobs.pop(job_id, None)
            if not job:
                continue
            tmp_path = job.get("tmp_path")
            if tmp_path and os.path.exists(tmp_path):
                try:
                    os.remove(tmp_path)
                except OSError:
                    pass


def _job_key(job_id: str) -> str:
    return f"job:{job_id}"


def create_job(job_id: str, tmp_path: str) -> None:
    conn = get_redis()
    if conn is not None:
        conn.hset(
            _job_key(job_id),
            mapping={
                "status": "processing",
                "created_at": str(time.time()),
                "tmp_path": tmp_path,
                "result": "",
                "error": "",
            },
        )
        conn.expire(_job_key(job_id), int(_jobs_ttl_s))
        return
    with _jobs_lock:
        _jobs[job_id] = {
            "status": "processing",
            "created_at": time.time(),
            "result": None,
            "error": "",
            "tmp_path": tmp_path,
        }


def set_job_done(job_id: str, result: dict) -> None:
    conn = get_redis()
    if conn is not None:
        conn.hset(_job_key(job_id), mapping={"status": "done", "result": json.dumps(result), "error": ""})
        conn.expire(_job_key(job_id), int(_jobs_ttl_s))
        return
    with _jobs_lock:
        job = _jobs.get(job_id)
        if job is not None:
            job["status"] = "done"
            job["result"] = result
            job["error"] = ""


def set_job_error(job_id: str, message: str) -> None:
    conn = get_redis()
    if conn is not None:
        conn.hset(_job_key(job_id), mapping={"status": "error", "error": message})
        conn.expire(_job_key(job_id), int(_jobs_ttl_s))
        return
    with _jobs_lock:
        job = _jobs.get(job_id)
        if job is not None:
            job["status"] = "error"
            job["error"] = message
            job["result"] = None


def get_job(job_id: str) -> dict | None:
    conn = get_redis()
    if conn is not None:
        data = conn.hgetall(_job_key(job_id))
        if not data:
            return None
        decoded = {k.decode("utf-8", "ignore"): v.decode("utf-8", "ignore") for k, v in data.items()}
        status = decoded.get("status", "processing")
        result_raw = decoded.get("result", "")
        result = None
        if result_raw:
            try:
                result = json.loads(result_raw)
            except Exception:
                result = None
        return {
            "status": status,
            "error": decoded.get("error", ""),
            "result": result,
        }
    with _jobs_lock:
        job = _jobs.get(job_id)
        if not job:
            return None
        return {
            "status": job.get("status", "processing"),
            "error": job.get("error", ""),
            "result": job.get("result"),
        }


def get_embedder() -> SentenceTransformer:
    global _embedder
    if _embedder is None:
        try:
            _embedder = SentenceTransformer("all-MiniLM-L6-v2")
        except Exception as exc:
            raise ServiceError(
                "Failed to load the embedding model (all-MiniLM-L6-v2). "
                "Check your internet connection on first run, then restart the server.",
                status_code=503,
            ) from exc
    return _embedder


# Helpers

def extract_text_from_pdf(pdf_path: str) -> str:
    """Extract plain text from an uploaded PDF resume."""
    try:
        doc = fitz.open(pdf_path)
    except Exception as exc:
        raise ValueError("Could not read PDF. Please upload a valid text-based PDF.") from exc
    text = ""
    for page in doc:
        text += page.get_text()
    doc.close()
    return text.strip()


def compute_match_score(resume_text: str, jd_text: str) -> int:
    """Semantic similarity score (0-100) between resume and JD."""
    embedder = get_embedder()
    emb_resume = embedder.encode(resume_text, convert_to_tensor=True)
    emb_jd = embedder.encode(jd_text, convert_to_tensor=True)
    semantic = util.cos_sim(emb_resume, emb_jd).item()
    semantic = max(0.0, min(1.0, float(semantic)))

    jd_keywords = set(_top_keywords(jd_text, limit=40))
    resume_tokens = set(_tokenize(resume_text))
    keyword_recall = (len(jd_keywords & resume_tokens) / max(1, len(jd_keywords))) if jd_keywords else 0.0

    blended = (0.75 * semantic) + (0.25 * keyword_recall)
    return max(0, min(100, int(round(blended * 100))))


def analyze_with_gemini(resume_text: str, jd_text: str, score: int, api_key_override: str = "") -> dict:
    """Call Gemini to get structured analysis."""
    api_key = (api_key_override or get_gemini_api_key()).strip()
    if not api_key:
        raise ServiceError(
            "Gemini API key not set. Add GEMINI_API_KEY on the server, or paste your own key in the UI.",
            status_code=503,
        )

    cache_key_src = f"{score}\n{resume_text}\n---\n{jd_text}".encode("utf-8", "ignore")
    cache_key = hashlib.sha256(cache_key_src).hexdigest()
    cached = _gemini_cache.get(cache_key)
    if cached is not None:
        return cached

    prompt = f"""
You are an expert technical recruiter and resume coach.

Return ONLY valid JSON (no markdown, no commentary). Use the schema:
{{
  "matching_skills": string[],
  "missing_skills": string[],
  "suggestions": string[],           // 3-5 very specific actions
  "recruiter_summary": string,       // 1 sentence
  "verdict": "Strong Fit"|"Good Fit"|"Partial Fit"|"Weak Fit",
  "evidence": [                      // up to 5 items
    {{
      "requirement": string,
      "evidence": string,            // short quote or paraphrase from resume
      "confidence": "High"|"Medium"|"Low"
    }}
  ]
}}

## Job Description
{jd_text}

## Candidate Resume
{resume_text}

## Semantic Match Score (already computed): {score}/100

Constraints:
- Prefer concrete skills/tech and measurable impact over generic words.
- If the resume text lacks proof for a requirement, mark it as missing.
- Keep skills to short tokens (e.g., "Python", "Flask", "AWS", "SQL", "Docker").
"""

    # Serialize Gemini calls and add a small cooldown to reduce 429s when users
    # double-click or the browser retries requests.
    response = None
    last_error = None
    global _gemini_last_call_ts
    with _gemini_lock:
        now = time.time()
        elapsed = now - _gemini_last_call_ts
        if elapsed < _gemini_min_interval_s:
            wait_s = int(_gemini_min_interval_s - elapsed) + 1
            raise ServiceError(
                f"Too many requests. Please wait {wait_s}s and try again.",
                status_code=429,
            )
        _gemini_last_call_ts = now

        genai.configure(api_key=api_key)
        # Try a small set of compatible model IDs because availability can vary
        # by account, API version, and rollout status.
        model_candidates = [
            "gemini-1.5-flash-latest",
            "gemini-1.5-flash",
            "gemini-2.0-flash",
        ]

        for model_name in model_candidates:
            try:
                model = genai.GenerativeModel(model_name)
                response = model.generate_content(
                    prompt,
                    generation_config={
                        "temperature": 0.2,
                        "max_output_tokens": 1200,
                    },
                )
                break
            except gexc.ResourceExhausted as exc:
                raise ServiceError(
                    "Too many requests to Gemini (rate limit/quota). Wait 30-60 seconds and try again.",
                    status_code=429,
                ) from exc
            except (gexc.Unauthenticated, gexc.PermissionDenied) as exc:
                raise ServiceError(
                    "Gemini authentication failed. Check that GEMINI_API_KEY is valid and enabled.",
                    status_code=401,
                ) from exc
            except gexc.InvalidArgument as exc:
                raise ServiceError(
                    "Gemini request was rejected (invalid input). Try shorter text.",
                    status_code=400,
                ) from exc
            except gexc.GoogleAPICallError as exc:
                last_error = exc
            except Exception as exc:
                last_error = exc

    if response is None:
        raise ServiceError("Gemini service temporarily unavailable. Please try again later.", status_code=503) from last_error

    raw = (response.text or "").strip()

    result: dict = {}
    try:
        result = json.loads(raw)
    except Exception:
        # Sometimes models wrap JSON in code fences. Extract the first JSON object.
        match = re.search(r"\{.*\}", raw, flags=re.DOTALL)
        if match:
            result = json.loads(match.group(0))
        else:
            raise ServiceError("LLM returned an unexpected response format. Please try again.", status_code=502)

    if not isinstance(result, dict):
        raise ServiceError("LLM returned an unexpected response format. Please try again.", status_code=502)

    result.setdefault("matching_skills", [])
    result.setdefault("missing_skills", [])
    result.setdefault("suggestions", [])
    result.setdefault("recruiter_summary", "")
    result.setdefault("verdict", "")
    result.setdefault("evidence", [])

    _gemini_cache[cache_key] = result
    _gemini_cache_order.append(cache_key)
    if len(_gemini_cache_order) > _GEMINI_CACHE_MAX:
        old = _gemini_cache_order.pop(0)
        _gemini_cache.pop(old, None)

    return result


_STOPWORDS = {
    "a", "an", "and", "or", "the", "to", "of", "in", "for", "on", "with", "as", "at", "by",
    "is", "are", "was", "were", "be", "been", "being", "this", "that", "these", "those",
    "you", "your", "we", "our", "they", "their", "i", "me", "my", "from", "will", "can",
    "should", "must", "may", "not", "no", "yes", "it", "its", "etc",
}


def _tokenize(text: str) -> list[str]:
    text = re.sub(r"[^a-zA-Z0-9\+\#\.\- ]+", " ", text.lower())
    tokens = [t.strip(".-") for t in text.split()]
    return [t for t in tokens if len(t) >= 3 and t not in _STOPWORDS]


def redact_pii(text: str) -> str:
    # Conservative redaction of common personal identifiers before sending to an LLM.
    # Local scoring/fallback can still use the original text.
    patterns = [
        (r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b", "[EMAIL]"),
        (r"\b(?:\+?\d{1,3}[\s.-]?)?(?:\(?\d{2,4}\)?[\s.-]?)?\d{3,4}[\s.-]?\d{4}\b", "[PHONE]"),
        (r"\bhttps?://\S+\b", "[URL]"),
        (r"\b(?:www\.)\S+\b", "[URL]"),
        (r"\b(?:linkedin\.com|github\.com|gitlab\.com|bitbucket\.org)/\S+\b", "[URL]"),
    ]
    redacted = text
    for pattern, repl in patterns:
        redacted = re.sub(pattern, repl, redacted, flags=re.IGNORECASE)
    return redacted


def _top_keywords(text: str, limit: int = 25) -> list[str]:
    freq: dict[str, int] = {}
    for t in _tokenize(text):
        freq[t] = freq.get(t, 0) + 1
    return [k for k, _ in sorted(freq.items(), key=lambda kv: (-kv[1], kv[0]))[:limit]]


def fallback_analysis(resume_text: str, jd_text: str, score: int) -> dict:
    jd_keywords = _top_keywords(jd_text, limit=30)
    resume_tokens = set(_tokenize(resume_text))
    matching = [k for k in jd_keywords if k in resume_tokens][:20]
    missing = [k for k in jd_keywords if k not in resume_tokens][:20]

    if score >= 75:
        verdict = "Strong Fit"
    elif score >= 60:
        verdict = "Good Fit"
    elif score >= 45:
        verdict = "Partial Fit"
    else:
        verdict = "Weak Fit"

    suggestions: list[str] = []
    if missing:
        suggestions.append(f"Add evidence of these missing keywords where applicable: {', '.join(missing[:8])}.")
    suggestions.append("Mirror the job description language in your summary and bullet points (without keyword stuffing).")
    suggestions.append("Quantify impact in experience bullets (metrics, scope, outcomes) and put the best projects first.")

    summary_bits = []
    if matching:
        summary_bits.append(f"Matches keywords like {', '.join(matching[:6])}.")
    if missing:
        summary_bits.append(f"Missing keywords like {', '.join(missing[:6])}.")
    recruiter_summary = " ".join(summary_bits) or "Resume analyzed locally (LLM unavailable)."

    return {
        "matching_skills": matching,
        "missing_skills": missing,
        "suggestions": suggestions[:3],
        "recruiter_summary": recruiter_summary,
        "verdict": verdict,
        "note": "LLM unavailable (rate limit/quota). Returned a local heuristic analysis.",
        "evidence": [],
    }


def run_analysis_from_path(pdf_path: str, job_description: str, api_key_override: str = "") -> dict:
    resume_text = extract_text_from_pdf(pdf_path)
    if not resume_text:
        raise ValueError("Could not extract text from PDF. Make sure it is not a scanned image.")

    score = compute_match_score(resume_text, job_description)
    redacted_resume = redact_pii(resume_text)
    redacted_jd = redact_pii(job_description)
    try:
        analysis = analyze_with_gemini(
            redacted_resume,
            redacted_jd,
            score,
            api_key_override=api_key_override,
        )
    except ServiceError as exc:
        if exc.status_code in (429, 503):
            analysis = fallback_analysis(resume_text, job_description, score)
        else:
            raise

    note_bits: list[str] = []
    if analysis.get("note"):
        note_bits.append(str(analysis.get("note")))
    note_bits.append("PII is redacted before sending text to the LLM.")

    return {
        "score": score,
        "verdict": analysis.get("verdict", ""),
        "matching_skills": analysis.get("matching_skills", []),
        "missing_skills": analysis.get("missing_skills", []),
        "suggestions": analysis.get("suggestions", []),
        "recruiter_summary": analysis.get("recruiter_summary", ""),
        "note": " ".join(note_bits).strip(),
        "evidence": analysis.get("evidence", []),
    }


def process_job(job_id: str, pdf_path: str, job_description: str, api_key_override: str = "") -> None:
    try:
        result = run_analysis_from_path(pdf_path, job_description, api_key_override=api_key_override)
        set_job_done(job_id, result)
    except Exception as exc:
        message = "Analysis failed. Please try again."
        if isinstance(exc, ValueError):
            message = str(exc)
        elif isinstance(exc, ServiceError):
            message = exc.message
        elif app.debug:
            message = f"{message} ({type(exc).__name__}: {exc})"
        else:
            app.logger.exception("Analyze job failed")
        set_job_error(job_id, message)
    finally:
        if pdf_path and os.path.exists(pdf_path):
            try:
                os.remove(pdf_path)
            except OSError:
                pass
        conn = get_redis()
        if conn is not None:
            try:
                conn.hset(_job_key(job_id), mapping={"tmp_path": ""})
                conn.expire(_job_key(job_id), int(_jobs_ttl_s))
            except Exception:
                pass


@app.route("/", methods=["GET"])
def index():
    return render_template("index.html")

@app.route("/privacy", methods=["GET"])
def privacy():
    return render_template("privacy.html")


@app.route("/favicon.ico", methods=["GET"])
def favicon():
    # Return an empty icon response to avoid noisy 404s in browser console.
    return Response(status=204)

@app.errorhandler(413)
def file_too_large(_err):
    return jsonify({"error": "File too large. Please upload a PDF under 10MB."}), 413


@app.route("/analyze", methods=["POST"])
def analyze():
    try:
        _cleanup_jobs()
        enforce_ip_rate_limit()
        job_description = request.form.get("job_description", "")
        resume_file = request.files.get("resume")
        api_key_override = (request.headers.get("X-Gemini-Api-Key") or request.form.get("gemini_api_key") or "").strip()

        if resume_file is None:
            raise ValueError("Please upload your resume PDF.")
        if not job_description or not job_description.strip():
            raise ValueError("Please paste a job description.")

        file_name = (resume_file.filename or "").lower()
        if not file_name.endswith(".pdf"):
            raise ValueError("Only PDF resumes are supported.")

        with tempfile.NamedTemporaryFile(delete=False, suffix=".pdf") as tmp:
            temp_path = tmp.name
            resume_file.save(temp_path)

        job_id = uuid.uuid4().hex
        create_job(job_id, temp_path)

        queue = get_queue()
        if queue is not None:
            queue.enqueue(process_job, job_id, temp_path, job_description, api_key_override, job_id=job_id)
        else:
            thread = threading.Thread(
                target=process_job,
                args=(job_id, temp_path, job_description, api_key_override),
                daemon=True,
            )
            thread.start()

        return jsonify({"job_id": job_id, "status": "processing"}), 202
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    except ServiceError as exc:
        return jsonify({"error": exc.message}), exc.status_code
    except Exception as exc:
        app.logger.exception("Analyze failed")
        message = "Analysis failed. Please try again."
        if app.debug:
            message = f"{message} ({type(exc).__name__}: {exc})"
        return jsonify({"error": message}), 500


@app.route("/result/<job_id>", methods=["GET"])
def result(job_id: str):
    _cleanup_jobs()
    job = get_job(job_id)
    if not job:
        return jsonify({"error": "Result not found (expired). Please re-run analysis."}), 404

    status = job.get("status", "processing")
    if status == "done":
        payload = {"status": "done", **(job.get("result") or {})}
        return jsonify(payload)
    if status == "error":
        return jsonify({"status": "error", "error": job.get("error", "Analysis failed.")}), 200

    return jsonify({"status": "processing"}), 200


if __name__ == "__main__":
    debug = (os.environ.get("FLASK_DEBUG") or "").strip() == "1"
    app.run(host="0.0.0.0", port=7860, debug=debug, use_reloader=debug, threaded=True)
