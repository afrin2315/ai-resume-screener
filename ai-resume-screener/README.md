# AI Resume Screener (Gemini + Evidence)

AI Resume Screener is a Flask web app that compares a **resume PDF** against a **job description** and returns a fit score, skill gaps, suggestions, and (when the LLM is available) **evidence-backed** insights.

It’s built to be safe to demo publicly:
- Works even when Gemini rate-limits (falls back to local analysis)
- Supports **BYO (Bring Your Own) Gemini API key** in the UI to avoid shared quota issues
- Redacts common PII before sending text to the LLM

## Features

- **Fit score (0–100)**: blended semantic similarity + keyword coverage
- **Matching skills** and **missing skills**
- **Actionable suggestions** (3–5)
- **Recruiter summary** + verdict (Strong/Good/Partial/Weak Fit)
- **Evidence** list (requirement → resume proof → confidence) when LLM is available
- **Async analysis**: job queue + result polling (more stable under load)

## Tech stack

- Backend: Flask
- LLM: Google Gemini (`google-generativeai`)
- Embeddings: `sentence-transformers` (`all-MiniLM-L6-v2`)
- PDF parsing: PyMuPDF (`fitz`)
- Optional: Redis + RQ for queueing + rate limiting
- Frontend: custom HTML/CSS/JS

## Run locally

```bash
cd ai-resume-screener

python -m venv .venv

# Windows
.\.venv\Scripts\Activate.ps1

# macOS/Linux
source .venv/bin/activate

pip install -r requirements.txt

# optional: server-side key (BYO key in UI also works)
cp .env.example .env

python app.py
```

Open `http://localhost:7860`.

## Configure Gemini

You have two options:

### Option A — Server key (simple)

Set `GEMINI_API_KEY` in:
- `ai-resume-screener/.env` (local dev), or
- your deployment secrets (HF Spaces “Secrets”)

### Option B — BYO key (recommended for public demos)

Users can paste their own Gemini API key in the UI.
- Saved only in their browser (localStorage)
- Sent per request via header `X-Gemini-Api-Key`

Get a key from Google AI Studio.

## Privacy

- The app redacts common PII (email/phone/URLs) before sending resume/JD text to the LLM.
- Uploaded PDFs are stored temporarily for processing and deleted after completion.
- See `/privacy` in the app.

## API (async)

### `POST /analyze`

- Content-Type: `multipart/form-data`
- Fields:
  - `job_description` (text)
  - `resume` (PDF)
- Optional header:
  - `X-Gemini-Api-Key: <user_key>`
- Response: `202` with:
  - `job_id`
  - `status`

### `GET /result/<job_id>`

- Returns:
  - `{"status":"processing"}` while running
  - `{"status":"done", ...results}` when finished
  - `{"status":"error","error":"..."}` on failure

## Optional: Redis + RQ (recommended for real traffic)

Using Redis makes jobs/rate limits reliable across restarts and higher traffic.

1) Set:
- `REDIS_URL` (Upstash Redis works well)

2) Run worker:
```bash
python queue_worker.py
```

## Project structure

```
ai-resume-screener/
  app.py
  queue_worker.py
  requirements.txt
  templates/
    index.html
    privacy.html
```

