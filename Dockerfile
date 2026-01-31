# Dockerfile for claudecode-telegram sandbox workers
#
# This image runs Claude Code workers in isolated Docker containers.
# Home directory (~) is mounted to /workspace at runtime.
#
# Build: docker build -t claudecode-telegram:latest .
# Test:  docker run --rm -it claudecode-telegram:latest claude --version
#
FROM node:22-bookworm-slim

LABEL org.opencontainers.image.title="claudecode-telegram"
LABEL org.opencontainers.image.description="Claude Code sandbox worker for Telegram bridge"
LABEL org.opencontainers.image.source="https://github.com/anthropics/claudecode-telegram"

# Install system dependencies
# - git: version control (required by many Claude Code operations)
# - curl: HTTP client for webhooks and APIs
# - jq: JSON processing for scripts
# - python3: scripting and tooling
# - ca-certificates: HTTPS connections
# - openssh-client: git over SSH
# - bash: shell scripts
# - procps: process utilities (ps, top)
# - less: pager for viewing files
# - vim-tiny: minimal text editor
RUN apt-get update && apt-get install -y --no-install-recommends \
    git \
    curl \
    jq \
    python3 \
    python3-pip \
    ca-certificates \
    openssh-client \
    bash \
    procps \
    less \
    vim-tiny \
    && rm -rf /var/lib/apt/lists/* \
    && ln -sf /usr/bin/vim.tiny /usr/bin/vim

# Install Claude Code CLI globally
RUN npm install -g @anthropic-ai/claude-code

# Use existing 'node' user (UID/GID 1000) for bind mount compatibility
# Rename to 'claude' for clarity and create home directory structure
# Note: Don't create /home/claude/.claude - it will be a symlink to /workspace/.claude
RUN usermod -l claude -d /home/claude -m node \
    && groupmod -n claude node \
    && chown -R claude:claude /home/claude

# Create workspace directory (will be mounted at runtime)
RUN mkdir -p /workspace && chown claude:claude /workspace

# Switch to non-root user
USER claude

# Set working directory and HOME to match mount point
WORKDIR /workspace
ENV HOME=/workspace

# Symlink /home/claude/.claude -> /workspace/.claude for compatibility
# (settings.json has hardcoded paths like /home/claude/.claude/hooks/...)
RUN ln -sf /workspace/.claude /home/claude/.claude

# Default command: run claude with skip permissions flag
# (actual command is overridden by bridge.py get_docker_run_cmd)
CMD ["claude", "--dangerously-skip-permissions"]
