"""Markdown-to-Telegram-HTML conversion utilities.

Extracted from telegram.py to reduce LOC in the core module.
"""
from __future__ import annotations

import re
from html.parser import HTMLParser
from typing import cast

from core import MarkdownToken


def escape_html(text: str) -> str:
    return text.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')
class _TelegramHTMLSanitizer(HTMLParser):
    SAFE_TAGS: frozenset[str] = frozenset({
        "b", "strong", "i", "em", "u", "ins", "s", "strike", "del",
        "code", "pre", "a", "blockquote", "span", "tg-emoji", "tg-spoiler",
    })
    SAFE_ATTRS: dict[str, frozenset[str]] = {
        "a": frozenset({"href"}),
        "code": frozenset({"class"}),
        "blockquote": frozenset({"expandable"}),
        "span": frozenset({"class"}),
        "tg-emoji": frozenset({"emoji-id"}), }
    def __init__(self, rejected_open_tags: list[str]) -> None:
        super().__init__(convert_charrefs=False)
        self._out: list[str] = []
        self._rejected_open_tags = rejected_open_tags
    def _escape_attr(self, value: str) -> str:
        return escape_html(value).replace('"', "&quot;")
    def _attrs_are_safe(self, tag: str, attrs: list[tuple[str, str | None]]) -> bool:
        allowed = self.SAFE_ATTRS.get(tag, frozenset())
        seen: set[str] = set()
        for name, value in attrs:
            if name in seen or name not in allowed: return False
            seen.add(name)
            if tag == "a" and name == "href":
                if value is None: return False
            elif tag == "code" and name == "class":
                if value is None or not value.startswith("language-"): return False
            elif tag == "blockquote" and name == "expandable":
                if value not in (None, "", "expandable"): return False
            elif tag == "span" and name == "class":
                if value != "tg-spoiler": return False
            elif tag == "tg-emoji" and name == "emoji-id":
                if value is None: return False
        return True
    def _render_start_tag(self, tag: str, attrs: list[tuple[str, str | None]]) -> str:
        if not attrs: return f"<{tag}>"
        rendered = []
        for name, value in attrs:
            if value is None: rendered.append(name)
            else: rendered.append(f'{name}="{self._escape_attr(value)}"')
        return f"<{tag} {' '.join(rendered)}>"
    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        accepted = tag in self.SAFE_TAGS and self._attrs_are_safe(tag, attrs)
        if accepted: self._out.append(self._render_start_tag(tag, attrs))
        else:
            self._out.append(escape_html(self.get_starttag_text() or f"<{tag}>"))
            self._rejected_open_tags.append(tag)
    def handle_endtag(self, tag: str) -> None:
        rejected_match = False
        for idx in range(len(self._rejected_open_tags) - 1, -1, -1):
            if self._rejected_open_tags[idx] == tag:
                rejected_match = True
                del self._rejected_open_tags[idx]
                break
        if tag in self.SAFE_TAGS and not rejected_match: self._out.append(f"</{tag}>")
        else: self._out.append(escape_html(f"</{tag}>"))
    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        accepted = tag in self.SAFE_TAGS and self._attrs_are_safe(tag, attrs)
        if accepted:
            start = self._render_start_tag(tag, attrs)
            self._out.append(f"{start[:-1]}/>")
        else: self._out.append(escape_html(self.get_starttag_text() or f"<{tag}/>"))
    def handle_data(self, data: str) -> None:
        self._out.append(escape_html(data))
    def handle_entityref(self, name: str) -> None:
        self._out.append(f"&{name};")
    def handle_charref(self, name: str) -> None:
        self._out.append(f"&#{name};")
    def handle_comment(self, data: str) -> None:
        self._out.append(escape_html(f"<!--{data}-->"))
    def html(self) -> str:
        return "".join(self._out)
def _sanitize_telegram_html(raw: str, rejected_open_tags: list[str]) -> str:
    if not raw: return ""
    sanitizer = _TelegramHTMLSanitizer(rejected_open_tags)
    sanitizer.feed(raw)
    sanitizer.close()
    return sanitizer.html()
def _render_md_inline_plain(children: list[MarkdownToken]) -> str:
    out: list[str] = []
    for tok in children:
        if tok.type in ("text", "code_inline"): out.append(tok.content)
        elif tok.type in ("softbreak", "hardbreak"): out.append(" ")
        elif tok.type == "image": out.append(tok.content or "image")
        elif tok.type in ("strong_open", "strong_close", "em_open", "em_close",
                          "s_open", "s_close", "link_open", "link_close",
                          "html_inline"):
            pass
        else:
            if tok.content: out.append(tok.content)
    return "".join(out)

_INLINE_TAG_MAP = {"strong_open": "<b>", "strong_close": "</b>", "em_open": "<i>",
                   "em_close": "</i>", "s_open": "<s>", "s_close": "</s>",
                   "link_close": "</a>", "softbreak": "\n", "hardbreak": "\n"}
