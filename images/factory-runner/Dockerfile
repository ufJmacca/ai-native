# Release automation must pass immutable manifest-list references.  No mutable
# base-image default is provided.
ARG PYTHON_BASE_IMAGE=required.invalid/python@sha256:0000000000000000000000000000000000000000000000000000000000000000
ARG UV_BASE_IMAGE=required.invalid/uv@sha256:0000000000000000000000000000000000000000000000000000000000000000

FROM ${UV_BASE_IMAGE} AS uv-source

FROM ${PYTHON_BASE_IMAGE} AS builder

ARG PYTHON_BASE_IMAGE
ARG UV_BASE_IMAGE
ARG FACTORY_RUNNER_CREATED
ARG FACTORY_RUNNER_SCHEMA_MANIFEST_SHA256
ARG FACTORY_RUNNER_SCHEMA_SET_DIGEST
ARG FACTORY_RUNNER_SOURCE_COMMIT
ARG FACTORY_RUNNER_SOURCE_TAG
ARG FACTORY_RUNNER_VERSION
ARG FACTORY_RUNNER_WHEEL_SHA256

ENV UV_LINK_MODE=copy \
    UV_NO_CACHE=1

COPY --from=uv-source /uv /usr/local/bin/uv
COPY pyproject.toml uv.lock /build/

WORKDIR /build

RUN python -c 'import os, re; image = re.compile(r"^[^@\s]+@sha256:[0-9a-f]{64}$"); digest = re.compile(r"^sha256:[0-9a-f]{64}$"); version_pattern = re.compile(r"^(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)$"); commit = re.compile(r"^[0-9a-f]{40}$"); created = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(?:\.[0-9]+)?Z$"); version = os.environ["FACTORY_RUNNER_VERSION"]; tag = os.environ.get("FACTORY_RUNNER_SOURCE_TAG", ""); assert image.fullmatch(os.environ["PYTHON_BASE_IMAGE"]); assert image.fullmatch(os.environ["UV_BASE_IMAGE"]); assert version_pattern.fullmatch(version); assert commit.fullmatch(os.environ["FACTORY_RUNNER_SOURCE_COMMIT"]); assert created.fullmatch(os.environ["FACTORY_RUNNER_CREATED"]); assert tag in ("", f"ai-native-base-v{version}"); assert digest.fullmatch(os.environ["FACTORY_RUNNER_WHEEL_SHA256"]); assert digest.fullmatch(os.environ["FACTORY_RUNNER_SCHEMA_SET_DIGEST"]); assert digest.fullmatch(os.environ["FACTORY_RUNNER_SCHEMA_MANIFEST_SHA256"])'

RUN uv export --frozen --no-dev --no-emit-project \
        --format requirements-txt \
        --output-file /build/requirements.txt \
    && uv venv --python /usr/local/bin/python /opt/ainative \
    && uv pip install \
        --python /opt/ainative/bin/python \
        --require-hashes \
        --requirements /build/requirements.txt

COPY dist/*.whl /wheelhouse/

RUN set -eu; \
    wheel_count="$(find /wheelhouse -maxdepth 1 -type f -name "*.whl" | wc -l | tr -d " ")"; \
    test "${wheel_count}" = "1"; \
    wheel_path="$(find /wheelhouse -maxdepth 1 -type f -name "*.whl" -print -quit)"; \
    actual_wheel_digest="sha256:$(sha256sum "${wheel_path}" | cut -d " " -f 1)"; \
    test "${actual_wheel_digest}" = "${FACTORY_RUNNER_WHEEL_SHA256}"; \
    uv pip install \
        --python /opt/ainative/bin/python \
        --no-deps /wheelhouse/*.whl; \
    /opt/ainative/bin/python -c 'import os; from ai_native.factory_runner.build_identity import load_build_identity; identity = load_build_identity(); tag = os.environ.get("FACTORY_RUNNER_SOURCE_TAG") or None; assert identity.distribution == "ai-native-base"; assert identity.version == os.environ["FACTORY_RUNNER_VERSION"]; assert identity.source_repository == "ufJmacca/ai-native"; assert identity.source_commit == os.environ["FACTORY_RUNNER_SOURCE_COMMIT"]; assert identity.source_tag == tag; assert identity.image is None; assert identity.schema_set_digest == os.environ["FACTORY_RUNNER_SCHEMA_SET_DIGEST"]; assert identity.schema_manifest_sha256 == os.environ["FACTORY_RUNNER_SCHEMA_MANIFEST_SHA256"]'

FROM ${PYTHON_BASE_IMAGE} AS runtime

ARG PYTHON_BASE_IMAGE
ARG FACTORY_RUNNER_CREATED
ARG FACTORY_RUNNER_SCHEMA_MANIFEST_SHA256
ARG FACTORY_RUNNER_SCHEMA_SET_DIGEST
ARG FACTORY_RUNNER_SOURCE_COMMIT
ARG FACTORY_RUNNER_SOURCE_TAG
ARG FACTORY_RUNNER_VERSION
ARG FACTORY_RUNNER_WHEEL_SHA256

ENV HOME=/home/ainative \
    PATH=/opt/ainative/bin:/usr/local/bin:/usr/bin:/bin \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONNOUSERSITE=1 \
    PYTHONUNBUFFERED=1 \
    TMPDIR=/run/ainative

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates git \
    && rm -rf /var/lib/apt/lists/* \
    && install -d -o root -g root -m 0555 \
        /home/ainative \
        /input \
        /output \
        /run/ainative \
        /workspace

COPY --from=builder /opt/ainative /opt/ainative

LABEL org.opencontainers.image.title="AI Native Factory Runner" \
    org.opencontainers.image.description="Non-interactive factory-runner-protocol/v1 execution image" \
    org.opencontainers.image.source="https://github.com/ufJmacca/ai-native" \
    org.opencontainers.image.revision="${FACTORY_RUNNER_SOURCE_COMMIT}" \
    org.opencontainers.image.version="${FACTORY_RUNNER_VERSION}" \
    org.opencontainers.image.created="${FACTORY_RUNNER_CREATED}" \
    org.opencontainers.image.ref.name="${FACTORY_RUNNER_SOURCE_TAG}" \
    org.opencontainers.image.base.name="${PYTHON_BASE_IMAGE}" \
    io.ai-native.factory-runner.distribution="ai-native-base" \
    io.ai-native.factory-runner.protocol="factory-runner-protocol/v1" \
    io.ai-native.factory-runner.wheel-sha256="${FACTORY_RUNNER_WHEEL_SHA256}" \
    io.ai-native.factory-runner.schema-set-digest="${FACTORY_RUNNER_SCHEMA_SET_DIGEST}" \
    io.ai-native.factory-runner.schema-manifest-sha256="${FACTORY_RUNNER_SCHEMA_MANIFEST_SHA256}"

USER 10001:10001
WORKDIR /workspace
STOPSIGNAL SIGTERM

ENTRYPOINT ["/opt/ainative/bin/ainative", "factory"]
CMD ["--help"]
