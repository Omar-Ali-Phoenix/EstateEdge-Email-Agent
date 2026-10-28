"""Central configuration.

Resolution order for every value:
  1. real environment variables (Lambda env vars / shell)
  2. a local `.env` file (development only)
  3. the AWS Secrets Manager secret named in APP_SECRET_ARN (production)
"""
import json
import logging
import os
import re

from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger(__name__)

_ATTR_NAME = re.compile(r"[A-Za-z0-9_.\-]{1,255}")

def _overlay_secret() -> None:
    arn = os.getenv("APP_SECRET_ARN")
    if not arn:
        return
    import boto3
    client = boto3.client("secretsmanager", region_name=os.getenv("AWS_REGION"))
    payload = json.loads(client.get_secret_value(SecretId=arn)["SecretString"])
    for key, value in payload.items():
        os.environ.setdefault(key, str(value))

_overlay_secret()

def _int(name: str) -> int:
    val = os.getenv(name)
    if val is None:
        return None
    try:
        return int(val)
    except ValueError:
        return None

def _bool(name: str, default: bool = True) -> bool:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


class Config:
    AWS_REGION = os.getenv("AWS_REGION")
    BEDROCK_MODEL_ID = os.getenv("BEDROCK_MODEL_ID")

    DB_MODE = (os.getenv("DB_MODE") or "data_api").lower()

    DATA_API_CLUSTER_ARN = os.getenv("DATA_API_CLUSTER_ARN")
    DATA_API_SECRET_ARN = os.getenv("DATA_API_SECRET_ARN")
    DATA_API_ROLE_ARN = os.getenv("DATA_API_ROLE_ARN") or None
    DATA_API_EXTERNAL_ID = os.getenv("DATA_API_EXTERNAL_ID") or None

    DB_HOST = os.getenv("DB_HOST")
    DB_PORT = _int("DB_PORT")
    DB_NAME = os.getenv("DB_NAME")
    DB_TABLE = os.getenv("DB_TABLE")
    DB_USER = os.getenv("DB_USER")
    DB_PASSWORD = os.getenv("DB_PASSWORD")
    DB_SSL_CA = os.getenv("DB_SSL_CA") or None

    # Branding and behaviour (set by deployment.config through template.yaml)
    BRAND_NAME = os.getenv("BRAND_NAME") or "Property Assistant"
    COMPANY_NAME = os.getenv("COMPANY_NAME") or ""
    COMPANY_URL = os.getenv("COMPANY_URL") or ""
    CURRENCY_CODE = os.getenv("CURRENCY_CODE") or ""  # required by deploy.py; no built-in default
    LANGUAGE_HINT = os.getenv("LANGUAGE_HINT") or "whatever language the customer writes in, exactly as they write"

    # Customer-facing fallback texts (override with REPLY_* in deployment.config)
    REPLY_TECHNICAL_ERROR = os.getenv("REPLY_TECHNICAL_ERROR") or (
        "Sorry, I ran into a technical issue. Please try again in a moment."
    )
    REPLY_RATE_LIMITED = os.getenv("REPLY_RATE_LIMITED") or (
        "You are sending messages very quickly. Please wait a while and try again."
    )
    REPLY_TOO_LONG = os.getenv("REPLY_TOO_LONG") or (
        "Your message is too long. Please send a shorter one."
    )

    # Web channel
    WEB_ALLOWED_ORIGIN = os.getenv("WEB_ALLOWED_ORIGIN") or ""  # blank = same-origin, no CORS header
    WEB_REQUIRE_VERIFIED_RECIPIENT = _bool("WEB_REQUIRE_VERIFIED_RECIPIENT", default=True)

    # Email channel (SES in and out, raw inbound mail stored in S3)
    SES_SENDER = os.getenv("SES_SENDER")  # the From address, e.g. agent@<their domain>
    SALES_TEAM_EMAIL = os.getenv("SALES_TEAM_EMAIL")  # sales hand-off recipient
    INBOUND_BUCKET = os.getenv("INBOUND_BUCKET")  # bucket the receipt rule writes raw emails to
    INBOUND_PREFIX = os.getenv("INBOUND_PREFIX") or ""  # receipt-rule key prefix, used literally
    MAX_EMAIL_BYTES = _int("MAX_EMAIL_BYTES") or 10 * 1024 * 1024  # raw emails larger than this are not parsed
    MAX_EMAIL_BODY_CHARS = _int("MAX_EMAIL_BODY_CHARS") or 4000  # new text kept from one email
    SES_TIMEOUT_S = 10
    SES_SENDER_NAME = os.getenv("SES_SENDER_NAME") or None
    SES_CONFIGURATION_SET = os.getenv("SES_CONFIGURATION_SET") or None
    SES_API = (os.getenv("SES_API") or "sesv2").lower()

    # DynamoDB
    DYNAMODB_SESSION_TABLE = os.getenv("DYNAMODB_SESSION_TABLE")
    DYNAMODB_LEADS_TABLE = os.getenv("DYNAMODB_LEADS_TABLE")
    DYNAMODB_KEY_ATTR = os.getenv("DYNAMODB_KEY_ATTR") or "customer_email"
    DYNAMODB_LEADS_KEY_ATTR = os.getenv("DYNAMODB_LEADS_KEY_ATTR") or DYNAMODB_KEY_ATTR

    QUEUE_URL = os.getenv("QUEUE_URL")  # SQS FIFO queue, set by template.yaml
    LOCAL_MODE = os.getenv("LOCAL_MODE", "0") == "1"

    AGENT_ENGINE = (os.getenv("AGENT_ENGINE") or "strands").lower()
    MAX_ROWS = 5
    MAX_TOOL_ROUNDS = 6
    # Time budget per message. No NEW model call starts after MESSAGE_BUDGET_SECONDS.
    MESSAGE_BUDGET_SECONDS = 45
    MODEL_READ_TIMEOUT_S = 60
    MODEL_TOTAL_ATTEMPTS = 2  # TOTAL tries per Bedrock call (botocore "total_max_attempts"), not retries
    DATA_API_READ_TIMEOUT_S = 50
    HISTORY_MESSAGES = 20  # last 10 turns

    # Cost protection
    MAX_MESSAGE_CHARS = _int("MAX_MESSAGE_CHARS") or 4000  # matches MAX_EMAIL_BODY_CHARS
    RATE_LIMIT_PER_HOUR = _int("RATE_LIMIT_PER_HOUR") or 40
    RATE_LIMIT_PER_DAY = _int("RATE_LIMIT_PER_DAY") or 150

    @classmethod
    def require(cls, *names: str) -> None:
        missing = [n for n in names if not getattr(cls, n, None)]
        if missing:
            raise RuntimeError(f"Missing required configuration: {', '.join(missing)}")

    @classmethod
    def _key_attr(cls, name: str) -> str:
        value = getattr(cls, name)
        if not _ATTR_NAME.fullmatch(value or ""):
            raise RuntimeError(f"{name} may only contain letters, digits, '_', '.' and '-' (max 255 characters)")
        return value

    @classmethod
    def session_key_attr(cls) -> str:
        return cls._key_attr("DYNAMODB_KEY_ATTR")

    @classmethod
    def leads_key_attr(cls) -> str:
        return cls._key_attr("DYNAMODB_LEADS_KEY_ATTR")

    @classmethod
    def ses_api(cls) -> str:
        if cls.SES_API not in ("sesv2", "ses"):
            raise RuntimeError("SES_API must be 'sesv2' or 'ses'")
        return cls.SES_API

    @classmethod
    def inbound_key(cls, message_id: str) -> str:
        message_id = (message_id or "").strip()
        if not message_id or "/" in message_id:
            raise ValueError("invalid SES message id")
        return f"{cls.INBOUND_PREFIX}{message_id}"

    @classmethod
    def table(cls) -> str:
        cls.require("DB_TABLE")
        if not re.fullmatch(r"[A-Za-z0-9_]+", cls.DB_TABLE):
            raise RuntimeError("DB_TABLE may only contain letters, digits and underscores")
        return cls.DB_TABLE

def bedrock_client_config():
    from botocore.config import Config as BotoConfig
    return BotoConfig(
        connect_timeout=5,
        read_timeout=Config.MODEL_READ_TIMEOUT_S,
        retries={"total_max_attempts": Config.MODEL_TOTAL_ATTEMPTS, "mode": "standard"},
    )

def data_api_client_config():
    from botocore.config import Config as BotoConfig
    return BotoConfig(
        connect_timeout=5,
        read_timeout=Config.DATA_API_READ_TIMEOUT_S,
        retries={"total_max_attempts": 1, "mode": "standard"},
    )

def ses_client_config():
    from botocore.config import Config as BotoConfig
    return BotoConfig(
        connect_timeout=5,
        read_timeout=Config.SES_TIMEOUT_S,
        retries={"total_max_attempts": 1, "mode": "standard"},
    )
