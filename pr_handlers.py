"""PR review HTTP handlers — extracted from bridge.py Handler class."""
from __future__ import annotations
import base64 as _b64
import json
import os
import urllib.error
from typing import TYPE_CHECKING, cast
from urllib.parse import parse_qs, urlparse

if TYPE_CHECKING:
    from urllib.parse import ParseResult

def _b():
    import bridge as _b; return _b

def handle_pr_file_content(handler, parsed: ParseResult) -> None:
    b = _b()
    params = dict(parse_qs(parsed.query)); token = params.get("token", [None])[0]
    if not b.tokens.validate_pr_review(token): handler._send_status(403); return
    owner = params.get("owner", [None])[0]; repo = params.get("repo", [None])[0]
    path = params.get("path", [None])[0]; ref = params.get("ref", [None])[0]
    if not all([owner, repo, path, ref]): handler._send_status(400); return
    ok, out = b._gh_api_call([f"repos/{owner}/{repo}/contents/{path}?ref={ref}", "--jq", ".content"])
    if not ok: handler._send_status(504 if out == "timeout" else 404); return
    try:
        raw = _b64.b64decode(out.strip()).decode('utf-8', errors='replace')
        handler._send_json(200, raw.splitlines())
    except (ValueError, TypeError): handler._send_status(500)

def handle_pr_keepalive(handler, parsed: ParseResult) -> None:
    b = _b()
    params = dict(parse_qs(parsed.query)); token = params.get("token", [None])[0]
    if not b.tokens.validate_pr_review(token): handler._send_status(403); return
    handler._send_status(204)

def handle_pr_review_comments(handler, body: bytes) -> None:
    b = _b()
    try: data = cast(dict[str, object], json.loads(body))
    except (json.JSONDecodeError, ValueError): handler._send_text(400, "Invalid JSON"); return
    if b._str_field(data, "path") and b._int_field(data, "line"): handle_pr_comment(handler, body)
    else: handle_pr_general_comment(handler, body)

def _pr_guard(handler, body: bytes):
    b = _b()
    try: data = json.loads(body)
    except (json.JSONDecodeError, ValueError): handler._send_text(400, "Invalid JSON"); return None
    if not b.tokens.validate_pr_review(b._str_field(data, "token")): handler._send_text(403, "Token expired"); return None
    return data

def _pr_notify(text: str) -> None:
    b = _b()
    try:
        import urllib.request; req = urllib.request.Request(
            f"{b.BRIDGE_PUBLIC_URL or f'http://localhost:{b.PORT}'}/notifications",
            data=json.dumps({"text": text}).encode(), headers={"Content-Type": "application/json"})
        b._urlopen(req, timeout=b.TIMEOUT_TMUX_SEND)
    except (urllib.error.URLError, OSError, TimeoutError) as exc:
        b._log(b._LOG_DEBUG, "notify:unknown", f"{type(exc).__name__}: {exc}")

def _pr_fan_out(pr_num: int, comment_body: str, prefix: str = "") -> None:
    b = _b()
    targets, _ = b.command_router.parse_at_mentions(comment_body)
    if targets:
        msg = f"manager: PR #{pr_num} review comment{prefix}\n\n{comment_body}"
        for t in targets: b.send_to_worker(t, msg)

def handle_pr_general_comment(handler, body: bytes) -> None:
    b = _b()
    data = _pr_guard(handler, body)
    if not data: return
    owner = b._str_field(data, "owner"); repo = b._str_field(data, "repo"); pr_num = b._int_field(data, "pr_num")
    comment_body = b._str_field(data, "body").strip()
    if not all([owner, repo, pr_num, comment_body]): handler._send_text(400, "Missing required fields"); return
    ok, err = b._gh_api_call([f"repos/{owner}/{repo}/issues/{pr_num}/comments", "--method", "POST", "-f", f"body={comment_body}"])
    if not ok: handler._send_text(504 if err == "timeout" else 502, f"GitHub API error: {err[:200]}"); return
    _pr_notify(f"\U0001f4ac PR #{pr_num} comment:\n{comment_body[:500]}")
    _pr_fan_out(pr_num, comment_body)
    handler._send_json(200, {"ok": True})

def handle_pr_merge(handler, body: bytes) -> None:
    b = _b()
    data = _pr_guard(handler, body)
    if not data: return
    owner = b._str_field(data, "owner"); repo = b._str_field(data, "repo"); pr_num = b._int_field(data, "pr_num")
    merge_method = b._str_field(data, "merge_method", "merge")
    if merge_method not in ("merge", "squash", "rebase"): merge_method = "merge"
    if not all([owner, repo, pr_num]): handler._send_text(400, "Missing required fields"); return
    ok, err = b._gh_api_call([f"repos/{owner}/{repo}/pulls/{pr_num}/merge", "--method", "PUT", "-f", f"merge_method={merge_method}"], timeout=b.TIMEOUT_GIT_OP)
    if not ok: handler._send_text(504 if err == "timeout" else 502, f"Merge failed: {err[:300]}"); return
    _pr_notify(f"✅ PR #{pr_num} merged ({merge_method}) via review page")
    handler._send_json(200, {"ok": True})

def handle_pr_review_endpoint(handler, parsed: ParseResult) -> None:
    b = _b()
    params = dict(parse_qs(parsed.query)); token = params.get("token", [None])[0]
    info = b.tokens.validate_pr_review(token)
    if not info: handler._send_html(b"<h2>Link expired</h2><p>Send <code>/pr &lt;url&gt;</code> in Telegram to get a fresh 5-minute link.</p>", 403); return
    pr_num = info["pr_num"]; html_path = f"/tmp/pr-review-{pr_num}.html"
    if not os.path.exists(html_path): handler._send_html(f"<h2>PR review not found</h2><p>File {html_path} missing. Re-run /pr command.</p>".encode(), 404); return
    with open(html_path, "rb") as f: handler._send_html(f.read())

def handle_pr_comment(handler, body: bytes) -> None:
    b = _b()
    data = _pr_guard(handler, body)
    if not data: return
    owner = b._str_field(data, "owner"); repo = b._str_field(data, "repo"); pr_num = b._int_field(data, "pr_num")
    path = b._str_field(data, "path"); line = b._int_field(data, "line"); side = b._str_field(data, "side", "RIGHT")
    comment_body = b._str_field(data, "body").strip(); head_sha = b._str_field(data, "head_sha")
    if not all([owner, repo, pr_num, path, line, comment_body, head_sha]): handler._send_text(400, "Missing required fields"); return
    gh_payload = json.dumps({"body": comment_body, "commit_id": head_sha, "path": path, "line": line, "side": side})
    ok, err = b._gh_api_call([f"repos/{owner}/{repo}/pulls/{pr_num}/comments", "--method", "POST", "--input", "-"], input_data=gh_payload)
    if not ok:
        b._log(b._LOG_ERROR, "pr-comment", f"GitHub API error: {err}"); handler._send_text(504 if err == "timeout" else 502, f"GitHub API error: {err}"); return
    if b.admin_chat_id: b.transport.send_text(b.admin_chat_id, f"\U0001f4ac PR #{pr_num} comment\n{path}:{line}\n\n{comment_body}")
    _pr_fan_out(pr_num, comment_body, f" on {path}:{line}")
    handler._send_json(200, {"ok": True})
