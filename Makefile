PY ?= .venv/bin/python

.PHONY: install test lint demo replay evaluate serve scan mysql-up mysql-down test-mysql media check \
        check-containers check-mysql check-postgres check-aws check-gcp check-azure containers-down

install:
	python3 -m venv .venv
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

# Everything GitHub runs on a change: lint, tests, the demo and the name scan. Free, and faster than waiting for CI.
check: lint test
	$(PY) -m capacitylab run campaign-overlap --provider mock --out runs/check-campaign.json --quiet
	$(PY) -m capacitylab replay runs/check-campaign.json
	$(PY) -m capacitylab scan

# The container suites, the same ones .github/workflows/containers.yml runs on request. They need Docker.
check-containers: check-mysql check-postgres check-aws check-gcp check-azure

check-mysql:
	docker compose up -d --wait sandbox-mysql
	CAPACITYLAB_TEST_MYSQL=1 $(PY) -m pytest -m mysql

check-postgres:
	docker compose up -d --wait sandbox-postgres
	CAPACITYLAB_TEST_POSTGRES=1 $(PY) -m pytest -m postgres

check-aws:
	docker compose --profile aws up -d floci
	$(PY) scripts/floci_seed.py
	CAPACITYLAB_TEST_FLOCI=1 $(PY) -m pytest -m aws

check-gcp:
	docker compose --profile gcp up -d floci-gcp
	$(PY) scripts/cloud_emulator_seed.py gcp
	CAPACITYLAB_TEST_FLOCI_GCP=1 $(PY) -m pytest -m gcp

check-azure:
	docker compose --profile azure up -d floci-az
	$(PY) scripts/cloud_emulator_seed.py azure
	CAPACITYLAB_TEST_FLOCI_AZ=1 $(PY) -m pytest -m azure

containers-down:
	docker compose --profile aws --profile gcp --profile azure down

mysql-up:
	docker compose up -d --wait sandbox-mysql

mysql-down:
	docker compose down

test-mysql:
	CAPACITYLAB_TEST_MYSQL=1 $(PY) -m pytest -m mysql

media:
	$(PY) scripts/capture_media.py
