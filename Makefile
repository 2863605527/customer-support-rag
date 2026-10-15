# ===== 环境 =====
PYTHON ?= python
PIP ?= pip

# ===== 安装 =====
.PHONY: install install-dev
install:
	$(PIP) install -e .
install-dev:
	$(PIP) install -e ".[dev]"

# ===== 数据准备（真实货源数据 → 知识库文档）=====
.PHONY: harvest kb-docs
harvest:
	$(PYTHON) scripts/harvest_zhaojiafang.py
kb-docs:
	$(PYTHON) scripts/build_kb_docs.py --clean

# ===== 知识库入库 =====
.PHONY: ingest
ingest:
	customer-support ingest

# ===== 检索阈值标定（用标注集量出 RETRIEVAL_SCORE_THRESHOLD）=====
.PHONY: calibrate
calibrate:
	$(PYTHON) scripts/calibrate_threshold.py --verbose

# ===== 质量检查 =====
.PHONY: lint format type
lint:
	ruff check src tests scripts
format:
	ruff format src tests scripts
	ruff check --fix src tests scripts
type:
	mypy src

# ===== 测试 =====
.PHONY: test test-int test-all
test:
	pytest tests/unit -m "not integration"
test-int:
	pytest tests/integration -m integration
test-all:
	pytest

# ===== 运行 =====
.PHONY: ask serve tickets
ask:
	customer-support ask "商品はいつ届きますか？"
serve:
	customer-support serve --port 8002
tickets:
	customer-support tickets

# ===== 本地 Milvus（仅备选后端用）=====
.PHONY: docker-up docker-down
docker-up:
	docker compose up -d milvus
docker-down:
	docker compose down

# ===== 检查清单 =====
.PHONY: check
check: lint type test
	@echo "✓ 全部检查通过"
