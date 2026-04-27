FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PORT=7860

WORKDIR /app

# Install dependencies
COPY ai-resume-screener/requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

# Pre-download embedding model to avoid runtime network issues on first request.
RUN python -c "from sentence_transformers import SentenceTransformer; SentenceTransformer('all-MiniLM-L6-v2')"

# Copy app source
COPY ai-resume-screener /app/ai-resume-screener

WORKDIR /app/ai-resume-screener

EXPOSE 7860

CMD ["sh", "-c", "python queue_worker.py & exec gunicorn -b 0.0.0.0:7860 --workers 1 --threads 4 --timeout 180 app:app"]
