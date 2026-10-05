# syntax=docker/dockerfile:1.7
# Plain Linux CI; no application, Docker daemon, model client or credentials.
FROM ghcr.io/astral-sh/uv:0.11.29@sha256:eb2843a1e56fd9e30c7276ce1a52cba86e64c7b385f5e3279a0e08e02dd058fc AS uv
FROM python:3.12.15-slim-bookworm@sha256:54c85f3c47607a77f32adec749d3c81d1348bf25833671f512b26a9b6d778cb3
ARG TARGETARCH
RUN test "$TARGETARCH" = amd64 \
    && apt-get update && apt-get install -y --no-install-recommends \
      build-essential ca-certificates curl git jq libgssapi-krb5-2 libicu72 \
      libssl3 libstdc++6 procps tini unzip xz-utils zlib1g \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --gid 1001 runner \
    && useradd --uid 1001 --gid 1001 --create-home --shell /bin/bash runner
COPY --from=uv /uv /uvx /usr/local/bin/
ADD --checksum=sha256:70920811a4f8ad4328818682bca5c6469c1c942fab52448868071d0063816613 https://github.com/actions/runner/releases/download/v2.337.0/actions-runner-linux-x64-2.337.0.tar.gz /tmp/runner.tar.gz
ADD --checksum=sha256:df450af89261115ef9f9e3830c3eeb2cc9213b63c720b1af623cb5dcbe2e02de https://nodejs.org/dist/v22.23.3/node-v22.23.3-linux-x64.tar.xz /tmp/node.tar.xz
RUN tar -xzf /tmp/runner.tar.gz -C /home/runner \
    && tar -xJf /tmp/node.tar.xz -C /usr/local --strip-components=1 \
    && rm /tmp/runner.tar.gz /tmp/node.tar.xz \
    && mkdir /home/runner/_work \
    && chown -R 1001:1001 /home/runner \
    && test -x /home/runner/run.sh && test -x /bin/kill && test -x /usr/bin/tini \
    && python3 --version | grep -Fx 'Python 3.12.15' \
    && uv --version | grep -F 'uv 0.11.29' \
    && node --version | grep -Fx v22.23.3
ARG SOURCE_REVISION
RUN python3 -c 'import re,sys; assert re.fullmatch("[0-9a-f]{40}",sys.argv[1])' "$SOURCE_REVISION"
LABEL org.opencontainers.image.source="https://github.com/iliazlobin/events-concierge" \
      org.opencontainers.image.revision="$SOURCE_REVISION" \
      org.opencontainers.image.description="Ephemeral Events Concierge Linux CI: Python 3.12.15 uv 0.11.29 Node 22.23.3"
ENV HOME=/home/runner LANG=C.UTF-8 \
    RUNNER_MANUALLY_TRAP_SIG=1 ACTIONS_RUNNER_PRINT_LOG_TO_STDOUT=1 \
    UV_PYTHON_DOWNLOADS=never UV_LINK_MODE=copy \
    PYTHONDONTWRITEBYTECODE=1
USER 1001:1001
WORKDIR /home/runner
ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["/home/runner/run.sh"]
