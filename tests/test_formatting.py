"""Tests migrated from test.sh — formatting category."""
import re

import pytest


@pytest.fixture(autouse=True)
def _bridge_env(monkeypatch, tmp_path):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "fake:token")
    monkeypatch.setenv("ADMIN_CHAT_ID", "")
    monkeypatch.setenv("NODE_NAME", "test")
    sessions = tmp_path / "sessions"
    sessions.mkdir()
    monkeypatch.setenv("BRIDGE_SESSIONS_DIR", str(sessions))
    monkeypatch.setenv("TEAM_DIR", str(tmp_path / "team"))


def test_formatting():
    from bridge import format_response_text, format_multipart_messages

    # Response prefix formatting
    text = 'Hello <code>world</code>'
    result = format_response_text('session-1', text)
    assert result == '<b>session-1:</b>\nHello <code>world</code>', f'prefix failed: {result}'

    # Single chunk - no part numbers
    chunks = ['Hello world']
    formatted = format_multipart_messages('worker', chunks)
    assert len(formatted) == 1
    assert formatted[0] == '<b>worker:</b>\nHello world'
    assert '(1/' not in formatted[0], 'single chunk should not have part numbers'

    # Multiple chunks - all have prefix
    chunks = ['Part 1 content', 'Part 2 content', 'Part 3 content']
    formatted = format_multipart_messages('lee', chunks)
    assert len(formatted) == 3
    assert formatted[0] == '<b>lee:</b>\nPart 1 content', f'first: {formatted[0]}'
    assert formatted[1] == '<b>lee:</b>\nPart 2 content', f'second: {formatted[1]}'
    assert formatted[2] == '<b>lee:</b>\nPart 3 content', f'third: {formatted[2]}'


def test_message_splitting():
    from bridge import split_message, markdown_to_telegram_html, TELEGRAM_MAX_LENGTH

    TAG_RE = re.compile(r'<(/?)(\w+)([^>]*?)>')
    VALID = {'b', 'i', 's', 'u', 'code', 'pre', 'a', 'strong', 'em', 'del', 'ins', 'strike'}

    def tags_balanced(html):
        stack = []
        for m in TAG_RE.finditer(html):
            close, tag = m.group(1) == '/', m.group(2).lower()
            if tag not in VALID:
                continue
            if close:
                for j in range(len(stack) - 1, -1, -1):
                    if stack[j] == tag:
                        stack.pop(j)
                        break
                else:
                    return False
            else:
                stack.append(tag)
        return len(stack) == 0

    # 1. Short message - no split
    chunks = split_message('Short message')
    assert len(chunks) == 1

    # 2. Split on newlines
    long_text = chr(10).join(['Line ' + str(i) + ' ' + 'x' * 100 for i in range(50)])
    chunks = split_message(long_text, max_len=4096)
    assert len(chunks) > 1
    for c in chunks:
        assert len(c) <= 4096

    # 3. Hard split (no natural breaks)
    chunks = split_message('x' * 10000, max_len=4096)
    assert len(chunks) >= 3
    for c in chunks:
        assert len(c) <= 4096

    # 4. HTML-aware: long code block stays balanced after split
    code = chr(10).join([f'echo line{i} padding' + 'x' * 40 for i in range(80)])
    fence = chr(96) * 3
    html = markdown_to_telegram_html(f'Script:\n\n{fence}bash\n{code}\n{fence}\n\nDone.')
    chunks = split_message(html, max_len=4096)
    assert len(chunks) >= 2, f'expected 2+ chunks, got {len(chunks)}'
    for i, c in enumerate(chunks):
        assert tags_balanced(c), f'chunk {i} has unbalanced HTML tags'
        assert len(c) <= 4096, f'chunk {i} too long: {len(c)}'

    # 5. Nested inline tags across split
    bt = chr(96)
    nested = f'Text **bold with {bt}code{bt} end** rest. ' * 100
    html = markdown_to_telegram_html(nested)
    chunks = split_message(html, max_len=4096)
    if len(chunks) > 1:
        for i, c in enumerate(chunks):
            assert tags_balanced(c), f'nested chunk {i} unbalanced'

    # 6. Code block reopened in continuation chunk
    code = chr(10).join([f'variable_{i} = "value_{i}_padding"' + 'x' * 30 for i in range(100)])
    html = markdown_to_telegram_html(f'{fence}python\n{code}\n{fence}')
    chunks = split_message(html, max_len=4096)
    assert len(chunks) >= 2
    # Second chunk should start with <pre><code (reopened)
    assert '<pre>' in chunks[1] or '<code' in chunks[1], 'continuation chunk missing reopened pre/code tag'
    for i, c in enumerate(chunks):
        assert tags_balanced(c), f'reopen chunk {i} unbalanced'


