# Image for the hosted demo (Render free tier: ~0.1 CPU, 512 MB).
# Speech-to-text uses Groq's hosted Whisper (STT_BACKEND=groq) and the visitor's
# browser speaks the replies with an English voice (TTS_BACKEND=browser): Piper
# took 2-6 s per sentence on this CPU. Set TTS_BACKEND=piper in Render to use
# Piper anyway; the TTS_* settings below keep it inside the memory limit.
FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    STT_BACKEND=groq \
    TTS_BACKEND=browser \
    TTS_THREADS=1 \
    TTS_MAX_CHARS=40 \
    MALLOC_ARENA_MAX=2 \
    LLM_MODEL=openai/gpt-oss-120b

WORKDIR /app
COPY pyproject.toml README.md ./
COPY src ./src
COPY data ./data
RUN pip install -e . && python -m bistrovoice download-models --agent-voice-only

EXPOSE 8000
CMD ["sh", "-c", "uvicorn bistrovoice.server:app --host 0.0.0.0 --port ${PORT:-8000}"]
