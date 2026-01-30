# claudecode-telegram worker image
# Runs Claude Code in an isolated container with hooks pre-installed

FROM ubuntu:22.04

# Avoid interactive prompts
ENV DEBIAN_FRONTEND=noninteractive

# Install dependencies
RUN apt-get update && apt-get install -y \
    curl \
    git \
    jq \
    tmux \
    python3 \
    python3-pip \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# Install Node.js (required for Claude Code)
RUN curl -fsSL https://deb.nodesource.com/setup_20.x | bash - \
    && apt-get install -y nodejs \
    && rm -rf /var/lib/apt/lists/*

# Install Claude Code CLI
RUN npm install -g @anthropic-ai/claude-code

# Create non-root user
RUN useradd -m -s /bin/bash claude
USER claude
WORKDIR /home/claude

# Copy hooks (will be mounted, but include defaults)
COPY --chown=claude:claude hooks/ /home/claude/.claude/hooks/

# Default environment
ENV BRIDGE_URL=""
ENV TMUX_PREFIX="claude-"
ENV SESSIONS_DIR="/home/claude/.claude/telegram/sessions"

# Entry point - run Claude Code
ENTRYPOINT ["claude"]
