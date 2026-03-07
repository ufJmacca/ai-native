FROM mcr.microsoft.com/devcontainers/base:ubuntu-24.04

ARG NODE_MAJOR=22

ENV DEBIAN_FRONTEND=noninteractive
ENV UV_LINK_MODE=copy
ENV CODEX_HOME=/home/vscode/.codex
ENV UV_INSTALL_DIR=/usr/local/bin
ENV PATH=/home/vscode/.local/bin:${PATH}

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        ca-certificates \
        curl \
        git \
        gh \
        graphviz \
        jq \
        make \
        npm \
        openssh-client \
        python3 \
        python3-pip \
        python3-venv \
    && rm -rf /var/lib/apt/lists/*

RUN curl -LsSf https://astral.sh/uv/install.sh | sh
RUN npm install -g @mermaid-js/mermaid-cli @openai/codex

RUN mkdir -p /home/vscode/.codex /workspace \
    && chown -R vscode:vscode /home/vscode /workspace

USER vscode
WORKDIR /workspace
