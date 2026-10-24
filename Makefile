.PHONY: dev test cov lint type fmt check docker clean

# Dev server (mock mode) - Windows PowerShell equivalent:
#   python run.py
dev:
	python run.py

test:
	python -m pytest

cov:
	python -m pytest --cov=app --cov-report=term-missing

lint:
	python -m ruff check app tests

type:
	python -m mypy app

fmt:
	python -m ruff check --fix app tests

check: lint type test

docker:
	docker build -t shopify-geo-agent .
	docker run --rm -p 8000:8000 shopify-geo-agent

clean:
	python -m ruff clean .