def test_send_response_html_formatting():
    import bridge

    # Track API calls — sendRichMessage must fail so code falls to HTML path
    calls = []

    def fake_api(method, data):
        calls.append((method, data))
        if method == 'sendRichMessage':
            return {'ok': False, 'error_code': 400, 'description': 'method not found'}
        return {'ok': True, 'result': {'message_id': 123}}

    orig_api = bridge.telegram_api
    bridge.telegram_api = fake_api

    try:
        # Send a response with markdown bold
        bridge.send_response_to_telegram('testworker', '**hello world**', 12345)

        html_calls = [c for c in calls if c[0] == 'sendMessage']
        assert len(html_calls) >= 1, f'Expected sendMessage call, got methods: {[c[0] for c in calls]}'
        method, data = html_calls[0]
        assert data['parse_mode'] == 'HTML', f'Expected HTML parse_mode, got {data.get("parse_mode")}'
        # markdown_to_telegram_html should convert **bold** to <b>bold</b>
        assert '<b>' in data['text'] or 'hello world' in data['text'], \
            f'Expected HTML bold tags or plain text, got: {data["text"]}'

        # Test with code block
        calls.clear()
        bridge.send_response_to_telegram('testworker', '```python\nprint(1)\n```', 12345)
        html_calls = [c for c in calls if c[0] == 'sendMessage']
        assert len(html_calls) >= 1, f'Expected sendMessage call for code block, got methods: {[c[0] for c in calls]}'
        method, data = html_calls[0]
        assert '<pre>' in data['text'] or '<code>' in data['text'] or 'print(1)' in data['text'], \
            f'Expected code HTML in text, got: {data["text"]}'
    finally:
        bridge.telegram_api = orig_api


def test_format_response_strips_name_prefix():
    from bridge import format_response_text

    # Normal message - no prefix to strip
    result = format_response_text('lee', 'hello world')
    assert result == '<b>lee:</b>\nhello world', f'unexpected: {result}'

    # Message with redundant prefix - should strip it
    result = format_response_text('lee', 'lee: hello world')
    assert result == '<b>lee:</b>\nhello world', f'unexpected: {result}'

    # Case-insensitive prefix strip
    result = format_response_text('lee', 'Lee: hello world')
    assert result == '<b>lee:</b>\nhello world', f'unexpected: {result}'

    # Different worker name - should NOT strip
    result = format_response_text('lee', 'chen: hello world')
    assert result == '<b>lee:</b>\nchen: hello world', f'unexpected: {result}'

    # Prefix with leading whitespace
    result = format_response_text('lee', '  lee: hello world')
    assert result == '<b>lee:</b>\nhello world', f'unexpected: {result}'


def test_session_id_backwards_compat_old_format(tmp_path):
    import bridge

    orig = bridge.SESSIONS_DIR
    bridge.SESSIONS_DIR = tmp_path / 'sessions2'
    bridge.SESSIONS_DIR.mkdir()

    try:
        worker_dir = bridge.SESSIONS_DIR / 'oldworker'
        worker_dir.mkdir()
        (worker_dir / 'claude_session_cwd').write_text('/home/claude/some-project')

        # Old format: just UUID, no CWD line
        (worker_dir / 'claude_session_id').write_text('old-format-uuid-only')

        # Should still return the UUID (backwards compatible)
        sid = bridge.get_claude_session_id('oldworker')
        assert sid == 'old-format-uuid-only', f'old format should work, got {sid!r}'
    finally:
        bridge.SESSIONS_DIR = orig


def test_format_reply_context_with_timestamp():
    import bridge

    b = bridge.CommandRouter.__new__(bridge.CommandRouter)
    result = b.format_reply_context('do this', 'I did that', 1719403200)
    assert 'at 2024-06-26 12:00 UTC' in result, f'Expected timestamp in result, got {result!r}'
    assert 'Manager reply:' in result
    assert 'do this' in result
    assert 'I did that' in result


def test_format_reply_context_without_timestamp():
    import bridge

    b = bridge.CommandRouter.__new__(bridge.CommandRouter)
    result = b.format_reply_context('do this', 'I did that')
    assert 'at ' not in result, f'Unexpected timestamp in result: {result!r}'
    assert 'Context (your previous message):' in result


