"""Parse one raw inbound email (MIME bytes read from S3) into what the agent needs.

Pure functions, no AWS calls. Used by Lambda B (full message) and, for the header checks,
by Lambda A (automated_reason works on a plain header dict, which the SES event provides).

What it does:
  * sender: the bare, lower-cased, validated address from From (never Reply-To: see reply_to)
  * subject, Message-ID, In-Reply-To, References
  * body: plain text (HTML is the fallback), quoted reply history removed, capped in length
  * loop protection: recognises auto-replies, bounces, mailing lists and our own mail
"""
import re
from dataclasses import dataclass, field
from email import policy
from email.parser import BytesParser
from email.utils import parseaddr
from html.parser import HTMLParser
from typing import Mapping, Optional

from src.config import Config

MAX_SUBJECT_CHARS = 200
_MAX_TEXT_TO_PROCESS = 200_000       # bound the work done on a huge body before it is cut to the real limit
_RESERVED_PREFIXES = ("dedupe#", "rate#")   # must match memory._RESERVED_PREFIXES (a test enforces this)

class EmailParseError(ValueError):
    """The message cannot be used (no valid sender, unreadable MIME)."""

class EmailTooLarge(EmailParseError):
    """The raw message is bigger than Config.MAX_EMAIL_BYTES."""

@dataclass
class ParsedEmail:
    sender: str                 # bare, lower-cased address from From: the customer identity and the reply address
    sender_name: str            # display name, may be empty
    subject: str
    body: str                   # new text only: quoted history removed, capped at the body limit
    message_id: str             # without angle brackets; may be empty if the header is missing
    in_reply_to: str = ""
    references: list = field(default_factory=list)
    reply_to: str = ""          # informational ONLY: replies never go here
    auto_reason: Optional[str] = None   # None for a human message, else a short reason code
    body_truncated: bool = False
    only_quoted_text: bool = False      # the message held nothing but quoted history; body is that history
    body_source: str = "none"           # "plain", "html" or "none"
    attachments: int = 0                # counted, never read

    @property
    def is_automated(self) -> bool:
        return self.auto_reason is not None

# ------------------------------------------------------------------------ addresses
_ADDR_RE = re.compile(
    r"^[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?)+$"
)

def normalize_address(value) -> Optional[str]:
    """'Omar <Omar@Example.COM>' -> 'omar@example.com'; None when it is not a usable address."""
    text = " ".join(str(value or "").split())
    addr = parseaddr(text)[1] if ("@" in text) else ""
    addr = addr.strip().lower()
    if not addr or len(addr) > 254 or not _ADDR_RE.match(addr):
        return None
    local = addr.rsplit("@", 1)[0]
    if len(local) > 64 or ".." in addr or addr.startswith(_RESERVED_PREFIXES):
        return None
    return addr

_SYSTEM_LOCAL = re.compile(
    r"^(no[-_.]?reply|do[-_.]?not[-_.]?reply|mailer-daemon|postmaster|bounces?|auto[-_.]?reply|autoresponder)([-_.+0-9].*)?$"
)

def is_system_address(address: str) -> bool:
    """no-reply@, mailer-daemon@, postmaster@, bounce@ ...: addresses nobody reads, never to be answered."""
    local = (address or "").rsplit("@", 1)[0].lower()
    return bool(_SYSTEM_LOCAL.match(local))

# ------------------------------------------------------------------- loop protection
_AUTO_SUBJECTS = (
    "automatic reply", "auto reply", "auto-reply", "autoreply", "automatic response", "out of office",
    "out-of-office", "undeliverable", "undelivered mail", "delivery status notification", "mail delivery failed",
    "mail delivery subsystem", "returned mail", "failure notice", "delivery failure",
    "رد تلقائي", "رد آلي", "رد اوتوماتيكي", "خارج المكتب", "خارج مقر العمل",
)

