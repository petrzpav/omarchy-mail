"""Turn a parsed message into readable text, quotes and attachment lists."""

import re
from email.message import EmailMessage

import html2text


# Escape sequences (CSI, OSC, DCS…) and other control characters, which a sender could use to
# drive the terminal: colours, the window title, the clipboard (OSC 52)… Tabs and newlines stay.
_CONTROL = re.compile(r"\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)?"           # OSC … BEL / ST
                      r"|\x1b[P^_X][^\x1b]*(?:\x1b\\)?"                  # DCS, PM, APC, SOS … ST
                      r"|\x1b\[[0-?]*[ -/]*[@-~]"                         # CSI
                      r"|\x1b.?"                                         # any other escape
                      r"|[\x00-\x08\x0b-\x1f\x7f-\x9f]")                   # C0 and C1 controls


def clean(s: str) -> str:
    """Text from a sender, safe to put on the terminal."""
    return _CONTROL.sub("", s)


def body_text(msg: EmailMessage) -> str:
    return body(msg)[0]


def body(msg: EmailMessage) -> tuple[str, bool]:
    """Readable body and whether it is Markdown (converted from HTML)."""
    plain = html = None
    try:
        part = msg.get_body(preferencelist=("plain",))
        plain = part.get_content() if part else None
    except Exception:
        plain = None
    if not plain or not plain.strip():
        try:
            part = msg.get_body(preferencelist=("html",))
            html = part.get_content() if part else None
        except Exception:
            html = None
    if html:
        conv = html2text.HTML2Text()
        conv.body_width = 0
        conv.ignore_images = True
        conv.ignore_emphasis = False
        conv.protect_links = True
        text = conv.handle(html)
    else:
        text = plain or ""
    text = clean(text.replace("\r\n", "\n"))
    text = re.sub(r"(?m)^[ \t\xa0]+$", "", text)   # whitespace-only lines count as blank
    is_md = bool(html) or looks_like_markdown(text)
    if is_md:
        text = strip_css(text)
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    return text, is_md


def strip_css(text: str) -> str:
    """Drop leaked <style> contents and the indentation that Markdown would turn into code blocks."""
    text = re.sub(r"(?s)/\*.*?\*/", "", text)
    text = re.sub(r"(?s)@media[^{]*\{(?:[^{}]*\{[^{}]*\})*[^{}]*\}", "", text)
    text = re.sub(r"(?m)^[ \t]*[.#@\w][\w\s.#:>,*()\[\]=\"'-]*\{[^{}]*\}[ \t]*$", "", text)
    text = re.sub(r"(?m)^[ \t]*[.#@\w][^\n{}]{0,120}\{\s*\n(?:[^{}\n]*\n)*?[ \t]*\}[ \t]*$", "", text)
    return re.sub(r"(?m)^[ \t]{4,}", "", text)


def looks_like_markdown(text: str) -> bool:
    """Some senders put Markdown in the plain part ([label](url), # headings, **bold**)."""
    hits = (len(re.findall(r"\[[^\]\n]+\]\(https?://", text))
            + len(re.findall(r"(?m)^#{1,4} \S", text))
            + len(re.findall(r"\*\*[^*\n]+\*\*", text)))
    return hits >= 2


# Where the history of a reply starts, in the languages this mailbox sees.
_QUOTE_START = re.compile(
    r"(?im)^(?:"
    r"On [^\n]{3,160}(?:\n[^\n]{0,120})?wrote:\s*$"            # Gmail/Apple, English (may wrap)
    r"|Dne [^\n]{3,160}(?:\n[^\n]{0,120})?napsal(?:\(a\)|a)?:?\s*$"  # Czech Seznam/Outlook
    r"|[^\n]{0,40}odesílatel [^\n]{3,160}(?:\n[^\n]{0,120})?napsal(?:\(a\)|a)?:\s*$"  # Czech Gmail
    r"|Am .{3,120}schrieb.*:\s*$"                              # German
    r"|-{2,}\s*(?:Original Message|Původní zpráva|Forwarded message|Přeposlaná zpráva)\s*-{2,}"
    r"|_{10,}\s*$"                                             # Outlook separator
    r"|(?:\*\*)?(?:From|Od):(?:\*\*)? .+\n(?:.*\n){0,3}?(?:\*\*)?(?:Sent|Date|Odesláno|Datum):"  # Outlook header block
    r")")


