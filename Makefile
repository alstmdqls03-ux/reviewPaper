# Prefer a local .venv interpreter, else system python.
PY := $(shell [ -x .venv/bin/python ] && echo .venv/bin/python || echo python)

.PHONY: help run mock test ci eval load selfcheck docker-build docker-up docker-down fmt clean

help:  ## list targets
	@echo "targets:"
	@echo "  run          real server (needs ANTHROPIC_API_KEY)"
	@echo "  mock         MOCK_LLM=1 offline server"
	@echo "  test         pytest -q"
	@echo "  ci           ./ci.sh (unit + MOCK gold-set gate + injection)"
	@echo "  eval         MOCK gold-set eval"
	@echo "  load         locust headless 20 users / 20s"
	@echo "  selfcheck    run each module's __main__ self-check"
	@echo "  docker-build build the image"
	@echo "  docker-up    docker compose up (build + detached)"
	@echo "  docker-down  docker compose down"
	@echo "  fmt          python -m compileall . (syntax gate; no formatter dep)"
	@echo "  clean        remove db/cache/upload runtime artifacts"

run:
	$(PY) -m uvicorn app:app --host 0.0.0.0 --port 8000

mock:
	MOCK_LLM=1 $(PY) -m uvicorn app:app --host 0.0.0.0 --port 8000

test:
	$(PY) -m pytest -q

ci:
	./ci.sh

eval:
	MOCK_LLM=1 $(PY) eval_gold.py

load:
	$(PY) -m locust -f locustfile.py --headless -u 20 -r 5 -t 20s --host http://127.0.0.1:8000

# `-` prefix tolerates modules that don't exist yet (won't abort the sweep).
selfcheck:
	-$(PY) session.py
	-$(PY) mastery.py
	-$(PY) corpus.py
	-$(PY) obs.py
	-$(PY) accounts.py
	-$(PY) history.py
	-$(PY) config.py
	-$(PY) limits.py
	-$(PY) analytics.py

docker-build:
	docker build -t reviewpaper .

docker-up:
	docker compose up --build -d

docker-down:
	docker compose down

fmt:
	$(PY) -m compileall .

clean:
	rm -f mastery.db app.db corpus.json file_cache.json
	rm -rf text_cache uploads __pycache__