def automated_reason(headers: Mapping, sender: str = "", subject: str = "", own_address: Optional[str] = None) -> Optional[str]:
    """Why this message must NOT be answered (a short code), or None for a normal human message.

    `headers` maps header names to values; names are matched case-insensitively. Lambda A builds it
    from the SES event's mail.headers; Lambda B builds it from the parsed message.
    """
    h = {str(k).lower(): " ".join(str(v or "").split()) for k, v in headers.items()}
    sender = (sender or "").lower()
    own = (own_address or "").lower()

    if own and sender == own:
        return "self"
    auto = h.get("auto-submitted", "").split(";")[0].strip().lower()
    if auto and auto != "no":
        return "auto-submitted"
    if h.get("precedence", "").lower() in ("bulk", "junk", "list", "auto_reply", "auto-reply"):
        return "precedence"
    suppress = h.get("x-auto-response-suppress", "").lower()
    if any(token in suppress for token in ("oof", "autoreply", "all")):
        return "auto-response-suppress"
    ctype = h.get("content-type", "").lower()
    if "multipart/report" in ctype or "delivery-status" in ctype or "disposition-notification" in ctype:
        return "bounce"
    if h.get("return-path", "").replace(" ", "") == "<>":
        return "bounce"
    if sender and is_system_address(sender):
        return "system-sender"
    if h.get("list-id") or h.get("list-unsubscribe"):
        return "mailing-list"
    low_subject = (subject or "").strip().lower()
    if any(low_subject.startswith(prefix) for prefix in _AUTO_SUBJECTS):
        return "auto-subject"
    return None

# ------------------------------------------------------------------ text extraction
class _HtmlText(HTMLParser):
    """HTML -> readable text. Drops script/style/head, and (optionally) quoted history inside <blockquote>."""

    _BLOCK = {"p", "div", "br", "tr", "table", "ul", "ol", "h1", "h2", "h3", "h4", "h5", "h6", "section", "article", "hr"}
    _SKIP = {"script", "style", "head", "title"}

    def __init__(self, skip_quotes: bool):
        super().__init__(convert_charrefs=True)
        self.parts: list = []
        self._skip_depth = 0
        self._quote_depth = 0
        self._skip_quotes = skip_quotes

    def handle_starttag(self, tag, attrs):
        if tag in self._SKIP:
            self._skip_depth += 1
        elif tag == "blockquote":
            self._quote_depth += 1
        elif tag == "li":
            self.parts.append("\n- ")
        elif tag == "td" or tag == "th":
            self.parts.append(" ")
        elif tag in self._BLOCK:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in self._SKIP and self._skip_depth:
            self._skip_depth -= 1
        elif tag == "blockquote" and self._quote_depth:
            self._quote_depth -= 1
        elif tag in self._BLOCK:
            self.parts.append("\n")

    def handle_data(self, data):
        if self._skip_depth or (self._skip_quotes and self._quote_depth):
            return
        self.parts.append(data)

    def text(self) -> str:
        return "".join(self.parts)

def html_to_text(markup: str) -> str:
    for skip_quotes in (True, False):   # an unclosed <blockquote> must not hide the whole message
        parser = _HtmlText(skip_quotes)
        try:
            parser.feed(markup)
            parser.close()
        except Exception:  # noqa: BLE001  (broken markup: use what was read so far)
            pass
        text = _clean(parser.text())
        if text:
            return text
    return ""

_INVISIBLE = dict.fromkeys(
    [0x200B, 0x200C, 0x200D, 0x200E, 0x200F, 0x202A, 0x202B, 0x202C, 0x202D, 0x202E, 0x2060, 0x2066, 0x2067, 0x2068, 0x2069, 0xFEFF, 0x00AD],
    None,
)
_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")

def _clean(text: str) -> str:
    text = text.replace("\r\n", "\n").replace("\r", "\n").replace("\u00a0", " ").translate(_INVISIBLE)
    text = _CONTROL.sub("", text)
    text = "\n".join(line.rstrip() for line in text.split("\n"))
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()