_LINK = re.compile(r"(?P<url>(?:https?://|www\.)[^\s<>\"')\]]+[^\s<>\"'),.;:!?\])])"
                   r"|(?P<mail>(?:mailto:)?[\w.+-]+@[\w-]+(?:\.[\w-]+)+)")
LINK_STYLE = "underline bright_blue"


def linkify(s: str):
    """Plain text with URLs and email addresses coloured and clickable (terminal hyperlinks)."""
    from rich.style import Style
    from rich.text import Text
    t = Text(s)
    base = Style.parse(LINK_STYLE)
    for m in _LINK.finditer(s):
        target = m.group(0)
        if m.group("url"):
            href = target if target.startswith("http") else "https://" + target
        else:
            href = target if target.startswith("mailto:") else "mailto:" + target
        t.stylize(base + Style(link=href), m.start(), m.end())
    return t


class Links:
    """A renderable whose terminal hyperlinks can be walked with Tab: remembers the links
    (and the line each starts on) from its last render and highlights the active one."""

    def __init__(self, renderable):
        self.renderable, self.active = renderable, None
        self.links: list[tuple[str, int]] = []   # (href, line)

    def __rich_measure__(self, console, options):
        from rich.measure import Measurement
        return Measurement.get(console, options, self.renderable)

    def __rich_console__(self, console, options):
        from rich.style import Style
        from rich.segment import Segment
        links, line, last, gap = [], 0, None, True
        mark = Style(reverse=True)
        for seg in console.render(self.renderable, options):
            href = seg.style.link if seg.style and not seg.control else None
            if href:
                # One link may span several segments and lines; a new one starts after other text.
                if gap or href != last:
                    links.append((href, line))
                last, gap = href, False
                if self.active == len(links) - 1:
                    seg = Segment(seg.text, seg.style + mark)
            elif seg.text.strip():
                gap = True
            line += seg.text.count("\n")
            yield seg
        self.links = links


def reflow(text: str) -> str:
    """Undo the sender's hard wrapping (~72-78 cols) so paragraphs fit our own column.
    Lists, quotes, signatures and short lines keep their line breaks."""
    out = []
    for para in re.split(r"\n\s*\n", text):
        lines = para.split("\n")
        longest = max((len(l) for l in lines), default=0)
        if longest < 60 or len(lines) < 2:
            out.append(para)
            continue
        merged = [lines[0]]
        for line in lines[1:]:
            prev = merged[-1]
            joinable = (len(prev) >= longest - 18 and line.strip()
                        and not re.match(r"\s*([-*•>]|\d+[.)])\s", line))
            if joinable:
                merged[-1] = prev.rstrip() + " " + line.lstrip()
            else:
                merged.append(line)
        out.append("\n".join(merged))
    return "\n\n".join(out)


def split_quoted(text: str) -> tuple[str, str]:
    """(what this message says, the quoted history below it)."""
    m = _QUOTE_START.search(text)
    if m and m.start() > 0:
        return text[:m.start()].rstrip(), text[m.start():].strip()
    lines = text.splitlines()
    end = len(lines)
    while end and (not lines[end - 1].strip() or lines[end - 1].lstrip().startswith(">")):
        end -= 1
    if end < len(lines) and any(l.lstrip().startswith(">") for l in lines[end:]) and end > 0:
        return "\n".join(lines[:end]).rstrip(), "\n".join(lines[end:]).strip()
    return text, ""


def attachments(msg: EmailMessage) -> list[EmailMessage]:
    try:
        return [p for p in msg.iter_attachments() if p.get_filename()]
    except Exception:
        return []


FORWARD_HEAD = "---------- Forwarded message ---------"   # what Gmail writes above a forward


def quote(text: str) -> str:
    return "\n".join("> " + line if line else ">" for line in text.splitlines())


def formataddr(pair) -> str:
    """Human-readable "Name <addr>" (email.utils.formataddr would RFC 2047-encode the name)."""
    name, addr = pair
    if not name or name == addr:
        return addr
    if any(ch in name for ch in ',;:<>@"()[]\\'):
        name = '"' + name.replace("\\", "\\\\").replace('"', '\\"') + '"'
    return f"{name} <{addr}>"
