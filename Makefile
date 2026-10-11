# 项目常用命令集中在这里。
# 为什么要有 Makefile：命令写进文档会腐烂（改了代码忘了改文档），
# 写进 Makefile 则是"可执行的文档"—— CI 和人都用同一套入口。

.PHONY: help install install-dev test lint fmt cov run docker-up docker-down eval clean

help:
	@echo "install      安装生产依赖"
	@echo "install-dev  安装开发依赖（pytest / ruff）"
	@echo "test         跑单元测试（不联网，0 API 成本）"
	@echo "lint         静态检查（只拦真错误）"
	@echo "fmt          自动格式化（ruff format + --fix）"
	@echo "cov          测试 + 覆盖率"
	@echo "run          本地起后端（8000）"
	@echo "dev-web      本地起前端（5173）"
	@echo "docker-up    用 docker compose 起前后端"
	@echo "eval         跑对照实验（会花真实 token，默认小样本）"

install:
	cd backend && .venv/Scripts/python -m pip install -r requirements.txt

install-dev:
	cd backend && .venv/Scripts/python -m pip install -r requirements.txt -r requirements-dev.txt

test:
	cd backend && .venv/Scripts/python -m pytest -q

lint:
	cd backend && .venv/Scripts/python -m ruff check .

fmt:
	cd backend && .venv/Scripts/python -m ruff check --fix . && .venv/Scripts/python -m ruff format .

cov:
	cd backend && .venv/Scripts/python -m pytest -q --cov=app --cov-report=term-missing

run:
	cd backend && .venv/Scripts/python -m uvicorn app.api.main:app --host 127.0.0.1 --port 8000

dev-web:
	cd frontend && npm run dev -- --host 127.0.0.1 --port 5173

docker-up:
	docker compose up --build

docker-down:
	docker compose down

# 实验会花真实 token。--reps 与 --timing-runs 是小样本默认值，别在 CI 里跑。
eval:
	cd backend && .venv/Scripts/python -u scripts/experiment_v05.py --reps 2 --timing-runs 3

clean:
	cd backend && rm -rf .pytest_cache .ruff_cache htmlcov .coverage
