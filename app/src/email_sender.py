"""Send plain-text emails through Amazon SES (replies to customers, sales hand-off)."""
import logging
import re
from email import policy
from email.headerregistry import Address
from email.message import EmailMessage
from email.utils import formatdate

from botocore.exceptions import BotoCoreError, ClientError

from src.config import Config, ses_client_config
from src.email_parser import is_system_address, normalize_address
from src.logutil import mask_email

logger = logging.getLogger(__name__)

MAX_REPLY_CHARS = 20_000
MAX_REFERENCES = 10
DEFAULT_SUBJECT = "Your property enquiry"

# Errors a retry cannot fix (identity, permission, sandbox or account problems). Anything else is transient.
_PERMANENT_CODES = {
    "MessageRejected", "MailFromDomainNotVerifiedException", "AccountSuspendedException", "SendingPausedException",
    "AccountSendingPausedException", "ConfigurationSetDoesNotExist", "ConfigurationSetSendingPausedException",
    "NotFoundException", "BadRequestException", "AccessDenied", "AccessDeniedException", "UnauthorizedOperation",
    "InvalidParameterValue", "ValidationError", "ValidationException",
}

_local_outbox: list = []
_ses = None

class EmailSendError(RuntimeError):
    def __init__(self, message: str, code: str = "", permanent: bool = False):
        super().__init__(message)
        self.code = code
        self.permanent = permanent

def to_plain_text(text: str) -> str:
    text = str(text or "").replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(r"\*\*(.+?)\*\*", r"\1", text)
    text = re.sub(r"__(.+?)__", r"\1", text)
    text = re.sub(r"^[ \t]*#{1,6}[ \t]*(.+?)[ \t]*#*[ \t]*$", r"\1", text, flags=re.M)
    text = re.sub(r"^([ \t]*)[*•][ \t]+", r"\1- ", text, flags=re.M)
    text = text.replace("`", "")
    text = "\n".join(line.rstrip() for line in text.split("\n"))
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    return text[:MAX_REPLY_CHARS]

def _one_line(value: str, limit: int = 200) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[\x00-\x1f\x7f]", " ", str(value or ""))).strip()[:limit]

_REPLY_PREFIX = re.compile(r"^\s*(re|رد|الرد)\s*:\s*", re.I)

def reply_subject(subject: str) -> str:
    subject = _one_line(subject)
    while _REPLY_PREFIX.match(subject):
        subject = _REPLY_PREFIX.sub("", subject, count=1)
    return f"Re: {subject or DEFAULT_SUBJECT}"[:200]

def _bracket(message_id: str) -> str:
    inner = str(message_id or "").strip().strip("<>").strip()
    return f"<{inner}>" if inner and not re.search(r"[\s<>]", inner) else ""

# ------------------------------------------------------------------------ building
def build_message(to: str, subject: str, body: str, in_reply_to: str = "", references=(), is_handoff: bool = False) -> EmailMessage:
    Config.require("SES_SENDER")
    sender = normalize_address(Config.SES_SENDER)
    if not sender:
        raise RuntimeError("SES_SENDER is not a valid email address")
    recipient = normalize_address(to)
    if not recipient:
        raise EmailSendError("recipient is not a valid email address", permanent=True)
    if recipient == sender:
        raise EmailSendError("refusing to send to our own address (reply loop)", permanent=True)
    if is_system_address(recipient):
        raise EmailSendError("refusing to send to a system address (no-reply, mailer-daemon ...)", permanent=True)
    text = to_plain_text(body)
    if not text:
        raise EmailSendError("empty message body", permanent=True)

    msg = EmailMessage(policy=policy.SMTP)
    local, domain = sender.rsplit("@", 1)
    msg["From"] = Address(display_name=_one_line(Config.SES_SENDER_NAME or "", 80), username=local, domain=domain)
    msg["To"] = recipient
    msg["Subject"] = _one_line(subject) or DEFAULT_SUBJECT
    msg["Date"] = formatdate(usegmt=True)
    parent = _bracket(in_reply_to)
    if parent:
        refs = [r for r in (_bracket(x) for x in references) if r and r != parent][-(MAX_REFERENCES - 1):] + [parent]
        msg["In-Reply-To"] = parent
        msg["References"] = " ".join(refs)
    msg["Auto-Submitted"] = "auto-generated" if is_handoff else "auto-replied"
    msg["X-Auto-Response-Suppress"] = "All"
    msg.set_content(text, subtype="plain", charset="utf-8", cte="quoted-printable")
    return msg