# --------------------------------------------------------------- quoted-reply removal
_RULE_ORIGINAL = re.compile(
    r"^\s*[-_=*]{2,}\s*(original message|forwarded message|reply message|الرسالة الأصلية|رسالة أصلية|الرسالة المعاد توجيهها|الرسالة المحولة)\s*[-_=*]{2,}\s*$",
    re.I,
)
_RULE_UNDERSCORES = re.compile(r"^\s*_{5,}\s*$")
_ON_WROTE_START = re.compile(r"^\s*(on|at)\s+\S", re.I)
_ON_WROTE_END = re.compile(r"\bwrote\s*:\s*$", re.I)
_AR_WROTE_START = re.compile(r"^\s*(في|يوم|بتاريخ)\s+\S")
_AR_WROTE_END = re.compile(r"(كتب|كتبت|كتبوا)[^\n]{0,80}:\s*$")
_HEADER_FROM = re.compile(r"^\s*(from|من)\s*:\s*\S", re.I)
_HEADER_FIELD = re.compile(r"^\s*(sent|date|to|cc|subject|تم الإرسال|التاريخ|إلى|الى|الموضوع)\s*:", re.I)
_SIGNATURE_LINE = re.compile(r"^\s*(sent from my .{2,40}|get outlook for .{2,20}|sent from yahoo mail.*|sent from mail for windows.*|أرسلت من .{2,40}|تم الإرسال من .{2,40})\s*$", re.I)

def _marker_span(lines: list, i: int) -> int:
    """If a reply marker starts at line i, return how many lines it spans (>=1), else 0."""
    line = lines[i]
    if _RULE_ORIGINAL.match(line):
        return 1
    for start, end, wide in ((_ON_WROTE_START, _ON_WROTE_END, 4), (_AR_WROTE_START, _AR_WROTE_END, 4)):
        if start.match(line):
            joined = ""
            for j in range(i, min(i + wide, len(lines))):
                joined = (joined + " " + lines[j]).strip()
                if end.search(joined):
                    return j - i + 1
    # Outlook header block, optionally preceded by an underline row
    probe = i + 1 if _RULE_UNDERSCORES.match(line) else i
    if probe < len(lines) and _HEADER_FROM.match(lines[probe]):
        following = lines[probe + 1: probe + 6]
        if sum(1 for ln in following if _HEADER_FIELD.match(ln)) >= 2:
            return probe - i + 1
    return 0

def strip_quoted_reply(text: str) -> str:
    """Keep what the customer just wrote; drop the quoted history of the conversation.

    Handles '> ' quoting, 'On ... wrote:' (English and Arabic), '-----Original Message-----' and the
    Outlook 'From: / Sent: / To: / Subject:' block. If the reply is written BELOW a '> ' quote
    (bottom posting) that text is kept. Returns '' when nothing but history is left.
    """
    lines = text.split("\n")
    kept: list = []
    i, n = 0, len(lines)
    while i < n:
        line = lines[i]
        span = _marker_span(lines, i)
        if span:
            j = i + span
            saw_quote = False
            while j < n and (lines[j].lstrip().startswith(">") or not lines[j].strip()):
                saw_quote = saw_quote or lines[j].lstrip().startswith(">")
                j += 1
            if saw_quote and not _RULE_ORIGINAL.match(line):
                i = j   # a quoted block that ends: what follows is the customer's own text
                continue
            break       # un-prefixed history (Outlook style): everything after the marker is old
        if line.lstrip().startswith(">"):
            i += 1
            continue
        kept.append(line)
        i += 1
    while kept and (not kept[-1].strip() or _SIGNATURE_LINE.match(kept[-1])):
        kept.pop()
    return _clean("\n".join(kept))

# ---------------------------------------------------------------------- parse_email
def _header(msg, name: str) -> str:
    try:
        value = msg.get(name)
    except Exception:  # noqa: BLE001  (a malformed header must never crash the parser)
        return ""
    return " ".join(str(value or "").split())

