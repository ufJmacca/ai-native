SHELL := /bin/bash

WORKSPACE ?= workspace
SPEC ?= specs/examples/todo-api.md
RUN_DIR ?=
DOCKER_COMPOSE ?= docker compose
DC_RUN = $(DOCKER_COMPOSE) run --rm $(WORKSPACE)
RUN_ARGS ?=

.PHONY: help bootstrap doctor test lint run plan architect prd slice loop verify commit pr

help:
	@printf "Targets:\n"
	@printf "  make bootstrap          Build the workspace image and install Python deps\n"
	@printf "  make doctor             Check runtime and auth mounts inside the container\n"
	@printf "  make plan SPEC=...      Run intake, recon, and planning stages\n"
	@printf "  make architect SPEC=... Run architecture stage and critique\n"
	@printf "  make prd SPEC=...       Run PRD stage and critique\n"
	@printf "  make slice SPEC=...     Generate implementation slices\n"
	@printf "  make loop SPEC=...      Run Ralph loop for every slice\n"
	@printf "  make verify SPEC=...    Verify all generated slices\n"
	@printf "  make commit SPEC=...    Commit verified slice changes\n"
	@printf "  make pr SPEC=...        Create a pull request\n"
	@printf "  make run SPEC=...       Run the full workflow\n"
	@printf "  make test               Run the full pytest suite in Docker\n"

bootstrap:
	$(DOCKER_COMPOSE) build $(WORKSPACE)
	$(DC_RUN) uv sync

doctor:
	$(DC_RUN) uv run ainative doctor

plan:
	$(DC_RUN) uv run ainative stage --stage plan --spec $(SPEC) $(if $(RUN_DIR),--run-dir $(RUN_DIR),)

architect:
	$(DC_RUN) uv run ainative stage --stage architecture --spec $(SPEC) $(if $(RUN_DIR),--run-dir $(RUN_DIR),)

prd:
	$(DC_RUN) uv run ainative stage --stage prd --spec $(SPEC) $(if $(RUN_DIR),--run-dir $(RUN_DIR),)

slice:
	$(DC_RUN) uv run ainative stage --stage slice --spec $(SPEC) $(if $(RUN_DIR),--run-dir $(RUN_DIR),)

loop:
	$(DC_RUN) uv run ainative stage --stage loop --spec $(SPEC) $(if $(RUN_DIR),--run-dir $(RUN_DIR),)

verify:
	$(DC_RUN) uv run ainative stage --stage verify --spec $(SPEC) $(if $(RUN_DIR),--run-dir $(RUN_DIR),)

commit:
	$(DC_RUN) uv run ainative stage --stage commit --spec $(SPEC) $(if $(RUN_DIR),--run-dir $(RUN_DIR),)

pr:
	$(DC_RUN) uv run ainative pr --spec $(SPEC) $(if $(RUN_DIR),--run-dir $(RUN_DIR),)

run:
	$(DC_RUN) uv run ainative run --spec $(SPEC) $(if $(RUN_DIR),--run-dir $(RUN_DIR),) $(RUN_ARGS)

lint:
	$(DC_RUN) uv run python -m compileall ai_native tests

test:
	$(DC_RUN) uv run pytest

