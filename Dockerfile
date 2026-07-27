# Pin to a stable 3.12 (dev box runs 3.14; image stays reproducible).
FROM python:3.12-slim

WORKDIR /app

# Deps first for layer caching: only re-installs when requirements.txt changes.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# App + corpus. papers/ PDFs are the corpus, so they ship in the image.
# .dockerignore keeps out .venv, caches, dbs, uploads, .env, etc.
COPY . .

# Non-root. chown /app so a mounted ./data (dbs, caches, uploads) is writable
# by this uid when the volume is bind-mounted in.
RUN useradd --create-home --uid 10001 appuser && chown -R appuser:appuser /app
USER appuser

EXPOSE 8000

# python one-liner instead of curl -> no extra apt package.
HEALTHCHECK --interval=30s --timeout=3s --start-period=10s --retries=3 \
  CMD python -c "import urllib.request,sys; sys.exit(0) if urllib.request.urlopen('http://localhost:8000/healthz').status==200 else sys.exit(1)"

# Pass the key at runtime, never bake it in:
#   docker run -e ANTHROPIC_API_KEY=sk-... -p 8000:8000 <image>
# or set it in .env (see docker-compose.yml env_file). MOCK_LLM=1 skips the key.
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8000"]