def test_markdown_to_telegram_html():
    from bridge import markdown_to_telegram_html

    # Bold, italic, strikethrough, inline code
    r = markdown_to_telegram_html('**bold** *italic* ~~strike~~ `code`')
    assert '<b>bold</b>' in r, f'Bold: {r}'
    assert '<i>italic</i>' in r, f'Italic: {r}'
    assert '<s>strike</s>' in r, f'Strike: {r}'
    assert '<code>code</code>' in r, f'Code: {r}'

    # Code block with language
    r = markdown_to_telegram_html('```python\nprint(1)\n```')
    assert '<pre><code class="language-python">' in r, f'Fence lang: {r}'

    # Link
    r = markdown_to_telegram_html('[click](http://example.com)')
    assert '<a href="http://example.com">click</a>' in r, f'Link: {r}'

    # Heading as bold
    r = markdown_to_telegram_html('## Heading')
    assert '<b>Heading</b>' in r, f'Heading: {r}'

    # Bullet list
    r = markdown_to_telegram_html('- a\n- b')
    assert '• a' in r and '• b' in r, f'Bullets: {r}'

    # Table as pre-block with aligned columns
    r = markdown_to_telegram_html('| A | B |\n|---|---|\n| 1 | 2 |')
    assert '<pre>' in r and 'A' in r and '1' in r, f'Table: {r}'

    # Blockquote
    r = markdown_to_telegram_html('> quoted')
    assert '<blockquote>' in r, f'Blockquote: {r}'

    # HTML escaping
    r = markdown_to_telegram_html('a < b & c > d')
    assert '&lt;' in r and '&amp;' in r, f'Escape: {r}'

    # HTML block with safe tags (e.g. <pre>...</pre> in source)
    r = markdown_to_telegram_html('<pre>a > b & c</pre>')
    assert '<pre>' in r, f'HTML block pre open: {r}'
    assert '</pre>' in r, f'HTML block pre close: {r}'
    assert '&lt;pre&gt;' not in r, f'pre tag should NOT be escaped: {r}'
    assert '&gt;' in r, f'Content > should be escaped: {r}'
    assert '&amp;' in r, f'Content & should be escaped: {r}'

    # HTML block with <code class="language-xxx"> inside <pre> (Telegram syntax)
    r = markdown_to_telegram_html('<pre><code class="language-bash">echo hello</code></pre>')
    assert '<code class="language-bash">' in r, f'code+class should be safe tag: {r}'
    assert '</code>' in r, f'closing code should be preserved: {r}'
    assert '&lt;code' not in r, f'code+class must NOT be escaped: {r}'

    # HTML inline safe tags
    r = markdown_to_telegram_html('<strong>bold</strong>')
    assert '<strong>bold</strong>' in r, f'strong should be preserved: {r}'
    r = markdown_to_telegram_html('<em>italic</em>')
    assert '<em>italic</em>' in r, f'em should be preserved: {r}'
    r = markdown_to_telegram_html('<del>strike</del>')
    assert '<del>strike</del>' in r, f'del should be preserved: {r}'

    # Unsafe tags should be escaped (open + close)
    r = markdown_to_telegram_html('<div>unsafe</div>')
    assert '&lt;div&gt;unsafe&lt;/div&gt;' in r, f'unsafe div should be escaped: {r}'

    # Telegram spoiler span rules
    r = markdown_to_telegram_html('<span class="tg-spoiler">secret</span>')
    assert '<span class="tg-spoiler">secret</span>' in r, f'tg-spoiler span should be preserved: {r}'
    r = markdown_to_telegram_html('<span class="other">bad</span>')
    assert '&lt;span class="other"&gt;bad&lt;/span&gt;' in r, f'non tg-spoiler span should be escaped: {r}'

    # Empty
    assert markdown_to_telegram_html('') == '', 'Empty'


def test_pipe_tables_inline_markdown():
    from bridge import _pipe_tables_to_html

    # Bold in table cells
    text = '''| Day | DAU |
|-----|-----|
| Jul 22 | **902** |
| Jul 23 | **1,096** |'''

    result = _pipe_tables_to_html(text)
    assert '<b>902</b>' in result, f'Expected bold 902, got: {result}'
    assert '<b>1,096</b>' in result, f'Expected bold 1,096, got: {result}'
    assert '**' not in result, f'Raw ** markers should not appear: {result}'

    # Inline code in cells
    text2 = '''| Key | Value |
|-----|-------|
| name | `foo` |'''

    result2 = _pipe_tables_to_html(text2)
    assert '<code>foo</code>' in result2, f'Expected code foo, got: {result2}'

    # Plain cells (no markdown) still work
    text3 = '''| A | B |
|---|---|
| hello | 42 |'''

    result3 = _pipe_tables_to_html(text3)
    assert '<td>hello</td>' in result3, f'Expected plain hello, got: {result3}'
    assert '<td>42</td>' in result3, f'Expected plain 42, got: {result3}'
