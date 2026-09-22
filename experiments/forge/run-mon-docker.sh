#!/bin/bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DATA_DIR="${FORGE_MON_DATA:-$SCRIPT_DIR/forge-mon-data}"
BRIDGE_URL="${BRIDGE_URL:-http://host.docker.internal:8271}"
IDENTITY_FILE="${IDENTITY_FILE:-}"
SESSION_PREFIX="${SESSION_PREFIX:-claude-prod-}"
IMAGE_NAME="forge-mon"
CONTAINER_NAME="forge-mon"

CREDENTIALS_FILE="${CREDENTIALS_FILE:-}"

usage() {
  cat <<EOF
Usage: $0 [command] [options]

Commands:
  build       Build the Docker image (requires mon binary at build/)
  onboard     Set up OAuth + onboarding state (run once before first 'run')
  run         Start mon container
  stop        Stop and remove container
  shell       Open a shell in running container
  logs        Show container logs
  status      Check container status

Options:
  --bridge-url URL      Bridge URL (default: $BRIDGE_URL)
  --identity FILE       Age identity file for cred decryption
  --credentials FILE    Path to .credentials.json (for onboard)
  --session-prefix PFX  Tmux session prefix (default: claude-prod-)
  --data-dir DIR        Persistent data directory (default: $DATA_DIR)

Environment:
  BRIDGE_URL            Bridge URL
  IDENTITY_FILE         Path to age identity file
  CREDENTIALS_FILE      Path to .credentials.json
  FORGE_MON_DATA        Persistent data directory
EOF
  exit 1
}

cmd_build() {
  local binary="$SCRIPT_DIR/build/mon-linux-amd64"
  if [[ ! -f "$binary" ]]; then
    echo "Error: mon binary not found at $binary"
    echo "Run: make build && worker-forge build mon ..."
    exit 1
  fi
  cp "$binary" "$SCRIPT_DIR/mon-linux-amd64"
  docker build -f "$SCRIPT_DIR/Dockerfile.mon" -t "$IMAGE_NAME" "$SCRIPT_DIR"
  rm -f "$SCRIPT_DIR/mon-linux-amd64"
  echo "✓ Image built: $IMAGE_NAME"
}

cmd_onboard() {
  if [[ -z "$IDENTITY_FILE" ]]; then
    echo "Error: --identity or IDENTITY_FILE required"
    exit 1
  fi

  mkdir -p "$DATA_DIR/home" "$DATA_DIR/team"

  local identity_mount="/tmp/identity.agekey"

  # Run mon --onboard inside the container
  docker run --rm \
    --network host \
    -v "$DATA_DIR/home:/home/mon" \
    -v "$DATA_DIR/team:/home/mon/team" \
    -v "$IDENTITY_FILE:$identity_mount:ro" \
    "$IMAGE_NAME" \
    --identity "$identity_mount" \
    --onboard

  # Install OAuth credentials if provided
  if [[ -n "$CREDENTIALS_FILE" && -f "$CREDENTIALS_FILE" ]]; then
    cp "$CREDENTIALS_FILE" "$DATA_DIR/home/.claude/.credentials.json"
    chmod 600 "$DATA_DIR/home/.claude/.credentials.json"
    echo "✓ Credentials installed from $CREDENTIALS_FILE"
  elif [[ -f "$HOME/.claude/.credentials.json" ]]; then
    cp "$HOME/.claude/.credentials.json" "$DATA_DIR/home/.claude/.credentials.json"
    chmod 600 "$DATA_DIR/home/.claude/.credentials.json"
    echo "✓ Credentials installed from ~/.claude/.credentials.json"
  fi

  # Fix ownership (container runs as uid 1001)
  chown -R 1001:1001 "$DATA_DIR/home/.claude" 2>/dev/null || true
}

