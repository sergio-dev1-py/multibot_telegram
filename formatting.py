"""Small, conservative Markdown-to-Telegram-HTML renderer.

Telegram rejects malformed entity markup, so every send falls back to plain text.
This module deliberately implements only the constructs Hermes commonly emits:
fenced code, inline code, bold, italic, strikethrough, links and headings.
"""

from __future__ import annotations

import html
import re

MAX_TELEGRAM_LENGTH = 4096
_FENCE_RE = re.compile(r"^```([A-Za-z0-9_+.-]*)\s*$")
_INLINE_CODE_RE = re.compile(r"`([^`\n]+)`")
_BOLD_RE = re.compile(r"\*\*(.+?)\*\*", re.DOTALL)
_STRIKE_RE = re.compile(r"~~(.+?)~~", re.DOTALL)
_ITALIC_RE = re.compile(r"(?<!\*)\*([^*\n]+?)\*(?!\*)")
_LINK_RE = re.compile(r"\[([^\]]+)\]\((https?://[^)\s]+)\)")
_HEADING_RE = re.compile(r"^\s{0,3}#{1,6}\s+(.+?)\s*$")


def utf16_len(value: str) -> int:
    return len(value.encode("utf-16-le")) // 2


def truncate_utf16(value: str, limit: int) -> str:
    """Truncate without splitting a UTF-16 surrogate pair."""
    out: list[str] = []
    used = 0
    for char in value:
        size = utf16_len(char)
        if used + size > limit:
            break
        out.append(char)
        used += size
    return "".join(out)


def _protect_code(text: str) -> tuple[str, list[str]]:
    """Replace fenced and inline code with placeholders before inline markup."""
    placeholders: list[str] = []

    def keep(value: str) -> str:
        token = f"\x00CODE{len(placeholders)}\x00"
        placeholders.append(value)
        return token

    out: list[str] = []
    in_fence = False
    fence_lang = ""
    fence_lines: list[str] = []
    for line in text.splitlines(keepends=True):
        bare = line.rstrip("\r\n")
        match = _FENCE_RE.match(bare.strip())
        if match and not in_fence:
            in_fence = True
            fence_lang = match.group(1)
            fence_lines = []
            out.append(keep(f"\x00OPENFENCE{fence_lang}\x00"))
            continue
        if in_fence and bare.strip() == "```":
            body = "".join(fence_lines)
            out.append(keep(f"\x00FENCE\x00{html.escape(body, quote=False)}\x00ENDFENCE\x00"))
            in_fence = False
            fence_lines = []
            continue
        if in_fence:
            fence_lines.append(line)
            continue
        out.append(line)

    if in_fence:
        # An unterminated fence: render what we have as a normal code block.
        body = "".join(fence_lines)
        out.append(keep(f"\x00FENCE\x00{html.escape(body, quote=False)}\x00ENDFENCE\x00"))

    text = "".join(out)

    def inline(match: re.Match[str]) -> str:
        return keep(f"\x00FENCE\x00{html.escape(match.group(1), quote=False)}\x00ENDFENCE\x00")

    text = _INLINE_CODE_RE.sub(inline, text)
    return text, placeholders


def _restore(text: str, placeholders: list[str]) -> str:
    def repl(match: re.Match[str]) -> str:
        idx = int(match.group(1))
        return placeholders[idx] if 0 <= idx < len(placeholders) else ""

    text = re.sub(r"\x00CODE(\d+)\x00", repl, text)
    text = text.replace("\x00FENCE\x00", "<pre>").replace("\x00ENDFENCE\x00", "</pre>")
    text = re.sub(r"\x00OPENFENCE[A-Za-z0-9_+.-]*\x00", "", text)
    return text


def render_html(content: str) -> str:
    """Return Telegram ``parse_mode='HTML'`` text for *content*."""
    if not content:
        return ""
    text, placeholders = _protect_code(content)
    # Escape everything that is not a protected code placeholder.
    text = html.escape(text, quote=False)

    # Links: labels/URLs are already escaped; only http(s) targets are converted.
    def link(match: re.Match[str]) -> str:
        label, url = match.group(1), match.group(2)
        if not url.startswith(("http://", "https://")):
            return match.group(0)
        safe_url = url.replace('"', "&quot;").replace("<", "&lt;").replace(">", "&gt;")
        return f'<a href="{safe_url}">{label}</a>'

    text = _LINK_RE.sub(link, text)
    text = _BOLD_RE.sub(r"<b>\1</b>", text)
    text = _STRIKE_RE.sub(r"<s>\1</s>", text)
    text = _ITALIC_RE.sub(r"<i>\1</i>", text)
    text = "\n".join(
        f"<b>{m.group(1)}</b>" if (m := _HEADING_RE.match(line)) else line
        for line in text.splitlines()
    )
    return _restore(text, placeholders)


def plain_fallback(content: str) -> str:
    """Best-effort readable plain text for a Telegram entity rejection."""
    try:
        from gateway.platforms.helpers import strip_markdown  # type: ignore

        return strip_markdown(content or "")
    except Exception:
        return content or ""


def split_text(content: str, max_length: int = MAX_TELEGRAM_LENGTH) -> list[str]:
    """Split preserving code fences when Hermes's splitter is available."""
    if not content:
        return []
    try:
        from gateway.platforms.base import truncate_message  # type: ignore

        return truncate_message(
            content, max_length=max_length, len_fn=utf16_len
        ) or [content]
    except Exception:
        # Standalone fallback: paragraph/newline-aware, never split a UTF-16
        # surrogate in half.
        chunks: list[str] = []
        remaining = content
        while remaining:
            if utf16_len(remaining) <= max_length:
                chunks.append(remaining)
                break
            cut = len(remaining)
            while cut > 1 and utf16_len(remaining[:cut]) > max_length:
                cut = int(cut * 0.9) if cut > 32 else cut - 1
            window = remaining[:cut]
            boundary = max(window.rfind("\n\n"), window.rfind("\n"), window.rfind(" "))
            if boundary <= 0:
                boundary = cut
            chunks.append(remaining[:boundary].rstrip())
            remaining = remaining[boundary:].lstrip()
        return chunks
