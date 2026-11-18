# Image for the hosted demo (Render free tier).
# Free instances have ~0.1 CPU, so speech-to-text uses Groq's hosted Whisper
# (STT_BACKEND=groq). Piper text-to-speech still runs inside the container.
FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    STT_BACKEND=groq \
    LLM_MODEL=openai/gpt-oss-120b

WORKDIR /app
COPY pyproject.toml README.md ./
COPY src ./src
COPY data ./data
RUN pip install -e . && python -m bistrovoice download-models --agent-voice-only

EXPOSE 8000
CMD ["sh", "-c", "uvicorn bistrovoice.server:app --host 0.0.0.0 --port ${PORT:-8000}"]
