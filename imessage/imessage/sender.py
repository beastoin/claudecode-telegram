"""
iMessage sender via AppleScript.

Sends messages through Messages.app using osascript.
"""

import os
import subprocess
import time
import logging
from typing import Optional

logger = logging.getLogger(__name__)

# Default chunk size (conservative, iMessage supports more)
DEFAULT_MAX_CHUNK_LEN = 3500

def get_max_chunk_len() -> int:
    """Get max message chunk length from env."""
    return int(os.environ.get('IMESSAGE_MAX_CHUNK_LEN', DEFAULT_MAX_CHUNK_LEN))

def escape_applescript_string(text: str) -> str:
    """Escape text for AppleScript string literal."""
    # Escape backslashes first, then quotes
    return text.replace('\\', '\\\\').replace('"', '\\"')

def ensure_messages_running() -> bool:
    """Ensure Messages.app is running."""
    script = '''
    tell application "Messages"
        launch
        delay 1
    end tell
    '''
    try:
        subprocess.run(
            ['osascript', '-e', script],
            capture_output=True,
            timeout=10
        )
        return True
    except Exception as e:
        logger.error(f"Failed to launch Messages.app: {e}")
        return False

def send_message(
    handle: str,
    text: str,
    retry_count: int = 3,
    retry_delay: float = 1.0
) -> tuple[bool, str]:
    """
    Send an iMessage to a handle (phone/email).

    Args:
        handle: Phone number (+1234567890) or email (user@icloud.com)
        text: Message text
        retry_count: Number of retries on failure
        retry_delay: Seconds between retries

    Returns:
        (success, message) tuple
    """
    if not handle:
        return False, "No handle provided"

    if not text:
        return False, "No text provided"

    # Ensure Messages is running
    if not ensure_messages_running():
        return False, "Failed to launch Messages.app"

    escaped_handle = escape_applescript_string(handle)
    escaped_text = escape_applescript_string(text)

    # AppleScript to send message
    # This targets a "buddy" (contact) by their handle
    script = f'''
    tell application "Messages"
        set targetService to 1st account whose service type = iMessage
        set targetBuddy to participant "{escaped_handle}" of targetService
        send "{escaped_text}" to targetBuddy
    end tell
    '''

    last_error = ""
    for attempt in range(retry_count):
        try:
            result = subprocess.run(
                ['osascript', '-e', script],
                capture_output=True,
                text=True,
                timeout=30
            )

            if result.returncode == 0:
                logger.info(f"Sent message to {handle} ({len(text)} chars)")
                return True, "OK"

            last_error = result.stderr.strip()
            logger.warning(
                f"AppleScript failed (attempt {attempt + 1}/{retry_count}): {last_error}"
            )

            # Check for permission error
            if "not authorized" in last_error.lower():
                return False, (
                    "Automation permission required.\n"
                    "Go to System Settings > Privacy & Security > Automation\n"
                    "and enable Messages for Terminal (or your terminal app)."
                )

        except subprocess.TimeoutExpired:
            last_error = "AppleScript timeout"
            logger.warning(f"AppleScript timeout (attempt {attempt + 1}/{retry_count})")
        except Exception as e:
            last_error = str(e)
            logger.warning(f"AppleScript error (attempt {attempt + 1}/{retry_count}): {e}")

        if attempt < retry_count - 1:
            time.sleep(retry_delay)

    return False, f"Failed after {retry_count} attempts: {last_error}"

def send_chunked(
    handle: str,
    text: str,
    max_len: Optional[int] = None,
    delay_between: float = 0.5
) -> tuple[bool, str]:
    """
    Send a long message in chunks.

    Args:
        handle: Phone number or email
        text: Message text (may be long)
        max_len: Max chars per chunk (default from env)
        delay_between: Seconds between chunks

    Returns:
        (success, message) tuple
    """
    max_len = max_len or get_max_chunk_len()

    if len(text) <= max_len:
        return send_message(handle, text)

    # Split into chunks
    chunks = split_message(text, max_len)
    total = len(chunks)

    for i, chunk in enumerate(chunks, 1):
        # Add part indicator for multi-part messages
        if total > 1:
            prefix = f"({i}/{total}) "
            chunk_text = prefix + chunk
        else:
            chunk_text = chunk

        success, msg = send_message(handle, chunk_text)
        if not success:
            return False, f"Failed on part {i}/{total}: {msg}"

        if i < total:
            time.sleep(delay_between)

    return True, f"Sent {total} parts"

def split_message(text: str, max_len: int) -> list[str]:
    """
    Split text into chunks at natural boundaries.

    Prefers splitting at:
    1. Blank lines (paragraph breaks)
    2. Single newlines
    3. Spaces
    4. Hard cut (last resort)
    """
    if len(text) <= max_len:
        return [text]

    chunks = []
    remaining = text

    while remaining:
        if len(remaining) <= max_len:
            chunks.append(remaining)
            break

        # Find best split point within max_len
        search_area = remaining[:max_len]

        # Try blank line (paragraph break)
        pos = search_area.rfind('\n\n')
        if pos > max_len // 2:
            chunks.append(remaining[:pos].rstrip())
            remaining = remaining[pos:].lstrip()
            continue

        # Try single newline
        pos = search_area.rfind('\n')
        if pos > max_len // 2:
            chunks.append(remaining[:pos].rstrip())
            remaining = remaining[pos + 1:].lstrip()
            continue

        # Try space
        pos = search_area.rfind(' ')
        if pos > max_len // 2:
            chunks.append(remaining[:pos].rstrip())
            remaining = remaining[pos + 1:].lstrip()
            continue

        # Hard cut
        chunks.append(remaining[:max_len])
        remaining = remaining[max_len:]

    return chunks

def check_permissions() -> tuple[bool, str]:
    """
    Check if we have Automation permission for Messages.app.

    Returns:
        (success, message) tuple
    """
    # Simple test: try to get Messages app name
    script = '''
    tell application "Messages"
        return name
    end tell
    '''

    try:
        result = subprocess.run(
            ['osascript', '-e', script],
            capture_output=True,
            text=True,
            timeout=10
        )

        if result.returncode == 0:
            return True, "OK"

        if "not authorized" in result.stderr.lower():
            return False, (
                "Automation permission required.\n"
                "Go to System Settings > Privacy & Security > Automation\n"
                "and enable Messages for Terminal (or your terminal app)."
            )

        return False, result.stderr.strip()

    except subprocess.TimeoutExpired:
        return False, "AppleScript timeout"
    except Exception as e:
        return False, str(e)
