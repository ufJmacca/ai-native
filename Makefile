SHELL := /bin/bash

WORKSPACE ?= workspace
SPEC ?= specs/examples/todo-api.md
RUN_DIR ?=
DOCKER_COMPOSE ?= docker compose
RUN_ARGS ?=
IN_CONTAINER := $(shell test -f /.dockerenv && echo 1 || echo 0)

ifeq ($(IN_CONTAINER),1)
RUNNER :=
else
RUNNER := $(DOCKER_COMPOSE) run --rm $(WORKSPACE)
endif

UV_RUN = $(RUNNER) uv run
UV_SYNC = $(RUNNER) uv sync

.PHONY: help bootstrap doctor test lint run plan architect prd slice loop verify commit pr

help:
	@printf "Targets:\n"
	@printf "  make bootstrap          Install Python deps in the current runtime (and build the image when run on the host)\n"
	@printf "  make doctor             Check runtime and auth mounts in the current runtime\n"
	@printf "  make plan SPEC=...      Run intake, recon, and planning stages\n"
	@printf "  make architect SPEC=... Run architecture stage and critique\n"
	@printf "  make prd SPEC=...       Run PRD stage and critique\n"
	@printf "  make slice SPEC=...     Generate implementation slices\n"
	@printf "  make loop SPEC=...      Run Ralph loop for every slice\n"
	@printf "  make verify SPEC=...    Verify all generated slices\n"
	@printf "  make commit SPEC=...    Commit verified slice changes\n"
	@printf "  make pr SPEC=...        Create a pull request\n"
	@printf "  make run SPEC=...       Run the full workflow\n"
	@printf "  make test               Run the full pytest suite in the current runtime\n"

bootstrap:
ifeq ($(IN_CONTAINER),1)
	uv sync
else
	$(DOCKER_COMPOSE) build $(WORKSPACE)
	$(UV_SYNC)
endif

doctor:
	$(UV_RUN) ainative doctor

plan:
	$(UV_RUN) ainative stage --stage plan --spec $(SPEC) $(if $(RUN_DIR),--run-dir $(RUN_DIR),)

architect:
	$(UV_RUN) ainative stage --stage architecture --spec $(SPEC) $(if $(RUN_DIR),--run-dir $(RUN_DIR),)

prd:
	$(UV_RUN) ainative stage --stage prd --spec $(SPEC) $(if $(RUN_DIR),--run-dir $(RUN_DIR),)

slice:
	$(UV_RUN) ainative stage --stage slice --spec $(SPEC) $(if $(RUN_DIR),--run-dir $(RUN_DIR),)

loop:
	$(UV_RUN) ainative stage --stage loop --spec $(SPEC) $(if $(RUN_DIR),--run-dir $(RUN_DIR),)

verify:
	$(UV_RUN) ainative stage --stage verify --spec $(SPEC) $(if $(RUN_DIR),--run-dir $(RUN_DIR),)

commit:
	$(UV_RUN) ainative stage --stage commit --spec $(SPEC) $(if $(RUN_DIR),--run-dir $(RUN_DIR),)

pr:
	$(UV_RUN) ainative pr --spec $(SPEC) $(if $(RUN_DIR),--run-dir $(RUN_DIR),)

run:
	$(UV_RUN) ainative run --spec $(SPEC) $(if $(RUN_DIR),--run-dir $(RUN_DIR),) $(RUN_ARGS)

lint:
	$(UV_RUN) python -m compileall ai_native tests

test:
	$(UV_RUN) pytest
