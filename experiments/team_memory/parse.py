#!/usr/bin/env python3
"""
Team Memory — Telegram JSON Parser/Normalizer
Parses Telegram chat export, extracts metadata, filters noise, outputs cleaned JSONL.

Usage:
  python3 parse.py                        # full export
  python3 parse.py --days 7               # last 7 days only
  python3 parse.py --days 7 --out /tmp/team-memory-parsed.jsonl
"""

import argparse
import json
import re
import sys
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

# === Constants ===

EXPORT_ZIP = Path.home() / "team/exports/ChatExport_2026-04-08-text.json.zip"
DEFAULT_OUT = "/tmp/team-memory-parsed.jsonl"

# Known agent names (for @mention and prefix detection)
AGENTS = {
    "lee", "mon", "geni", "chen", "kelvin", "kenji", "luck", "kai",
    "sora", "noa", "jin", "ren", "yuki", "ryo", "hiro", "taro",
    "x", "finn", "noa",
}

# Noise commands — no semantic value, skip entirely
NOISE_COMMANDS = {
    "start", "hire", "end", "settings", "compact", "new", "pilot",
    "pause", "restart", "progress", "status", "list", "use", "rewind",
    "retart",  # common typo for restart
}

# /worker_name routing commands — also noise
NOISE_AGENT_COMMANDS = AGENTS.copy()

ALL_NOISE = NOISE_COMMANDS | NOISE_AGENT_COMMANDS


def flatten_text(raw_text):
    """Flatten Telegram's text field (string or rich array) to plain string."""
    if isinstance(raw_text, str):
        return raw_text
    if isinstance(raw_text, list):
        parts = []
        for part in raw_text:
            if isinstance(part, str):
                parts.append(part)
            elif isinstance(part, dict):
                parts.append(part.get("text", ""))
        return "".join(parts)
    return ""


def is_noise_command(text):
    """Check if message is a noise-only bot command."""
    text = text.strip()
    if not text.startswith("/"):
        return False
    # Extract command name (first word after /)
    match = re.match(r"^/(\w+)", text)
    if not match:
        return False
    cmd = match.group(1).lower()
    if cmd in ALL_NOISE:
        # Check if the rest is just flags/agent names (no real content)
        rest = text[match.end():].strip()
        # Allow if there's substantial content after the command
        # e.g., "/pilot lee" is noise, but a hypothetical "/pilot here's what to do" is not
        # In practice, these commands are always short
        if len(rest) < 50:
            return True
    return False


def extract_target_agents(text, sender):
    """Extract target agent(s) from message text."""
    agents_found = set()

    if sender == "Thinh":
        # Manager messages: look for @agent_name mentions
        for match in re.finditer(r"@(\w+)", text):
            name = match.group(1).lower()
            if name in AGENTS:
                agents_found.add(name)

    elif sender == "beasts":
        # Bot messages: look for "agent_name:" prefix (agent responding)
        match = re.match(r"^(\w+):", text)
        if match:
            name = match.group(1).lower()
            if name in AGENTS:
                agents_found.add(name)

    return sorted(agents_found) if agents_found else []


def has_command(text):
    """Check if message contains a bot command (even if not pure noise)."""
    return bool(re.match(r"^/\w+", text.strip()))


def parse_export(zip_path, days=None):
    """Parse Telegram export and yield normalized message dicts."""
    with zipfile.ZipFile(zip_path) as zf:
        with zf.open("result.json") as f:
            data = json.load(f)

    messages = data.get("messages", [])

    # Date filter
    cutoff_ts = 0
    if days:
        cutoff_ts = int((datetime.now(timezone.utc) - timedelta(days=days)).timestamp())

    skipped_noise = 0
    skipped_service = 0
    skipped_short = 0
    skipped_date = 0
    emitted = 0

    for msg in messages:
        # Skip service messages
        if msg.get("type") != "message":
            skipped_service += 1
            continue

        ts = int(msg.get("date_unixtime", 0))

        # Date filter
        if cutoff_ts and ts < cutoff_ts:
            skipped_date += 1
            continue

        sender = msg.get("from", "")
        raw_text = msg.get("text", "")
        text = flatten_text(raw_text)

        # Skip noise commands
        if is_noise_command(text):
            skipped_noise += 1
            continue

        # Skip very short messages (< 10 chars, likely just emojis or "ok")
        if len(text.strip()) < 10:
            skipped_short += 1
            continue

        target_agents = extract_target_agents(text, sender)

        record = {
            "id": msg.get("id"),
            "timestamp": msg.get("date", ""),
            "timestamp_unix": ts,
            "from": sender,
            "text": text,
            "target_agents": target_agents,
            "has_command": has_command(text),
            "reply_to": msg.get("reply_to_message_id"),
        }

        emitted += 1
        yield record

    print(f"Parse stats:", file=sys.stderr)
    print(f"  Emitted:         {emitted}", file=sys.stderr)
    print(f"  Skipped (date):  {skipped_date}", file=sys.stderr)
    print(f"  Skipped (noise): {skipped_noise}", file=sys.stderr)
    print(f"  Skipped (short): {skipped_short}", file=sys.stderr)
    print(f"  Skipped (svc):   {skipped_service}", file=sys.stderr)


def main():
    parser = argparse.ArgumentParser(description="Parse Telegram export for team memory")
    parser.add_argument("--zip", default=str(EXPORT_ZIP), help="Path to Telegram export zip")
    parser.add_argument("--days", type=int, help="Only include last N days")
    parser.add_argument("--out", default=DEFAULT_OUT, help="Output JSONL path")
    args = parser.parse_args()

    out_path = Path(args.out)
    count = 0

    with open(out_path, "w") as f:
        for record in parse_export(args.zip, days=args.days):
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
            count += 1

    print(f"Wrote {count} records to {out_path}", file=sys.stderr)


if __name__ == "__main__":
    main()
