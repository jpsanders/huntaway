SRC := src
TESTS := tests
SCRIPTS := scripts
ROOT_EC_FILES := $(wildcard *.yaml *.yml *.md *.sh) .github docs web-src
PACKAGE := huntaway
TAILWIND_VERSION := 4.3.3
TAILWIND := build/tailwindcss-$(TAILWIND_VERSION)

.PHONY: help
help:
	@echo "Available targets:"
	@grep -E "^([a-zA-Z_-]+):.*" $(MAKEFILE_LIST) | sort | awk 'BEGIN {FS = ":"}; {printf "    \033[36m%s\n\033[0m", $$1}'
	@echo ""
	@echo "Hints:"
	@echo "    Use '-n' to do a dry run"
	@echo "    Use '-k' to ignore failed targets and keep going"

.PHONY: init
init:
	@echo
	pip install -e .

.PHONY: init-dev
init-dev:
	@echo
	pip install -e .[dev]

.PHONY: format
format: black isort

.PHONY: isort-check
isort-check:
	@echo
	@echo --- Isort check ---
	isort --check --diff ${SRC} ${TESTS} ${SCRIPTS}

.PHONY: isort
isort:
	@echo
	@echo --- Isort auto format ---
	isort ${SRC} ${TESTS} ${SCRIPTS}

.PHONY: black-check
black-check:
	@echo
	@echo --- Black check ---
	black --check --diff ${SRC} ${TESTS} ${SCRIPTS}

.PHONY: black
black:
	@echo
	@echo --- Black auto format ---
	black ${SRC} ${TESTS} ${SCRIPTS}

.PHONY: ec
ec:
	@echo
	@echo --- Editorconfig linting checks ---
	ec -exclude 'app\.css$$' ${SRC} ${TESTS} ${SCRIPTS} ${ROOT_EC_FILES}

.PHONY: docstyle
docstyle:
	@echo
	@echo --- Darglint docstring checks ---
	darglint -v 2 ${SRC} ${TESTS} ${SCRIPTS}

.PHONY: lint
lint:
	@echo
	@echo --- Flake8 linting checks ---
	flake8 ${SRC} ${TESTS} ${SCRIPTS}

.PHONY: mypy
mypy:
	@echo
	@echo --- Mypy type checks ---
	mypy ${SRC} ${TESTS} ${SCRIPTS}

.PHONY: pytest
pytest:
	@echo
	@echo --- Pytest regression and unit tests ---
	pytest --cov ${SRC}/${PACKAGE} ${TESTS}

.PHONY: bandit
bandit:
	@echo
	@echo --- Bandit security checks ---
	bandit -c pyproject.toml -f custom -r ${SRC} ${TESTS} ${SCRIPTS}

.PHONY: man-check
man-check:
	@echo
	@echo --- Man page and manual up to date ---
	python scripts/build_man.py --check

# Build the man page and the packaged manual from docs/MANUAL.md and the settings schema
.PHONY: man
man:
	@echo
	@echo --- Build the man page ---
	python scripts/build_man.py

# The dashboard's stylesheet, built with the standalone Tailwind CLI, so no Node is needed
$(TAILWIND):
	@echo
	@echo --- Download Tailwind CSS $(TAILWIND_VERSION) ---
	mkdir -p build
	curl -sSL -o $(TAILWIND) https://github.com/tailwindlabs/tailwindcss/releases/download/v$(TAILWIND_VERSION)/tailwindcss-$$(uname -s | tr A-Z a-z | sed s/darwin/macos/)-$$(uname -m | sed -e s/x86_64/x64/ -e s/aarch64/arm64/)
	chmod +x $(TAILWIND)

.PHONY: css
css: $(TAILWIND)
	@echo
	@echo --- Build the dashboard stylesheet ---
	$(TAILWIND) -i web-src/input.css -o ${SRC}/${PACKAGE}/web/app.css --minify

# The dashboard on made up data
.PHONY: demo
demo:
	python -m huntaway demo

.PHONY: build
build: clean
	@echo
	@echo --- Build package wheel ---
	python -m build -o build/dist

.PHONY: clean
clean:
	@echo
	rm -rf build/
	find ${SRC} ${TESTS} | grep -E "(\.egg-info|__pycache__|\.pyc|\.pyo$$)" | xargs rm -rf

# Run all checking tools
.PHONY: checks
checks: isort-check black-check ec docstyle lint mypy bandit man-check pytest
