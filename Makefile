PYTHON ?= uv run
PORT ?= 8787
AUTOPENTEST_API ?= http://127.0.0.1:$(PORT)
NEXT_PUBLIC_SSE_BASE ?= http://127.0.0.1:$(PORT)

.PHONY: sync migrate run test lint health docker-up docker-down ov-init ov-up ov-down ov-health web-install web-dev web-build web-export

sync:
	uv sync --all-extras

migrate:
	$(PYTHON) penr migrate

run:
	$(PYTHON) penr serve --host 0.0.0.0 --port $(PORT)

test:
	$(PYTHON) pytest -q

lint:
	$(PYTHON) ruff check src tests

health:
	curl -sf http://127.0.0.1:$(PORT)/health | python -m json.tool

docker-up:
	docker compose up -d

docker-down:
	docker compose down

# ---- 本仓内置 OpenViking（长期知识库）----
ov-init:
	mkdir -p data/openviking
	@if [ ! -f data/openviking/ov.conf ]; then \
		cp deploy/openviking/ov.conf.example data/openviking/ov.conf; \
		echo "wrote data/openviking/ov.conf — 请填入 embedding/VLM 与 root_api_key"; \
	else echo "data/openviking/ov.conf exists"; fi
	@if [ ! -f data/openviking/ovcli.conf ]; then \
		cp deploy/openviking/ovcli.conf.example data/openviking/ovcli.conf; \
		echo "wrote data/openviking/ovcli.conf — 指向本仓 http://127.0.0.1:1933"; \
	else echo "data/openviking/ovcli.conf exists"; fi

ov-up: ov-init
	docker compose up -d openviking

ov-down:
	docker compose stop openviking

ov-health:
	curl -sf http://127.0.0.1:1933/health | python -m json.tool

# ---- Next.js 前端（web/）----
web-install:
	cd web && npm ci

web-dev:
	cd web && AUTOPENTEST_API=$(AUTOPENTEST_API) NEXT_PUBLIC_SSE_BASE=$(NEXT_PUBLIC_SSE_BASE) npm run dev

web-build:
	cd web && npm run build

web-export:
	cd web && npm run build:static
	mkdir -p data/webui
	find data/webui -mindepth 1 -maxdepth 1 -exec rm -rf {} +
	cp -R web/out/. data/webui/
	@echo "exported web/out → data/webui (FastAPI StaticFiles 挂载)"
