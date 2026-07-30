# Pin to a stable 3.12 (dev box runs 3.14; image stays reproducible).
FROM python:3.12-slim

WORKDIR /app

# Deps first for layer caching: only re-installs when requirements.txt changes.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# .dockerignore keeps out .venv, caches, dbs, uploads, .env, etc.
COPY . .

# 시드 논문 PDF는 저장소에 없다 (재배포 불가 1편 · 비상업 1편 — papers/SOURCES.md).
# 그래서 빌드가 arXiv에서 직접 받는다. 예전엔 이 자리 주석이 "papers/ PDFs ship in
# the image"였는데, PDF를 저장소에서 뺀 뒤로는 사실이 아니었다 — git에서 빌드하면
# 소스 0편으로 떴다.
#
# 이어서 쪽 텍스트를 미리 뽑아 둔다 (343쪽, 약 3초). 안 뽑아 두면 첫 질문이 PDF 9편을
# 파싱하는 동안 멈춰 있고, 무료 티어의 콜드 스타트와 겹쳐 "느린 앱"으로 보인다.
# app.db는 지우고 나간다 — 레지스트리는 런타임에 새로 시드된다.
RUN python fetch_papers.py \
 && python -c "import json,pathlib,corpus; [corpus.extract_pages(e['path']) for e in json.loads(pathlib.Path('papers/papers.json').read_text())]" \
 && rm -f app.db mastery.db

# Non-root. chown /app so a mounted ./data (dbs, caches, uploads) is writable
# by this uid when the volume is bind-mounted in.
RUN useradd --create-home --uid 10001 appuser && chown -R appuser:appuser /app
USER appuser

EXPOSE 8000

# python one-liner instead of curl -> no extra apt package.
# 포트는 $PORT를 따른다 (아래 CMD와 같은 이유).
HEALTHCHECK --interval=30s --timeout=3s --start-period=15s --retries=3 \
  CMD python -c "import os,urllib.request,sys; p=os.getenv('PORT','8000'); sys.exit(0 if urllib.request.urlopen(f'http://localhost:{p}/healthz').status==200 else 1)"

# Pass the key at runtime, never bake it in:
#   docker run -e ANTHROPIC_API_KEY=sk-... -p 8000:8000 <image>
# or set it in .env (see docker-compose.yml env_file). MOCK_LLM=1 skips the key.
# PaaS(Render·Railway 등)는 포트를 $PORT로 주입한다. 8000으로 고정하면 플랫폼이
# 헬스체크를 붙이지 못해 배포가 실패한다. 로컬·compose에는 PORT가 없어 8000이 된다.
CMD ["sh", "-c", "uvicorn app:app --host 0.0.0.0 --port ${PORT:-8000}"]