cmd_onboard_interactive() {
  if [[ -z "$IDENTITY_FILE" ]]; then
    echo "Error: --identity or IDENTITY_FILE required"
    exit 1
  fi

  mkdir -p "$DATA_DIR/home" "$DATA_DIR/team"

  local identity_mount="/tmp/identity.agekey"

  docker rm -f "${CONTAINER_NAME}-onboard" 2>/dev/null || true

  echo "Starting interactive onboard container..."
  echo "Claude will show an OAuth URL — open it in your browser and paste the code back."
  echo ""

  docker run -it --rm \
    --name "${CONTAINER_NAME}-onboard" \
    --network host \
    -v "$DATA_DIR/home:/home/mon" \
    -v "$DATA_DIR/team:/home/mon/team" \
    -v "$IDENTITY_FILE:$identity_mount:ro" \
    --entrypoint bash \
    "$IMAGE_NAME" \
    -c "mon --force-extract --bridge-url ${BRIDGE_URL} --identity $identity_mount --session-prefix ${SESSION_PREFIX} --launch-command 'claude --dangerously-skip-permissions' & sleep 10; tmux attach -t ${SESSION_PREFIX}mon"

  echo ""
  echo "✓ Interactive onboard done. Credentials saved to persistent volume."
}

cmd_run() {
  if [[ -z "$IDENTITY_FILE" ]]; then
    echo "Error: --identity or IDENTITY_FILE required"
    exit 1
  fi
  if [[ ! -f "$IDENTITY_FILE" ]]; then
    echo "Error: identity file not found: $IDENTITY_FILE"
    exit 1
  fi

  mkdir -p "$DATA_DIR/home" "$DATA_DIR/team"

  local identity_mount="/tmp/identity.agekey"

  docker rm -f "$CONTAINER_NAME" 2>/dev/null || true

  docker run -d \
    --name "$CONTAINER_NAME" \
    --network host \
    -v "$DATA_DIR/home:/home/mon" \
    -v "$DATA_DIR/team:/home/mon/team" \
    -v "$IDENTITY_FILE:$identity_mount:ro" \
    "$IMAGE_NAME" \
    --bridge-url "$BRIDGE_URL" \
    --identity "$identity_mount" \
    --session-prefix "$SESSION_PREFIX"

  echo "✓ Container started: $CONTAINER_NAME"
  echo "  Data: $DATA_DIR"
  echo "  Bridge: $BRIDGE_URL"
  echo ""
  echo "  shell:  $0 shell"
  echo "  logs:   $0 logs"
  echo "  stop:   $0 stop"
}

cmd_stop() {
  docker rm -f "$CONTAINER_NAME" 2>/dev/null && echo "✓ Stopped" || echo "Not running"
}

cmd_shell() {
  docker exec -it "$CONTAINER_NAME" bash
}

cmd_logs() {
  docker logs -f "$CONTAINER_NAME"
}

cmd_status() {
  if docker ps --format '{{.Names}}' | grep -qx "$CONTAINER_NAME"; then
    echo "✓ Running"
    docker exec "$CONTAINER_NAME" tmux has-session -t "${SESSION_PREFIX}mon" 2>/dev/null \
      && echo "  tmux: active" || echo "  tmux: no session"
  else
    echo "✗ Not running"
  fi
}

# Parse args
CMD="${1:-}"
shift || true

while [[ $# -gt 0 ]]; do
  case "$1" in
    --bridge-url) BRIDGE_URL="$2"; shift 2 ;;
    --identity) IDENTITY_FILE="$2"; shift 2 ;;
    --credentials) CREDENTIALS_FILE="$2"; shift 2 ;;
    --session-prefix) SESSION_PREFIX="$2"; shift 2 ;;
    --data-dir) DATA_DIR="$2"; shift 2 ;;
    *) echo "Unknown option: $1"; usage ;;
  esac
done

case "$CMD" in
  build) cmd_build ;;
  onboard) cmd_onboard ;;
  onboard-interactive) cmd_onboard_interactive ;;
  run) cmd_run ;;
  stop) cmd_stop ;;
  shell) cmd_shell ;;
  logs) cmd_logs ;;
  status) cmd_status ;;
  *) usage ;;
esac
