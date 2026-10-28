"""Web inquiry handler (synchronous).

Backs the web form (served by CloudFront; /api forwards to this Lambda via
a Function URL). Invoked with a JSON body:

    { "email": "...", "name": "...", "question": "...", "company": "" }

Flow:
  1. Validate input + honeypot.
  2. Rate-limit per email (reuses the DynamoDB counters in src.memory).
  3. Optional recipient guard: while the SES account is in the sandbox, only
     VERIFIED addresses can receive a reply, so if the submitted email is not
     a verified SES identity we return 403 {reason: unverified_recipient}
     and do NOT run the agent. Switch this off with
     WEB_REQUIRE_VERIFIED_RECIPIENT=false after SES production access.
  4. Run the existing agent (Bedrock + Aurora) with customer_email = form email.
  5. Email the answer to the form email via SES, AND return it in the response
     so the page can display it synchronously.

CORS: the /api route is same-origin behind CloudFront, so no CORS header is
needed. When WEB_ALLOWED_ORIGIN is set (custom web domain) it is echoed back;
when blank, no Access-Control-Allow-Origin header is sent.
"""
import json
import logging

import boto3
from botocore.exceptions import ClientError

from src.agent import run_agent_loop, save_conversation
from src.config import Config
from src.email_parser import normalize_address
from src.email_sender import EmailSendError, send_email
from src.logutil import mask_email
from src.memory import register_message

logger = logging.getLogger()
logger.setLevel(logging.INFO)

ALLOWED_ORIGIN = Config.WEB_ALLOWED_ORIGIN or ""
MAX_QUESTION_CHARS = 2000

_ses_ident = None

def _cors_headers():
    headers = {
        "Access-Control-Allow-Methods": "POST, OPTIONS",
        "Access-Control-Allow-Headers": "Content-Type",
        "Content-Type": "application/json",
    }
    if ALLOWED_ORIGIN:
        headers["Access-Control-Allow-Origin"] = ALLOWED_ORIGIN
    return headers

def _resp(status, body):
    return {"statusCode": status, "headers": _cors_headers(), "body": json.dumps(body)}

def _ses_identity_client():
    global _ses_ident
    if _ses_ident is None:
        _ses_ident = boto3.client("sesv2", region_name=Config.AWS_REGION)
    return _ses_ident

def _is_verified_recipient(email: str) -> bool:
    """True only if `email` is a verified SES identity in this account."""
    try:
        resp = _ses_identity_client().get_email_identity(EmailIdentity=email)
        return bool(resp.get("VerifiedForSendingStatus"))
    except ClientError as exc:
        if exc.response["Error"]["Code"] in ("NotFoundException", "BadRequestException"):
            return False
        logger.warning("SES identity check failed for %s: %s", mask_email(email), exc)
        return False

def _parse_body(event) -> dict:
    raw = event.get("body") or "{}"
    if event.get("isBase64Encoded"):
        import base64
        raw = base64.b64decode(raw).decode("utf-8", "replace")
    try:
        return json.loads(raw)
    except (ValueError, TypeError):
        return {}

def lambda_handler(event, context):
    method = (
        event.get("requestContext", {}).get("http", {}).get("method")
        or event.get("httpMethod")
    )
    if method == "OPTIONS":
        return {"statusCode": 204, "headers": _cors_headers(), "body": ""}

    data = _parse_body(event)

    # Honeypot: real users never fill 'company'. Pretend success, do nothing.
    if (data.get("company") or "").strip():
        logger.info("Honeypot triggered; ignoring submission")
        return _resp(200, {"status": "sent"})

    email = normalize_address(data.get("email"))
    question = (data.get("question") or "").strip()
    name = (data.get("name") or "").strip()[:80]

    if not email:
        return _resp(400, {"status": "error", "message": "A valid email address is required."})
    if not question:
        return _resp(400, {"status": "error", "message": "Please describe what you're looking for."})
    if len(question) > MAX_QUESTION_CHARS:
        return _resp(400, {"status": "error", "message": "Your question is too long. Please shorten it."})

    # Rate limit per email (hourly + daily windows, shared DynamoDB counters).
    verdict = register_message(email)
    if verdict in ("drop", "notify"):
        logger.warning("Rate limit hit for %s (%s)", mask_email(email), verdict)
        return _resp(429, {
            "status": "error",
            "message": "You've sent several requests recently. Please wait a little and try again.",
        })

    # Optional recipient guard (SES sandbox). Off after production access.
    if Config.WEB_REQUIRE_VERIFIED_RECIPIENT and not _is_verified_recipient(email):
        logger.info("Unverified recipient %s; not running agent", mask_email(email))
        return _resp(403, {
            "status": "error",
            "reason": "unverified_recipient",
            "message": "This preview can only reply to pre-approved email addresses.",
        })

    # Prefix the question with the name so the agent can greet them.
    user_text = question if not name else f"(From {name})\n{question}"

    # Run the agent synchronously.
    try:
        reply = run_agent_loop(email, user_text, send_reply=False, save_history=False)
    except Exception:
        logger.exception("Agent run failed for %s", mask_email(email))
        return _resp(502, {
            "status": "error",
            "message": "Our assistant is busy right now. Please try again in a moment.",
        })

    if not reply:
        reply = ("Sorry, I couldn't put together an answer just now. "
                 "Please try rephrasing your request.")

    # Send the answer by email, then save the turn.
    subject = f"Your {Config.BRAND_NAME} property enquiry"
    try:
        send_email(email, subject, reply)
    except EmailSendError as exc:
        logger.error("SES send failed for %s: %s", mask_email(email), exc)
        # The page can still show the answer even if the email bounced.
        return _resp(200, {
            "status": "sent",
            "answer": reply,
            "note": "Shown here; email delivery could not be confirmed.",
        })

    try:
        save_conversation(email, user_text, reply)
    except Exception:
        logger.warning("History save failed for %s", mask_email(email), exc_info=True)

    logger.info("Web inquiry answered and emailed to %s", mask_email(email))
    return _resp(200, {"status": "sent", "answer": reply})
