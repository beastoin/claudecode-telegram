#!/bin/bash
# broadcast.sh - Send message to Telegram without needing a pending file
#
# Usage: ./broadcast.sh "Your message here"
#        ./broadcast.sh --from kelvin "Build complete!"
#
# This allows Claude sessions to send unprompted messages to Telegram.
# Token isolation maintained - bridge handles Telegram API.

set -euo pipefail

BRIDGE_PORT="${PORT:-8080}"
BRIDGE_URL="http://localhost:${BRIDGE_PORT}/broadcast"

# Parse arguments
FROM="${USER:-unknown}"
TEXT=""

while [[ $# -gt 0 ]]; do
    case $1 in
        --from|-f)
            FROM="$2"
            shift 2
            ;;
        *)
            TEXT="$1"
            shift
            ;;
    esac
done

if [[ -z "$TEXT" ]]; then
    echo "Usage: $0 [--from name] \"message\""
    echo "  --from, -f  Sender name (default: \$USER)"
    exit 1
fi

# Send to bridge
curl -s -X POST "$BRIDGE_URL" \
    -H "Content-Type: application/json" \
    -d "{\"from\": \"$FROM\", \"text\": \"$TEXT\"}"