def _render_md_inline_html(children: list[MarkdownToken], rejected_open_tags: list[str]) -> str:
    out: list[str] = []
    for tok in children:
        if tok.type == "text": out.append(escape_html(tok.content))
        elif tok.type == "code_inline": out.append(f"<code>{escape_html(tok.content)}</code>")
        elif tok.type in _INLINE_TAG_MAP: out.append(_INLINE_TAG_MAP[tok.type])
        elif tok.type == "link_open":
            href = escape_html((tok.attrs or {}).get("href", ""))
            out.append(f'<a href="{href}">')
        elif tok.type == "image":
            alt = escape_html(tok.content or "image"); src = escape_html((tok.attrs or {}).get("src", ""))
            out.append(f'[{alt}]({src})')
        elif tok.type == "html_inline": out.append(_sanitize_telegram_html(tok.content, rejected_open_tags))
        elif tok.content: out.append(escape_html(tok.content))
    return "".join(out)
def _render_table_as_pre(headers: list[str], rows: list[list[str]]) -> str:
    all_rows = [headers] + rows
    if not all_rows or not all_rows[0]: return ""
    num_cols = max(len(r) for r in all_rows); col_widths = [0] * num_cols
    for row in all_rows:
        for ci, cell in enumerate(row):
            if ci < num_cols: col_widths[ci] = max(col_widths[ci], len(cell))
    lines: list[str] = []
    for ri, row in enumerate(all_rows):
        cols = []
        for ci in range(num_cols):
            cell = row[ci] if ci < len(row) else ""
            cols.append(escape_html(cell.ljust(col_widths[ci])))
        lines.append("  ".join(cols).rstrip())
        if ri == 0: lines.append("═" * (sum(col_widths) + 2 * (num_cols - 1)))
    return f"<pre>{chr(10).join(lines)}</pre>\n"

_MULTI_SPACE_RE = re.compile(r'\S  +\S.*\S  +\S')
def _wrap_plain_tables(text: str) -> str:
    parts = re.split(r'(<pre>.*?</pre>)', text, flags=re.DOTALL)
    out: list[str] = []
    for part in parts:
        if part.startswith('<pre>'):
            out.append(part)
            continue
        lines = part.split('\n'); i = 0
        while i < len(lines):
            if _MULTI_SPACE_RE.search(lines[i]):
                run = [lines[i]]; j = i + 1
                while j < len(lines) and (_MULTI_SPACE_RE.search(lines[j]) or lines[j].strip() == ''):
                    run.append(lines[j])
                    j += 1
                tabular_count = sum(1 for line in run if _MULTI_SPACE_RE.search(line))
                if tabular_count >= 2:
                    while run and run[-1].strip() == '':
                        j -= 1
                        run.pop()
                    raw = []
                    for rl in run:
                        plain = re.sub(r'<[^>]+>', '', rl)
                        plain = plain.replace('&amp;', '&').replace('&lt;', '<').replace('&gt;', '>')
                        raw.append(plain)
                    content = escape_html('\n'.join(raw))
                    out.append(f'<pre>{content}</pre>')
                    i = j
                else:
                    out.append(lines[i])
                    i += 1
            else:
                out.append(lines[i])
                i += 1
    return '\n'.join(out) if out else text
