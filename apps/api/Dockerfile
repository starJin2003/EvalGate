# EvalGate API. Built natively on the arm64 Ampere node — no buildx, no QEMU.
#
# Build context is the REPO ROOT, not this directory: the API is a uv workspace
# member and depends on packages/evalcore through the workspace, so a context
# rooted at apps/api could not see it.
#
#   nerdctl --namespace k8s.io build -f apps/api/Dockerfile -t evalgate-api:0.1.0 .
#
# The k8s.io namespace is not optional. containerd namespaces are hard
# isolation and kubelet only ever looks in k8s.io, so an image built into
# `default` is invisible to k3s while `ctr images list` still shows it present.

# --- builder ------------------------------------------------------------------
# The builder base is the SAME image as the runtime stage, with only the uv
# binary added. That matters: the venv built here is copied wholesale into the
# runtime stage, and a venv is bound to the exact interpreter path it was
# created against. A builder on a different Python — uv's own managed build, or
# a different Debian release — produces a venv whose symlinks dangle on arrival.
#
# uv comes from its own pinned binary-only image rather than a curl installer,
# so the version is reproducible and no network fetch happens inside the build.
FROM python:3.12-slim-bookworm AS builder
COPY --from=ghcr.io/astral-sh/uv:0.12.0 /uv /usr/local/bin/uv

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never

WORKDIR /app

# Workspace members are discovered from disk, so every member named by the root
# pyproject's `members` glob has to exist here even though only evalgate-api and
# its dependencies get installed. sdk and training contribute a pyproject and
# nothing else — neither is built.
COPY pyproject.toml uv.lock .python-version ./
COPY packages/sdk/pyproject.toml ./packages/sdk/
COPY training/pyproject.toml ./training/
COPY packages/evalcore/ ./packages/evalcore/
COPY apps/api/ ./apps/api/

# --frozen: fail rather than silently re-resolve if uv.lock is stale.
# --no-dev: pytest, ruff, and pre-commit have no business in a runtime image.
# --no-editable: install evalcore as a real package instead of a .pth pointing
#   at /app/packages, which would not exist in the runtime stage.
# --package: install this member only. A bare `uv sync` would install `training`
#   too and drag its dependency tree into the image.
RUN uv sync --frozen --no-dev --no-editable --package evalgate-api

# --- test ---------------------------------------------------------------------
# Same build, plus dev dependencies and the tests. Exists because
# tests/test_store.py's PostgresStore half needs a live database, and the only
# real Postgres this project has is the one on k3s — so the tests have to run
# from inside the cluster, not from a laptop. Built and run explicitly; the
# runtime stage below never depends on it.
#
#   nerdctl --namespace k8s.io build --target test -t evalgate-api:0.1.0-test .
#   kubectl -n evalgate run api-tests --rm -i --restart=Never \
#     --image=evalgate-api:0.1.0-test --image-pull-policy=IfNotPresent --env=...
FROM builder AS test
RUN uv sync --frozen --no-editable --package evalgate-api
ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONDONTWRITEBYTECODE=1
# apps/api explicitly, rather than the root testpaths: `training` is a workspace
# member that this context deliberately does not carry, so a bare pytest would
# fail at collection.
CMD ["pytest", "apps/api", "-q", "-p", "no:cacheprovider"]

# --- runtime ------------------------------------------------------------------
FROM python:3.12-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/app/.venv/bin:$PATH"

# Non-root, and a fixed uid so the k8s securityContext can name it. No shell:
# nothing in this image ever needs an interactive login.
RUN useradd --uid 10001 --create-home --shell /usr/sbin/nologin evalgate

WORKDIR /app
COPY --from=builder --chown=10001:10001 /app/.venv /app/.venv

USER 10001

EXPOSE 8000

# No --workers. Concurrency is k8s replicas, which keeps one process per pod and
# lets the readiness probe speak for exactly the process it is probing.
CMD ["uvicorn", "evalgate_api.app:app", "--host", "0.0.0.0", "--port", "8000"]