def _message_ids(value: str) -> list:
    return [m.strip("<>").strip() for m in re.findall(r"<[^<>\s]+>", value or "") if m.strip("<>").strip()]

def _part_text(part) -> str:
    try:
        return part.get_content()
    except Exception:  # noqa: BLE001  (unknown charset or broken encoding)
        payload = part.get_payload(decode=True) or b""
        try:
            return payload.decode(part.get_content_charset() or "utf-8", errors="replace")
        except LookupError:
            return payload.decode("utf-8", errors="replace")

def _count_attachments(msg) -> int:
    count = 0
    for part in msg.walk():
        if part.is_multipart():
            continue
        if part.get_content_disposition() == "attachment" or (part.get_filename() and part.get_content_maintype() != "text"):
            count += 1
    return count

def _extract_body(msg) -> tuple:
    """(text, source). Plain text wins; HTML is used when there is no plain text or it is blank."""
    plain = msg.get_body(preferencelist=("plain",))
    if plain is not None:
        text = _clean(_part_text(plain))
        if text:
            return text, "plain"
    markup = msg.get_body(preferencelist=("html",))
    if markup is not None:
        text = html_to_text(_part_text(markup))
        if text:
            return text, "html"
    return "", "none"

def _truncate(text: str, limit: int) -> tuple:
    if len(text) <= limit:
        return text, False
    cut = text[:limit]
    space = max(cut.rfind(" "), cut.rfind("\n"))
    if space > limit * 0.8:
        cut = cut[:space]
    return cut.rstrip(), True

def parse_email(raw, max_body_chars: Optional[int] = None) -> ParsedEmail:
    """Parse raw MIME bytes. Raises EmailTooLarge / EmailParseError; everything else is tolerated."""
    if isinstance(raw, str):
        raw = raw.encode("utf-8", errors="replace")
    if not raw:
        raise EmailParseError("empty message")
    if len(raw) > Config.MAX_EMAIL_BYTES:
        raise EmailTooLarge(f"message is {len(raw)} bytes; the limit is {Config.MAX_EMAIL_BYTES}")
    limit = max_body_chars or Config.MAX_EMAIL_BODY_CHARS
    try:
        msg = BytesParser(policy=policy.default).parsebytes(raw)
    except Exception as exc:  # noqa: BLE001
        raise EmailParseError("unreadable message") from exc

    from_value = _header(msg, "From")
    sender = normalize_address(from_value)
    if not sender:
        raise EmailParseError("no usable sender address in From")
    name = parseaddr(from_value)[0].strip()
    subject = re.sub(r"\s+", " ", _CONTROL.sub("", _header(msg, "Subject"))).strip()[:MAX_SUBJECT_CHARS]

    text, source = _extract_body(msg)
    text = text[:_MAX_TEXT_TO_PROCESS]
    stripped = strip_quoted_reply(text)
    only_quoted = False
    if not stripped and text:
        only_quoted = True
        stripped = _clean("\n".join(ln.lstrip().lstrip(">").strip() for ln in text.split("\n")))
    body, truncated = _truncate(stripped, limit)

    header_map = {
        k: _header(msg, k)
        for k in ("Auto-Submitted", "Precedence", "X-Auto-Response-Suppress", "Content-Type", "Return-Path", "List-Id", "List-Unsubscribe")
    }
    reply_to = normalize_address(_header(msg, "Reply-To")) or ""
    return ParsedEmail(
        sender=sender,
        sender_name=name,
        subject=subject,
        body=body,
        message_id=(_message_ids(_header(msg, "Message-ID")) or [""])[0],
        in_reply_to=(_message_ids(_header(msg, "In-Reply-To")) or [""])[0],
        references=_message_ids(_header(msg, "References")),
        reply_to=reply_to,
        auto_reason=automated_reason(header_map, sender, subject, Config.SES_SENDER and normalize_address(Config.SES_SENDER)),
        body_truncated=truncated,
        only_quoted_text=only_quoted,
        body_source=source,
        attachments=_count_attachments(msg),
    )
