SHELL := /bin/bash

WORKSPACE ?= workspace
SPEC ?= specs/examples/todo-api.md
TARGET_DIR ?=
RUN_DIR ?=
SLICE ?=
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

.PHONY: help bootstrap doctor test lint require-target-dir run plan architect prd slice loop verify commit pr

help:
	@printf "Targets:\n"
	@printf "  make bootstrap          Install Python deps in the current runtime (and build the image when run on the host)\n"
	@printf "  make doctor             Check runtime and auth mounts in the current runtime\n"
	@printf "  make plan SPEC=... TARGET_DIR=/path/to/repo      Run intake, recon, and planning stages\n"
	@printf "  make architect SPEC=... TARGET_DIR=/path/to/repo Run architecture stage and critique\n"
	@printf "  make prd SPEC=... TARGET_DIR=/path/to/repo       Run PRD stage and critique\n"
	@printf "  make slice SPEC=... TARGET_DIR=/path/to/repo     Generate implementation slices\n"
	@printf "  make loop SPEC=... TARGET_DIR=/path/to/repo SLICE=S001   Run Ralph loop for one slice or the sole remaining candidate\n"
	@printf "  make verify SPEC=... TARGET_DIR=/path/to/repo SLICE=S001 Verify one slice or the sole remaining candidate\n"
	@printf "  make commit SPEC=... TARGET_DIR=/path/to/repo SLICE=S001 Commit one slice or the sole remaining candidate\n"
	@printf "  make pr SPEC=... TARGET_DIR=/path/to/repo SLICE=S001     Create a pull request for one slice or the sole remaining candidate\n"
	@printf "  make run SPEC=... TARGET_DIR=/path/to/repo       Run the full workflow\n"
	@printf "  make test               Run the full pytest suite in the current runtime\n"

require-target-dir:
	@test -n "$(TARGET_DIR)" || (printf "TARGET_DIR is required. Example: make run SPEC=specs/task-management.md TARGET_DIR=/workspace/my-app\n" >&2; exit 1)

bootstrap:
ifeq ($(IN_CONTAINER),1)
	uv sync
else
	$(DOCKER_COMPOSE) build $(WORKSPACE)
	$(UV_SYNC)
endif

doctor:
	$(UV_RUN) ainative doctor

plan: require-target-dir
	$(UV_RUN) ainative stage --stage plan --spec $(SPEC) $(if $(TARGET_DIR),--workspace-dir $(TARGET_DIR),) $(if $(RUN_DIR),--run-dir $(RUN_DIR),)

architect: require-target-dir
	$(UV_RUN) ainative stage --stage architecture --spec $(SPEC) $(if $(TARGET_DIR),--workspace-dir $(TARGET_DIR),) $(if $(RUN_DIR),--run-dir $(RUN_DIR),)

prd: require-target-dir
	$(UV_RUN) ainative stage --stage prd --spec $(SPEC) $(if $(TARGET_DIR),--workspace-dir $(TARGET_DIR),) $(if $(RUN_DIR),--run-dir $(RUN_DIR),)

slice: require-target-dir
	$(UV_RUN) ainative stage --stage slice --spec $(SPEC) $(if $(TARGET_DIR),--workspace-dir $(TARGET_DIR),) $(if $(RUN_DIR),--run-dir $(RUN_DIR),)

loop: require-target-dir
	$(UV_RUN) ainative loop --spec $(SPEC) $(if $(TARGET_DIR),--workspace-dir $(TARGET_DIR),) $(if $(RUN_DIR),--run-dir $(RUN_DIR),) $(if $(SLICE),--slice-id $(SLICE),)

verify: require-target-dir
	$(UV_RUN) ainative verify --spec $(SPEC) $(if $(TARGET_DIR),--workspace-dir $(TARGET_DIR),) $(if $(RUN_DIR),--run-dir $(RUN_DIR),) $(if $(SLICE),--slice-id $(SLICE),)

commit: require-target-dir
	$(UV_RUN) ainative commit --spec $(SPEC) $(if $(TARGET_DIR),--workspace-dir $(TARGET_DIR),) $(if $(RUN_DIR),--run-dir $(RUN_DIR),) $(if $(SLICE),--slice-id $(SLICE),)

pr: require-target-dir
	$(UV_RUN) ainative pr --spec $(SPEC) $(if $(TARGET_DIR),--workspace-dir $(TARGET_DIR),) $(if $(RUN_DIR),--run-dir $(RUN_DIR),) $(if $(SLICE),--slice-id $(SLICE),)

run: require-target-dir
	$(UV_RUN) ainative run --spec $(SPEC) $(if $(TARGET_DIR),--workspace-dir $(TARGET_DIR),) $(if $(RUN_DIR),--run-dir $(RUN_DIR),) $(RUN_ARGS)

lint:
	$(UV_RUN) python -m compileall ai_native tests

test:
	$(UV_RUN) pytest
