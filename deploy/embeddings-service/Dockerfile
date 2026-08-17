# SR-24 companion embedding container -- multi-stage, non-root, healthcheck
# (CLAUDE.md packaging standard). Self-contained, single-file FastAPI app
# wrapping sentence-transformers/all-MiniLM-L6-v2 -- see app.py's docstring
# for why this exists (no hosted API serves this model).
#
# torch is installed from the CPU-only wheel index explicitly, BEFORE
# requirements.txt -- plain `pip install torch` defaults to the CUDA build
# (multiple GB) which is both unnecessary (this Droplet has no GPU) and a
# much slower build/pull. Installing the CPU wheel first satisfies
# sentence-transformers' torch dependency without pip reinstalling it.
#
# Build context is this directory (unlike services/api/Dockerfile, this
# service has no sibling-package dependency):
#
#   docker build -f deploy/embeddings-service/Dockerfile -t chatbot-embeddings:<tag> deploy/embeddings-service

FROM python:3.12-slim AS builder
WORKDIR /build

COPY requirements.txt .
RUN python -m venv /venv
ENV PATH="/venv/bin:${PATH}"
RUN pip install --no-cache-dir --upgrade pip \
    && pip install --no-cache-dir torch --index-url https://download.pytorch.org/whl/cpu \
    && pip install --no-cache-dir -r requirements.txt

# Bake the model weights into the image at build time -- no runtime download,
# no network dependency once the container is running.
RUN python -c "from sentence_transformers import SentenceTransformer; \
    SentenceTransformer('sentence-transformers/all-MiniLM-L6-v2')"

FROM python:3.12-slim AS runtime

RUN groupadd --system app && useradd --system --gid app --create-home --home-dir /home/app app

WORKDIR /app
COPY --from=builder /venv /venv
COPY --from=builder /root/.cache/huggingface /home/app/.cache/huggingface
COPY app.py .

ENV PATH="/venv/bin:${PATH}" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

RUN chown -R app:app /app /home/app/.cache
USER app

EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
    CMD python -c "import sys,urllib.request; sys.exit(0 if urllib.request.urlopen('http://localhost:8080/healthz', timeout=3).status == 200 else 1)"

CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8080"]
