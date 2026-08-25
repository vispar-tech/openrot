.PHONY: help install fix lint types test check clean pre-commit docker-build docker-run docker-sh installer install-docker binary package

POETRY ?= poetry
DOCKER ?= docker
PORT ?= 7890
BRIDGE_PORT ?= 7891
MODE ?= cascade
CONFIG_DIR ?= $(HOME)/.config/openrot
# WARP runs on the host, not in the container. Default bridge network reaches it
# via OPENROT_WARP_HOST (host.docker.internal works on Docker Desktop and on
# Linux with --add-host). On Linux you can instead set NETWORK=host to share the
# host loopback (WARP then answers at 127.0.0.1:40000).
NETWORK ?= bridge
WARP_HOST ?= host.docker.internal

help: ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-12s\033[0m %s\n", $$1, $$2}'

install: ## Install dependencies (with dev group)
	$(POETRY) install

fix: ## Format and auto-fix code with ruff (mutates files)
	$(POETRY) run ruff format .
	$(POETRY) run ruff check . --fix

lint: ## Lint check only with ruff (no changes)
	$(POETRY) run ruff check .
	$(POETRY) run ruff format --check .

types: ## Type-check with mypy
	$(POETRY) run mypy openrot/

test: ## Run tests
	$(POETRY) run pytest

check: lint types test ## Verify: lint + types + tests (no changes)

clean: ## Remove build artifacts
	rm -rf dist build *.egg-info
	find . -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true

pre-commit: ## Run pre-commit hooks on all files
	$(POETRY) run pre-commit run -a

installer: ## Install the standalone openrot binary + prerequisites
	./install.sh

install-docker: ## Install/run openrot in Docker (host-side WARP)
	./install-docker.sh

binary: ## Build a standalone onedir openrot into dist/openrot/ (PyInstaller openrot.spec)
	$(POETRY) run pyinstaller --noconfirm --clean openrot.spec

package: binary ## Package dist/openrot/ as dist/openrot-<os>-<arch>.tar.gz (launcher + _internal/ at root)
	@os=`uname -s | tr '[:upper:]' '[:lower:]'`; \
	arch=`uname -m`; \
	case "$$arch" in x86_64|amd64) arch=x86_64;; arm64|aarch64) arch=aarch64;; esac; \
	echo "packaging dist/openrot -> dist/openrot-$$os-$$arch.tar.gz"; \
	cd dist/openrot && tar -czf ../openrot-$$os-$$arch.tar.gz .

docker-build: ## Build the openrot Docker image
	$(DOCKER) build -t openrot .

docker-run: docker-build ## Run openrot in Docker, wired to the host-side WARP
	@if [ "$(NETWORK)" = "host" ]; then \
		$(DOCKER) run --rm -it --network host \
			-v $(CONFIG_DIR):/root/.config/openrot \
			openrot $(MODE); \
	else \
		$(DOCKER) run --rm -it --network bridge \
			-p $(PORT):7890 \
			-p $(BRIDGE_PORT):7891 \
			--add-host host.docker.internal:host-gateway \
			-e OPENROT_WARP_HOST=$(WARP_HOST) \
			-v $(CONFIG_DIR):/root/.config/openrot \
			openrot $(MODE); \
	fi

docker-sh: docker-build ## Open a shell inside the running image
	@if [ "$(NETWORK)" = "host" ]; then \
		$(DOCKER) run --rm -it --network host \
			-v $(CONFIG_DIR):/root/.config/openrot \
			--entrypoint /bin/sh openrot; \
	else \
		$(DOCKER) run --rm -it --network bridge \
			-p $(PORT):7890 \
			--add-host host.docker.internal:host-gateway \
			-e OPENROT_WARP_HOST=$(WARP_HOST) \
			-v $(CONFIG_DIR):/root/.config/openrot \
			--entrypoint /bin/sh openrot; \
	fi