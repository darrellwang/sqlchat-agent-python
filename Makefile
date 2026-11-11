# mysqlagent (Python) 常用命令
#
# 运行环境任选其一：
#   - 本机已装 Python 3.11+：直接 make（pip 装到当前环境）
#   - WSL / Linux：在 WSL 内运行

PYTHON  ?= python
IMAGE   := mysqlagent:latest

.PHONY: install run test fmt lint docker-build docker-run scan sbom clean

# 安装依赖（运行时 + 开发）
install:
	$(PYTHON) -m pip install -r requirements.txt
	$(PYTHON) -m pip install -e ".[dev]"

# 本地运行（读 config.yaml；不存在时回退环境变量 + 默认值）
run:
	$(PYTHON) main.py -config config.yaml

# 全量测试
test:
	$(PYTHON) -m pytest tests/ -q

# 格式化与静态检查（ruff 未安装时给出提示，不阻断）
fmt:
	@$(PYTHON) -m ruff format mysqlagent tests main.py 2>/dev/null \
	  || echo "提示：pip install ruff 后可自动格式化"

lint:
	@$(PYTHON) -m ruff check mysqlagent tests main.py 2>/dev/null \
	  || echo "提示：pip install ruff 后可静态检查"

# 构建容器镜像
docker-build:
	docker build -t $(IMAGE) .

# 容器化运行（host 网络，适配 WSL2 mirrored 拓扑）
docker-run:
	docker rm -f mysqlagent 2>/dev/null || true
	docker run -d --name mysqlagent --network host --restart unless-stopped \
	  -e AUDIT_OUTPUT=/var/log/mysqlagent/audit.jsonl \
	  -v mysqlagent-audit:/var/log/mysqlagent \
	  $(IMAGE)

# 安全扫描：镜像漏洞（HIGH/CRITICAL 失败）+ CycloneDX SBOM + 源码/密钥扫描
scan:
	bash scripts/security-scan.sh $(IMAGE)

# 仅生成 SBOM
sbom:
	bash scripts/security-scan.sh $(IMAGE) || true
	@echo "SBOM: scan/sbom.cdx.json"

clean:
	rm -rf .pytest_cache build dist *.egg-info
	find . -name __pycache__ -type d -prune -exec rm -rf {} + 2>/dev/null || true
	docker rmi $(IMAGE) 2>/dev/null || true
