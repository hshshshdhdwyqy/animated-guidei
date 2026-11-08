"""Convert the Markdown that Gemini writes into Telegram-safe HTML, and split long text."""
import html
import re

_FENCE = re.compile(r"```[ \t]*([\w+#.-]*)[ \t]*\n?(.*?)```", re.S)
_INLINE_CODE = re.compile(r"`([^`\n]+)`")


def split_markdown(text: str, limit: int = 3300) -> list:
    """Split text into chunks below `limit` chars, never breaking a code fence open."""
    text = (text or "").strip()
    if not text:
        return [""]
    chunks, cur, in_fence, fence_lang = [], "", False, ""

    def flush():
        nonlocal cur
        out = cur
        if in_fence:
            out += "\n```"
        if out.strip():
            chunks.append(out.rstrip())
        cur = f"```{fence_lang}\n" if in_fence else ""

    for line in text.split("\n"):
        # break very long single lines
        pieces = [line[i:i + limit] for i in range(0, len(line), limit)] or [""]
        for piece in pieces:
            if len(cur) + len(piece) + 1 > limit and cur.strip():
                flush()
            cur += piece + "\n"
        stripped = line.strip()
        if stripped.startswith("```"):
            if in_fence:
                in_fence = False
            else:
                in_fence = True
                fence_lang = stripped[3:].strip()
    if cur.strip():
        chunks.append(cur.rstrip() + ("\n```" if in_fence else ""))
    return chunks or [""]


def md_to_html(text: str) -> str:
    stash = []

    def keep(fragment: str) -> str:
        stash.append(fragment)
        return f"\x00{len(stash) - 1}\x00"

    def fence(m):
        lang, code = m.group(1), m.group(2).strip("\n")
        code = html.escape(code, quote=False)
        if lang:
            return keep(f'<pre><code class="language-{html.escape(lang)}">{code}</code></pre>')
        return keep(f"<pre>{code}</pre>")

    text = _FENCE.sub(fence, text or "")
    text = _INLINE_CODE.sub(lambda m: keep(f"<code>{html.escape(m.group(1), quote=False)}</code>"), text)

    # markdown tables -> monospaced block
    lines, out, table = text.split("\n"), [], []

    def end_table():
        nonlocal table
        if table:
            rows = [r for r in table if not re.fullmatch(r"\|?[\s:\-|]+\|?", r)]
            out.append(keep("<pre>" + html.escape("\n".join(rows), quote=False) + "</pre>"))
            table = []

    for ln in lines:
        if ln.strip().startswith("|") and ln.strip().count("|") >= 2:
            table.append(ln.strip())
        else:
            end_table()
            out.append(ln)
    end_table()
    text = "\n".join(out)

    text = html.escape(text, quote=False)

    # headings, quotes, bullets (line based)
    text = re.sub(r"(?m)^\s{0,3}#{1,6}\s+(.+?)\s*#*\s*$", r"<b>\1</b>", text)
    text = re.sub(r"(?m)^\s*[-*+]\s+", "• ", text)
    text = re.sub(r"(?m)^(?:&gt;\s?.*(?:\n|$))+", _quote, text)
    text = re.sub(r"(?m)^[-*_]{3,}\s*$", "──────────", text)

    # inline styles
    text = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", text, flags=re.S)
    text = re.sub(r"(?<![\w])__(.+?)__(?![\w])", r"<b>\1</b>", text, flags=re.S)
    text = re.sub(r"(?<![\w*])\*(?!\s)([^*\n]+?)(?<!\s)\*(?![\w*])", r"<i>\1</i>", text)
    text = re.sub(r"(?<![\w])_(?!\s)([^_\n]+?)(?<!\s)_(?![\w])", r"<i>\1</i>", text)
    text = re.sub(r"~~(.+?)~~", r"<s>\1</s>", text, flags=re.S)
    text = re.sub(
        r"\[([^\]\n]+)\]\((https?://[^)\s]+)\)",
        lambda m: f'<a href="{m.group(2).replace(chr(34), "%22")}">{m.group(1)}</a>',
        text,
    )

    # restore stashed code (loop: tables may contain stash markers of inline code)
    for _ in range(3):
        text = re.sub(r"\x00(\d+)\x00", lambda m: stash[int(m.group(1))], text)
    return text.strip()


def _quote(m) -> str:
    body = "\n".join(re.sub(r"^&gt;\s?", "", ln) for ln in m.group(0).rstrip("\n").split("\n"))
    return f"<blockquote>{body}</blockquote>\n"


def strip_html_hint(text: str) -> str:
    """Escape text for use inside HTML messages."""
    return html.escape(text or "", quote=False)
