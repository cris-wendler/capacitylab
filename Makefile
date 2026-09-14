PY ?= .venv/bin/python

.PHONY: install test lint demo replay evaluate serve scan mysql-up mysql-down test-mysql media

install:
	python3.11 -m venv .venv
	.venv/bin/pip install -e ".[dev,anthropic,mysql]"

test:
	$(PY) -m pytest

lint:
	.venv/bin/ruff check src tests scripts

demo:
	$(PY) -m capacitylab run campaign-overlap --provider mock --out runs/demo.json

replay:
	$(PY) -m capacitylab replay runs/demo.json

evaluate:
	$(PY) -m capacitylab evaluate campaign-overlap --provider mock

serve:
	$(PY) -m capacitylab serve --port 8765

scan:
	$(PY) -m capacitylab scan

mysql-up:
	docker compose up -d --wait sandbox-mysql

mysql-down:
	docker compose down

test-mysql:
	CAPACITYLAB_TEST_MYSQL=1 $(PY) -m pytest -m mysql

media:
	$(PY) scripts/capture_media.py
