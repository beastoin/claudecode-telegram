"""Tests migrated from test.sh — media tag parsing."""
import os
import textwrap
import pytest
from pathlib import Path
from unittest.mock import patch, MagicMock


@pytest.fixture(autouse=True)
def _bridge_env(monkeypatch, tmp_path):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "fake:token")
    monkeypatch.setenv("ADMIN_CHAT_ID", "")
    monkeypatch.setenv("NODE_NAME", "test")
    sessions = tmp_path / "sessions"
    sessions.mkdir()
    monkeypatch.setenv("BRIDGE_SESSIONS_DIR", str(sessions))
    monkeypatch.setenv("TEAM_DIR", str(tmp_path / "team"))


def test_media_tag_parsing(tmp_path):
    from bridge import parse_image_tags, parse_file_tags

    # Create temp files so tags are recognized
    img = tmp_path / "test.jpg"
    img.write_bytes(b"\xff\xd8")  # JPEG magic
    img_a = tmp_path / "a.jpg"
    img_a.write_bytes(b"\xff\xd8")
    img_b = tmp_path / "b.png"
    img_b.write_bytes(b"\x89PNG")
    report = tmp_path / "report.pdf"
    report.write_bytes(b"%PDF")
    data_json = tmp_path / "data.json"
    data_json.write_text("{}")
    file_a = tmp_path / "a.txt"
    file_a.write_text("a")
    file_b = tmp_path / "b.csv"
    file_b.write_text("b")

    # === Image tag parsing ===
    text = f'Here is an image [[image:{img}|my caption]] and more text'
    clean, images = parse_image_tags(text)
    assert 'Here is an image' in clean, f'clean text wrong: {clean!r}'
    assert len(images) == 1, f'expected 1 image, got {len(images)}'
    assert images[0] == (str(img), 'my caption'), f'image data wrong: {images[0]}'

    # Non-existent file (tag stays)
    text2 = '[[image:/nonexistent/photo.png]]'
    clean2, images2 = parse_image_tags(text2)
    assert len(images2) == 0
    assert '[[image:' in clean2

    # Multiple images
    text3 = f'First [[image:{img_a}|cap1]] middle [[image:{img_b}|cap2]] end'
    clean3, images3 = parse_image_tags(text3)
    assert len(images3) == 2

    # Escaped image tag
    text4 = f'Example: \\[[image:{img}|caption]]'
    clean4, images4 = parse_image_tags(text4)
    assert len(images4) == 0

    # === File tag parsing ===
    text = f'Here is the report: [[file:{report}|Q4 Report]]'
    clean, files = parse_file_tags(text)
    assert 'Here is the report:' in clean
    assert len(files) == 1
    assert files[0] == (str(report), 'Q4 Report')

    text = f'Output: [[file:{data_json}]]'
    clean, files = parse_file_tags(text)
    assert len(files) == 1
    assert files[0] == (str(data_json), '')

    text = 'Output: [[file:/nonexistent/file.txt]]'
    clean, files = parse_file_tags(text)
    assert len(files) == 0
    assert '[[file:' in clean

    text = f'[[file:{file_a}|A]] and [[file:{file_b}|B]]'
    clean, files = parse_file_tags(text)
    assert len(files) == 2

    # Escaped file tag
    text = f'Example: \\[[file:{report}|caption]]'
    clean, files = parse_file_tags(text)
    assert len(files) == 0


def test_notify_parses_image_tags(tmp_path):
    from bridge import parse_image_tags

    img = tmp_path / "test.png"
    img.write_bytes(b"\x89PNG")

    text = f'Hello [[image:{img}|test caption]] world'
    clean, images = parse_image_tags(text)
    assert 'Hello' in clean, f'clean text should have Hello: {clean}'
    assert '[[image:' not in clean, f'tag should be removed: {clean}'
    assert len(images) == 1, f'expected 1 image, got {len(images)}'
    assert images[0][1] == 'test caption', f'caption mismatch: {images[0][1]}'


def test_notify_parses_image_tags_remote(tmp_path):
    import bridge

    with patch.object(bridge, 'get_worker_host', return_value='remote-host'):
        _accept_all = lambda p: (True, bridge.Path(p))
        text = 'Check this [[image:/tmp/remote.png|compare]]'
        clean, images = bridge._parse_media_tags(text, 'image', _accept_all)
        assert '[[image:' not in clean, f'tag should be removed: {clean}'
        assert len(images) == 1, f'expected 1 image, got {len(images)}'
        assert images[0][0] == '/tmp/remote.png'
        assert images[0][1] == 'compare'