# -------------------------------------------------------------------------- sending
def _client():
    global _ses
    if _ses is None:
        import boto3

        _ses = boto3.client(Config.ses_api(), region_name=Config.AWS_REGION, config=ses_client_config())
    return _ses

def _hint(code: str) -> str:
    if code in ("AccessDenied", "AccessDeniedException", "UnauthorizedOperation"):
        return " (the worker role may lack ses:SendEmail / ses:SendRawEmail, or SES_API does not match the permission granted)"
    if code == "MessageRejected":
        return " (the sender or recipient identity is not verified, or the SES account is still in the sandbox)"
    return ""

def send_email(to: str, subject: str, body: str, in_reply_to: str = "", references=(), client=None, is_handoff: bool = False):
    msg = build_message(to, subject, body, in_reply_to, references, is_handoff)
    recipient = msg["To"]
    if Config.LOCAL_MODE:
        text = msg.get_content()
        _local_outbox.append({"to": str(recipient), "subject": str(msg["Subject"]), "body": text.strip(), "headers": dict(msg.items())})
        del _local_outbox[:-50]
        print(f"\n[Email -> {recipient}] Subject: {msg['Subject']}\n{text}\n")
        return None

    raw = msg.as_bytes()
    sender = normalize_address(Config.SES_SENDER)
    api = Config.ses_api()
    ses = client or _client()
    try:
        if api == "sesv2":
            kwargs = {"FromEmailAddress": sender, "Destination": {"ToAddresses": [str(recipient)]}, "Content": {"Raw": {"Data": raw}}}
            if Config.SES_CONFIGURATION_SET:
                kwargs["ConfigurationSetName"] = Config.SES_CONFIGURATION_SET
            resp = ses.send_email(**kwargs)
        else:
            kwargs = {"Source": sender, "Destinations": [str(recipient)], "RawMessage": {"Data": raw}}
            if Config.SES_CONFIGURATION_SET:
                kwargs["ConfigurationSetName"] = Config.SES_CONFIGURATION_SET
            resp = ses.send_raw_email(**kwargs)
    except ClientError as exc:
        code = exc.response.get("Error", {}).get("Code", "")
        logger.error("SES_SEND_FAILED to=%s code=%s", mask_email(recipient), code)
        raise EmailSendError(f"SES rejected the send ({code}){_hint(code)}", code=code, permanent=code in _PERMANENT_CODES) from exc
    except BotoCoreError as exc:
        logger.error("SES_SEND_FAILED to=%s error=%s", mask_email(recipient), type(exc).__name__)
        raise EmailSendError(f"could not reach SES ({type(exc).__name__})", permanent=False) from exc
    message_id = resp.get("MessageId")
    logger.info("SES_SENT to=%s id=%s", mask_email(recipient), message_id)
    return message_id

def send_reply(parsed, body: str, client=None):
    """Answer a ParsedEmail: to its From address, 'Re:' subject, threaded onto the customer's message."""
    if parsed.is_automated:
        raise EmailSendError(f"refusing to answer an automated message ({parsed.auto_reason})", permanent=True)
    return send_email(
        parsed.sender, reply_subject(parsed.subject), body,
        in_reply_to=parsed.message_id, references=list(parsed.references), client=client,
        is_handoff=False,
    )
