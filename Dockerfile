FROM ubuntu:24.04 AS base
ENV LANG=C.UTF-8
ENV LC_ALL=C.UTF-8
ENV DEBIAN_FRONTEND=noninteractive
# uv-managed Pythons go in a fixed path so they can be copied between stages
ENV UV_PYTHON_INSTALL_DIR=/opt/python


FROM base AS python-deps
ARG PYTHON_VERSION=3.13.9
ARG UV_VERSION=0.13.5

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates curl \
    && rm -rf /var/lib/apt/lists/*

# DowVERSION=$(curl -fsSLI -o /dev/null -w '%{url_effective}'nload the uv binary. "latest" resolves the newest release tag from the
# GitHub redirect; pass --build-arg UV_VERSION=<version> to pin one instead.
RUN set -eu; \
    arch="$(uname -m)"; \
    curl -fsSL "https://releases.astral.sh/github/uv/releases/download/${UV_VERSION}/uv-${arch}-unknown-linux-gnu.tar.gz" \
        | tar -xz --strip-components=1 -C /usr/local/bin \
            "uv-${arch}-unknown-linux-gnu/uv" "uv-${arch}-unknown-linux-gnu/uvx"

# The venv lives outside the project so it can be copied to the runtime stage
# at the same absolute path (venv scripts hardcode it)
ENV UV_PROJECT_ENVIRONMENT=/.venv \
    UV_LINK_MODE=copy \
    UV_COMPILE_BYTECODE=1

RUN uv python install ${PYTHON_VERSION}

WORKDIR /build
COPY pyproject.toml uv.lock ./
RUN uv sync --locked --no-dev --no-install-project --python ${PYTHON_VERSION}


FROM base AS runtime
COPY --from=python-deps /opt/python /opt/python
COPY --from=python-deps /.venv /.venv

RUN groupadd -r python && useradd --no-log-init -rm -d /home/python -g python python
RUN mkdir /opt/code /data && chown -R python:python /opt/code /data
ENV PATH="/.venv/bin/:$PATH"
ENV KJELLER_STATE_DB=/data/kjeller.db
VOLUME /data
EXPOSE 8000

WORKDIR /opt/code
USER python

COPY kjeller/ .

ENTRYPOINT ["python", "main.py"]
