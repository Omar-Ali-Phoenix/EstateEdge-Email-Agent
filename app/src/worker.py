"""Worker Lambda (Lambda B): triggered by SQS FIFO, runs the agent for one inbound email.

History is saved only AFTER a successful send, so a transient SES failure does not
duplicate the turn on SQS retry. Transient exceptions (S3, Bedrock, SES) propagate
to the caller so SQS retries and eventually the DLQ receives the message.
"""
import json
import logging
import time

import boto3

from src.agent import FALLBACK_REPLY, run_agent_loop, save_conversation
from src.config import Config
from src.email_parser import EmailParseError, ParsedEmail, parse_email
from src.email_sender import EmailSendError, send_reply
from src.logutil import mask_email
from src.memory import register_message

logger = logging.getLogger()
logger.setLevel(logging.INFO)
for _noisy in ("strands", "botocore", "urllib3"):
    logging.getLogger(_noisy).setLevel(logging.WARNING)

_s3 = None

def _s3_client():
    global _s3
    if _s3 is None:
        _s3 = boto3.client("s3", region_name=Config.AWS_REGION)
    return _s3

RATE_LIMIT_REPLY = Config.REPLY_RATE_LIMITED
TOO_LONG_REPLY = Config.REPLY_TOO_LONG

SAVE_ATTEMPTS = 3
SAVE_BACKOFF_S = 0.5

def _save_after_send(sender: str, user_text: str, reply_text: str, msg_id: str) -> None:
    """Persist the turn once the customer already has the reply.

    This must NEVER raise: if it did, SQS would redeliver the message and the customer would
    receive a second reply. A few quick retries cover DynamoDB throttling; if they all fail the
    turn is only missing from the history, which is far cheaper than a duplicate email.
    """
    for attempt in range(1, SAVE_ATTEMPTS + 1):
        try:
            save_conversation(sender, user_text, reply_text)
            return
        except Exception:  # noqa: BLE001
            logger.warning("History save failed for %s (attempt %d/%d)", msg_id, attempt, SAVE_ATTEMPTS, exc_info=True)
            if attempt < SAVE_ATTEMPTS:
                time.sleep(SAVE_BACKOFF_S * attempt)
    logger.error("HISTORY_NOT_SAVED for %s after the reply was sent; not retrying to avoid a duplicate reply", msg_id)

def lambda_handler(event, context):
    """SQS event (batch size 1). Raising from here lets SQS retry (up to maxReceiveCount) and then DLQ."""
    for record in event.get("Records", []):
        _handle_message(json.loads(record["body"]))
    return {"status": "done"}

def _handle_message(message: dict) -> None:
    msg_id = message.get("message_id")
    sender = message.get("sender")
    bucket = message.get("bucket")
    key = message.get("key")

    if not all([msg_id, sender, bucket, key]):
        logger.error("Invalid SQS payload: %s", message)
        return

    # 1. Fetch from S3 (transient: raise so SQS retries)
    resp = _s3_client().get_object(Bucket=bucket, Key=key)
    raw_email = resp["Body"].read()

    if len(raw_email) > Config.MAX_EMAIL_BYTES:
        logger.warning("Dropping email %s: size %d > limit %d", msg_id, len(raw_email), Config.MAX_EMAIL_BYTES)
        return

    # 2. Parse (permanent: don't retry)
    try:
        parsed = parse_email(raw_email)
    except EmailParseError as exc:
        logger.warning("Failed to parse email %s: %s", msg_id, exc)
        return

    # 3. Loop protection
    if parsed.is_automated:
        logger.info("Dropping automated email %s from %s (reason: %s)", msg_id, mask_email(sender), parsed.auto_reason)
        return

    # 4. Rate limiting
    verdict = register_message(parsed.sender)
    if verdict == "drop":
        logger.warning("Rate limit exceeded for %s. Dropping.", mask_email(parsed.sender))
        return
    if verdict == "notify":
        logger.warning("Rate limit reached for %s. Notifying.", mask_email(parsed.sender))
        try:
            send_reply(parsed, RATE_LIMIT_REPLY)
        except EmailSendError:
            logger.exception("Could not send rate-limit reply for %s", msg_id)
        return

    # 5. Length check (uses MAX_MESSAGE_CHARS; matches MAX_EMAIL_BODY_CHARS by default)
    if len(parsed.body) > Config.MAX_MESSAGE_CHARS:
        try:
            send_reply(parsed, TOO_LONG_REPLY)
        except EmailSendError:
            logger.exception("Could not send too-long reply for %s", msg_id)
        return

    if not parsed.body:
        return

    # 6. Run agent. Exceptions propagate (Bedrock throttling, network) -> SQS retry.
    reply_text = run_agent_loop(parsed.sender, parsed.body, send_reply=False, save_history=False)

    # 7. Send reply, THEN save history.
    # Saving after the send means a transient SES failure does not duplicate the turn.
    try:
        send_reply(parsed, reply_text)
    except EmailSendError as exc:
        if exc.permanent:
            logger.error("Permanent SES failure for %s: %s", msg_id, exc)
            _save_after_send(parsed.sender, parsed.body, reply_text, msg_id)
            return
        logger.warning("Transient SES failure for %s, retrying.", msg_id)
        raise exc

    _save_after_send(parsed.sender, parsed.body, reply_text, msg_id)