def markdown_to_telegram_html(text: str) -> str:
    from markdown_it import MarkdownIt
    md = MarkdownIt("commonmark").enable("strikethrough").enable("table"); tokens = md.parse(text)
    result: list[str] = []
    list_depth = 0
    ordered_counter: list[int] = []
    in_table = False
    table_row: list[str] = []
    table_headers: list[str] = []
    table_rows: list[list[str]] = []
    in_thead = False
    rejected_open_tags: list[str] = []
    i = 0
    while i < len(tokens):
        tok = tokens[i]
        if tok.type == "paragraph_open": pass
        elif tok.type == "paragraph_close":
            if not in_table: result.append("\n")
        elif tok.type == "inline":
            if in_table: table_row.append(_render_md_inline_plain(cast(list[MarkdownToken], tok.children or [])))
            else:
                result.append(_render_md_inline_html(cast(list[MarkdownToken], tok.children or []), rejected_open_tags))
        elif tok.type == "heading_open": result.append("<b>")
        elif tok.type == "heading_close": result.append("</b>\n")
        elif tok.type == "fence":
            lang = tok.info.strip() if tok.info else ""; code = escape_html(tok.content.rstrip("\n"))
            if lang: result.append(f'<pre><code class="language-{escape_html(lang)}">{code}</code></pre>\n')
            else: result.append(f"<pre>{code}</pre>\n")
        elif tok.type == "code_block":
            code = escape_html(tok.content.rstrip("\n"))
            result.append(f"<pre>{code}</pre>\n")
        elif tok.type == "blockquote_open": result.append("<blockquote>")
        elif tok.type == "blockquote_close":
            if result and result[-1].endswith("\n"): result[-1] = result[-1][:-1]
            result.append("</blockquote>\n")
        elif tok.type == "bullet_list_open": list_depth += 1
        elif tok.type == "bullet_list_close":
            list_depth -= 1
            if list_depth == 0: result.append("\n")
        elif tok.type == "ordered_list_open":
            list_depth += 1
            ordered_counter.append(0)
        elif tok.type == "ordered_list_close":
            list_depth -= 1
            ordered_counter.pop()
            if list_depth == 0: result.append("\n")
        elif tok.type == "list_item_open":
            indent = "  " * (list_depth - 1)
            if ordered_counter:
                ordered_counter[-1] += 1
                result.append(f"{indent}{ordered_counter[-1]}. ")
            else: result.append(f"{indent}• ")
        elif tok.type == "list_item_close":
            if result and not result[-1].endswith("\n"): result.append("\n")
        elif tok.type == "table_open":
            in_table = True; table_headers = []; table_rows = []
        elif tok.type == "table_close":
            in_table = False; pre_block = _render_table_as_pre(table_headers, table_rows)
            if pre_block: result.append(pre_block)
            table_headers = []; table_rows = []
        elif tok.type == "thead_open": in_thead = True
        elif tok.type == "thead_close": in_thead = False
        elif tok.type in ("tbody_open", "tbody_close"): pass
        elif tok.type == "tr_open": table_row = []
        elif tok.type == "tr_close":
            if in_thead: table_headers = table_row[:]
            else: table_rows.append(table_row[:])
            table_row = []
        elif tok.type in ("th_open", "th_close", "td_open", "td_close"): pass
        elif tok.type == "hr": result.append("————\n")
        elif tok.type == "html_block": result.append(_sanitize_telegram_html(tok.content, rejected_open_tags))
        else:
            if tok.content: result.append(escape_html(tok.content))
        i += 1
    output = "".join(result).strip()
    while "\n\n\n" in output: output = output.replace("\n\n\n", "\n\n")
    return _wrap_plain_tables(output)
def _pipe_tables_to_html(text: str) -> str:
    import re
    def _parse_row(line: str) -> list[str] | None:
        line = line.strip()
        if line.startswith('|'): line = line[1:]
        if line.endswith('|'): line = line[:-1]
        return [cell.strip() for cell in line.split('|')]
    def _esc(s: str) -> str:
        return s.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')
    def _cell_md(s: str) -> str:
        s = _esc(s); s = re.sub(r'\*\*(.+?)\*\*', r'<b>\1</b>', s); s = re.sub(r'__(.+?)__', r'<b>\1</b>', s)
        s = re.sub(r'(?<!\*)\*(?!\*)(.+?)(?<!\*)\*(?!\*)', r'<i>\1</i>', s); s = re.sub(r'~~(.+?)~~', r'<s>\1</s>', s)
        s = re.sub(r'`([^`]+)`', r'<code>\1</code>', s)
        s = re.sub(r'\[([^\]]+)\]\(([^)]+)\)', r'<a href="\2">\1</a>', s)
        return s
    lines = text.split('\n'); result = []; i = 0; in_code_fence = False
    while i < len(lines):
        if re.match(r'^\s*(`{3,}|~{3,})', lines[i]):
            in_code_fence = not in_code_fence
            result.append(lines[i])
            i += 1
            continue
        if (not in_code_fence
                and i + 1 < len(lines)
                and '|' in lines[i]
                and re.match(r'^\s*\|[\s:]*-+[\s:]*(\|[\s:]*-+[\s:]*)*\|?\s*$', lines[i + 1])):
            headers = _parse_row(lines[i]); aligns = []
            for cell in (_parse_row(lines[i + 1]) or []):
                cell = cell.strip()
                if cell.startswith(':') and cell.endswith(':'): aligns.append(' style="text-align:center"')
                elif cell.endswith(':'): aligns.append(' style="text-align:right"')
                else: aligns.append('')
            html = ['<table>']
            html.append('<tr>')
            for j, h in enumerate(headers or []):
                align = aligns[j] if j < len(aligns) else ''
                html.append(f'<th{align}>{_cell_md(h)}</th>')
            html.append('</tr>')
            i += 2
            while i < len(lines) and '|' in lines[i] and lines[i].strip().startswith('|'):
                cells = _parse_row(lines[i])
                html.append('<tr>')
                for j, cell in enumerate(cells or []):
                    align = aligns[j] if j < len(aligns) else ''
                    html.append(f'<td{align}>{_cell_md(cell)}</td>')
                html.append('</tr>')
                i += 1
            html.append('</table>')
            result.append('\n'.join(html))
        else:
            result.append(lines[i])
            i += 1
    return '\n'.join(result)
