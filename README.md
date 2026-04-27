---
title: AI Resume Screener
emoji: "📝"
colorFrom: indigo
colorTo: teal
sdk: docker
app_port: 7860
pinned: false
---

# AI Resume Screener

This Space runs a Flask app that scores a resume against a job description and returns:
- Match score
- Matching/missing skills
- Suggestions + recruiter summary
- Evidence (when LLM available)

## Setup (Secrets)

Add a Space secret:
- `GEMINI_API_KEY` = your Google Gemini API key

If the LLM rate-limits (429) or is temporarily unavailable, the app returns a local fallback analysis instead of failing.

## (Optional) Production queue

For higher traffic, set:
- `REDIS_URL` (e.g. Upstash Redis)

This enables a Redis-backed queue + rate limiting so multiple users can run analyses reliably.

## Run locally

See `ai-resume-screener/README.md`.
