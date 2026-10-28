"""Lambda A: SES Email Ingest.

Triggered by the SES receipt rule. It screens the message, de-duplicates it on
the email Message-ID header (not the SES envelope id, which changes on retries),
and enqueues a pointer to the raw email (stored in S3) on the SQS FIFO queue.
"""
import hashlib
import json
import logging

import boto3

from src.config import Config
from src.email_parser import automated_reason, normalize_address
from src.logutil import mask_email
from src.memory import claim_message, release_message

logger = logging.getLogger()
logger.setLevel(logging.INFO)

_sqs = None

def _sqs_client():
    global _sqs
    if _sqs is None:
        _sqs = boto3.client("sqs", region_name=Config.AWS_REGION)
    return _sqs

def _hash_group_id(email: str) -> str:
    """SQS FIFO group IDs are limited to 128 chars. Hash the email if it is too long."""
    if len(email) <= 128:
        return email
    return hashlib.sha256(email.encode("utf-8")).hexdigest()

def _dedupe_id(message_id: str) -> str:
    """SQS MessageDeduplicationId is limited to 128 chars. Hash the email Message-ID when needed."""
    mid = (message_id or "").strip().strip("<>").strip()
    if len(mid.encode("utf-8")) > 128:
        mid = "sha256:" + hashlib.sha256(mid.encode("utf-8")).hexdigest()
    return mid or "unknown"

def _message_id_header(mail: dict) -> str:
    """Return the customer's actual Message-ID header (not the SES envelope id)."""
    for h in mail.get("headers", []):
        if h.get("name", "").lower() == "message-id":
            return (h.get("value") or "").strip().strip("<>").strip()
    # Fall back to commonHeaders.messageId (the SES id). Better than nothing.
    return (mail.get("commonHeaders", {}).get("messageId") or mail.get("messageId") or "").strip()

def lambda_handler(event, context):
    for record in event.get("Records", []):
        ses = record.get("ses", {})
        mail = ses.get("mail", {})
        receipt = ses.get("receipt", {})

        ses_message_id = mail.get("messageId")
        email_message_id = _message_id_header(mail)

        from_header = mail.get("commonHeaders", {}).get("from", [])
        source = from_header[0] if from_header else mail.get("source", "")

        spf = receipt.get("spfVerdict", {}).get("status")
        dkim = receipt.get("dkimVerdict", {}).get("status")
        dmarc = receipt.get("dmarcVerdict", {}).get("status")
        spam = receipt.get("spamVerdict", {}).get("status")
        virus = receipt.get("virusVerdict", {}).get("status")

        if spam == "FAIL" or virus == "FAIL" or spf == "FAIL" or dkim == "FAIL" or dmarc == "FAIL":
            logger.warning(
                "Dropping email %s due to failed verdicts (SPF: %s, DKIM: %s, DMARC: %s, Spam: %s, Virus: %s)",
                ses_message_id, spf, dkim, dmarc, spam, virus,
            )
            continue

        sender_email = normalize_address(source)
        if not sender_email:
            logger.warning("Dropping email %s: invalid sender address '%s'", ses_message_id, source)
            continue

        headers = {h.get("name", "").lower(): h.get("value", "") for h in mail.get("headers", [])}
        subject = headers.get("subject", "")

        reason = automated_reason(headers, sender_email, subject, own_address=normalize_address(Config.SES_SENDER))
        if reason:
            logger.info("Dropping automated email %s from %s (reason: %s)", ses_message_id, mask_email(sender_email), reason)
            continue

        # De-duplicate on the customer's actual Message-ID (persistent across SES retries).
        if not claim_message(email_message_id):
            logger.info("Duplicate email %s skipped", email_message_id)
            continue

        try:
            Config.require("QUEUE_URL", "INBOUND_BUCKET")
            s3_key = Config.inbound_key(ses_message_id)

            payload = {
                "message_id": ses_message_id,
                "sender": sender_email,
                "bucket": Config.INBOUND_BUCKET,
                "key": s3_key,
            }

            _sqs_client().send_message(
                QueueUrl=Config.QUEUE_URL,
                MessageBody=json.dumps(payload),
                MessageGroupId=_hash_group_id(sender_email),
                MessageDeduplicationId=_dedupe_id(email_message_id),
            )
            logger.info("Enqueued email %s from %s", ses_message_id, mask_email(sender_email))
        except Exception as exc:
            logger.exception("Failed to enqueue email %s", ses_message_id)
            release_message(email_message_id)
            raise exc
