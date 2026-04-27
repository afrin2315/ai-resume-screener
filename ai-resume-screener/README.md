# Resume Screener AI

A Flask-powered AI resume analyzer with a custom HTML/CSS/JS frontend. It evaluates how well a resume matches a job description and returns a semantic fit score, skill gaps, and practical resume improvements.

---

## Features

- Semantic match score (0-100) using Sentence Transformers (MiniLM)
- Matching skills extracted from resume vs job description
- Missing skills to highlight fit gaps
- Actionable suggestions for resume improvement
- Recruiter-style summary and fit verdict
- Custom dark glassmorphism UI with animated interactions

---

## Tech Stack

| Layer | Tool |
|---|---|
| Backend | Flask |
| LLM | Google Gemini 1.5 Flash API |
| Embeddings | Sentence Transformers (all-MiniLM-L6-v2) |
| PDF Parsing | PyMuPDF (fitz) |
| Frontend | Custom HTML, CSS, JavaScript |

---

## Run Locally

```bash
# 0) Enter the app folder (recommended)
cd ai-resume-screener

# 1) Install dependencies
pip install -r requirements.txt

# 2) Create a .env file next to app.py with:
# GEMINI_API_KEY=your_key_here

# 3) Start the Flask app
python app.py
```

Then open http://localhost:7860 in your browser.

Get a free Gemini API key at [aistudio.google.com](https://aistudio.google.com).

The app reads `GEMINI_API_KEY` from environment variables (or a local `.env` file during development).

Note: Analysis runs asynchronously (the UI shows a loader while the server processes).

BYO key: Users can optionally paste their own Gemini API key in the UI (stored only in their browser) to avoid shared-quota rate limits.

Privacy: The app redacts common PII (email/phone/URLs) before sending text to the LLM. See `/privacy`.

Optional Redis queue (recommended for public traffic):
- Set `REDIS_URL` (e.g. Upstash) and run the worker with `python queue_worker.py`.

---

## Project Structure

```
ai-resume-screener/
├── app.py                # Flask backend + analysis logic
├── templates/
│   └── index.html        # Custom frontend (HTML/CSS/JS)
├── requirements.txt
└── README.md
```

---

## API Endpoint

- POST /analyze
	- Content-Type: multipart/form-data
	- Fields: job_description, resume (PDF)
	- Returns JSON:
		- score
		- verdict
		- matching_skills[]
		- missing_skills[]
		- suggestions[]
		- recruiter_summary

---

## Setting up on Hugging Face Spaces

1. Create a new Space (Gradio or Docker SDK).
2. Go to Settings -> Variables and Secrets.
3. Add a new Secret:
   - Name: `GEMINI_API_KEY`
   - Value: your key
4. Push the code. The app automatically picks up the key from environment variables.
