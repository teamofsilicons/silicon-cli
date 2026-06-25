# syntax=docker/dockerfile:1
FROM node:22-bookworm-slim

ARG SILICON_CLI_SPEC=silicon-cli
ARG SILICON_BROWSER_SPEC=silicon-browser
ARG SILICON_INTERFACE_CLI_SPEC=@teamofsilicons/silicon-interface-cli
ARG CLAUDE_CODE_SPEC=@anthropic-ai/claude-code
ARG CODEX_SPEC=@openai/codex

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PATH="/opt/silicon-runtime/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        bash \
        build-essential \
        ca-certificates \
        curl \
        git \
        libffi-dev \
        libssl-dev \
        openssh-client \
        pkg-config \
        procps \
        python3 \
        python3-dev \
        python3-pip \
        python3-venv \
        tini \
        unzip \
    && rm -rf /var/lib/apt/lists/*

RUN python3 -m venv /opt/silicon-runtime \
    && /opt/silicon-runtime/bin/pip install --upgrade pip setuptools wheel \
    && /opt/silicon-runtime/bin/pip install "${SILICON_CLI_SPEC}" "${SILICON_BROWSER_SPEC}"

RUN npm install -g \
    "${SILICON_INTERFACE_CLI_SPEC}" \
    "${CLAUDE_CODE_SPEC}" \
    "${CODEX_SPEC}"

COPY docker/runtime/runtime-entrypoint.sh /usr/local/bin/silicon-runtime-entrypoint
RUN chmod +x /usr/local/bin/silicon-runtime-entrypoint

WORKDIR /silicon
ENTRYPOINT ["tini", "--", "/usr/local/bin/silicon-runtime-entrypoint"]
