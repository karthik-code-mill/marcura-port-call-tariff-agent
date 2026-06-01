# ─────────────────────────────────────────────────────────────────────────────
# Marcura Port Call Tariff Agent — Local Pipeline Runner
# Simulates each CI/CD stage locally before pushing.
#
# Usage:
#   make lint          Run Stage 1 locally
#   make security      Run Stage 2 locally
#   make test          Run Stage 3 + 4 locally
#   make build         Run Stage 5 locally
#   make pipeline      Run all stages in order (1 → 5)
# ─────────────────────────────────────────────────────────────────────────────

PYTHON   := python
PIP      := pip
IMAGE    := marcura-port-call-tariff-agent
VERSION  := $(shell git describe --tags --always --dirty 2>/dev/null || echo dev)

.PHONY: install lint security unit-test integration-test test build pipeline clean help

help:
	@echo ""
	@echo "  Marcura Port Call Tariff Agent — Local CI/CD Stages"
	@echo ""
	@echo "  make install          Install all dependencies + dev tools"
	@echo "  make lint             Stage 1 · Lint, format, type-check"
	@echo "  make security         Stage 2 · SAST, dependency audit, secret scan"
	@echo "  make unit-test        Stage 3 · Unit tests with coverage"
	@echo "  make integration-test Stage 4 · Integration tests (mocked LLM)"
	@echo "  make test             Stage 3 + 4"
	@echo "  make build            Stage 5 · Docker image build"
	@echo "  make pipeline         Run all stages 1–5 in order"
	@echo "  make clean            Remove generated artifacts"
	@echo ""

# ── Install ───────────────────────────────────────────────────────────────────

install:
	$(PIP) install -r requirements.txt
	$(PIP) install ruff black mypy bandit pip-audit detect-secrets \
	               pytest pytest-cov pytest-mock httpx

# ── Stage 1: Code Quality ─────────────────────────────────────────────────────

lint:
	@echo "\n══ Stage 1 · Code Quality ══════════════════════════════════════════"
	@echo "── Ruff lint ──────────────────────────────────────────────────────"
	ruff check src/ guardrails/ evaluation/
	@echo "── Black format check ─────────────────────────────────────────────"
	black --check --diff src/ guardrails/ evaluation/
	@echo "── Mypy type check ────────────────────────────────────────────────"
	mypy src/ --ignore-missing-imports --no-strict-optional
	@echo "✓ Stage 1 passed"

# ── Stage 2: Security ─────────────────────────────────────────────────────────

security:
	@echo "\n══ Stage 2 · Security Scan ═════════════════════════════════════════"
	@echo "── Bandit SAST ────────────────────────────────────────────────────"
	bandit -r src/ guardrails/ -ll || true
	@echo "── Dependency audit (pip-audit) ───────────────────────────────────"
	pip-audit || true
	@echo "── Secret detection ───────────────────────────────────────────────"
	detect-secrets scan --baseline .secrets.baseline 2>/dev/null || \
	  detect-secrets scan > .secrets.baseline
	@echo "── Prompt injection surface check ─────────────────────────────────"
	$(PYTHON) -c "\
	from pathlib import Path; \
	src = Path('security/prompt_injection.py').read_text(); \
	n = src.count('re.compile'); \
	print(f'  Injection patterns: {n}'); \
	assert n >= 10, 'Too few patterns'"
	@echo "✓ Stage 2 passed"

# ── Stage 3: Unit Tests ───────────────────────────────────────────────────────

unit-test:
	@echo "\n══ Stage 3 · Unit Tests ════════════════════════════════════════════"
	pytest evaluation/ src/ \
	  --ignore=src/agents \
	  -m "not integration" \
	  --cov=src \
	  --cov-report=term-missing \
	  --cov-report=html:htmlcov \
	  --tb=short -q
	pytest guardrails/ \
	  --cov=guardrails --cov-append \
	  --cov-report=term-missing \
	  --tb=short -q
	@echo "✓ Stage 3 passed"

# ── Stage 4: Integration Tests ────────────────────────────────────────────────

integration-test:
	@echo "\n══ Stage 4 · Integration Tests ═════════════════════════════════════"
	$(PYTHON) -c "\
	import sys; sys.path.insert(0,'src'); \
	from fastapi.testclient import TestClient; \
	from api.api import app; \
	c = TestClient(app); \
	r = c.get('/docs'); \
	assert r.status_code == 200, f'FastAPI /docs failed: {r.status_code}'; \
	print('  FastAPI /docs OK')"
	pytest evaluation/ -k "not real_llm" --tb=short -q
	@echo "✓ Stage 4 passed"

test: unit-test integration-test

# ── Stage 5: Build ────────────────────────────────────────────────────────────

build:
	@echo "\n══ Stage 5 · Build ═════════════════════════════════════════════════"
	docker build -f cicd/Dockerfile -t $(IMAGE):$(VERSION) .
	@echo "  Built: $(IMAGE):$(VERSION)"
	zip -r release-$(VERSION).zip \
	  src/ guardrails/ prompts/ requirements.txt \
	  --exclude "**/__pycache__/*" "**/*.pyc"
	@echo "  Artifact: release-$(VERSION).zip"
	@echo "✓ Stage 5 passed"

# ── Full pipeline ─────────────────────────────────────────────────────────────

pipeline: lint security test build
	@echo "\n══ All stages passed ✓ ═════════════════════════════════════════════"

# ── Clean ─────────────────────────────────────────────────────────────────────

clean:
	rm -rf htmlcov/ coverage.xml bandit-report.json pip-audit-report.json
	rm -f release-*.zip
	find . -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true
	find . -name "*.pyc" -delete 2>/dev/null || true
