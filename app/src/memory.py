"""DynamoDB persistence: chat sessions, lead profiles, inbound-email de-duplication, rate limits.

Every customer is identified by the sender's e-mail address, lower-cased (see customer_key).
The partition-key attribute name comes from configuration (Config.DYNAMODB_KEY_ATTR /
DYNAMODB_LEADS_KEY_ATTR), so the code adapts to the AWS team's table definitions.

When LOCAL_MODE=1 everything lives in process memory so the agent can be tested
from a laptop with only Aurora + Bedrock access.
"""
import hashlib
import time
from decimal import Decimal
from email.utils import parseaddr

import boto3
from botocore.exceptions import ClientError

from src.config import Config

SESSION_TTL_SECONDS = 30 * 24 * 3600
DEDUPE_TTL_SECONDS = 24 * 3600

# Rows for de-duplication and rate counters share the sessions table with the chat history, so a
# customer identity may never start with one of these (otherwise a crafted sender address such as
# "dedupe#<id>@host" could collide with a bookkeeping row).
_RESERVED_PREFIXES = ("dedupe#", "rate#")
_MAX_MESSAGE_ID_BYTES = 512

_local_sessions: dict = {}
_local_leads: dict = {}
_local_seen: set = set()
_local_rate: dict = {}

_dynamodb = None

def _ddb():
    global _dynamodb
    if _dynamodb is None:
        _dynamodb = boto3.resource("dynamodb", region_name=Config.AWS_REGION)
    return _dynamodb

# --------------------------------------------------------------------- identity
def customer_key(customer) -> str:
    """Canonical customer identity: the bare, lower-cased e-mail address.

    'Omar Ali <Omar@Example.COM>' and 'omar@example.com' are the same customer.
    Raises ValueError for an empty or reserved identity.
    """
    raw = str(customer or "").strip()
    if "@" in raw:
        raw = parseaddr(raw)[1] or raw
    key = raw.strip().lower()
    if not key:
        raise ValueError("customer identity is empty")
    if key.startswith(_RESERVED_PREFIXES):
        raise ValueError("customer identity uses a reserved prefix")
    return key

def message_key(message_id) -> str:
    """Canonical e-mail Message-ID: '<abc@mail.example.com>' -> 'abc@mail.example.com'.

    Very long ids are replaced by their SHA-256 so they always fit a DynamoDB key.
    """
    mid = str(message_id or "").strip().strip("<>").strip()
    if not mid:
        raise ValueError("message id is empty")
    if len(mid.encode("utf-8")) > _MAX_MESSAGE_ID_BYTES:
        mid = "sha256:" + hashlib.sha256(mid.encode("utf-8")).hexdigest()
    return mid

# --------------------------------------------------------------------- sessions
def get_session_history(customer_email: str) -> list:
    """Return prior turns as Bedrock Converse messages (text only)."""
    key = customer_key(customer_email)
    if Config.LOCAL_MODE:
        stored = _local_sessions.get(key, [])
    else:
        item = _ddb().Table(Config.DYNAMODB_SESSION_TABLE).get_item(
            Key={Config.session_key_attr(): key}
        ).get("Item", {})
        stored = item.get("messages", [])
    return [{"role": m["role"], "content": [{"text": m["text"]}]} for m in stored]

def save_session_history(customer_email: str, turns: list) -> None:
    """Persist plain-text turns [{'role','text'}], trimmed to the last N messages.

    Only text is stored (not tool calls) so trimming can never orphan a
    toolUse/toolResult pair, which Bedrock would reject.
    """
    key = customer_key(customer_email)
    turns = turns[-Config.HISTORY_MESSAGES:]
    while turns and turns[0]["role"] != "user":  # Converse requires a user-first history
        turns = turns[1:]

    if Config.LOCAL_MODE:
        _local_sessions[key] = turns
        return
    _ddb().Table(Config.DYNAMODB_SESSION_TABLE).put_item(
        Item={
            Config.session_key_attr(): key,
            "messages": turns,
            "updated_at": int(time.time()),
            "expires_at": int(time.time()) + SESSION_TTL_SECONDS,
        }
    )

def reset_session(customer_email: str) -> None:
    key = customer_key(customer_email)
    _local_sessions.pop(key, None)
    if not Config.LOCAL_MODE:
        _ddb().Table(Config.DYNAMODB_SESSION_TABLE).delete_item(
            Key={Config.session_key_attr(): key}
        )

# ------------------------------------------------------------------------ leads
def save_lead_qualification(customer_email: str, **fields) -> dict:
    """Merge non-null qualification fields into the lead record; return the full lead."""
    key = customer_key(customer_email)
    clean = {k: v for k, v in fields.items() if v not in (None, "")}
    for k, v in list(clean.items()):
        if isinstance(v, float):
            clean[k] = Decimal(str(v))  # DynamoDB rejects Python floats

    if Config.LOCAL_MODE:
        _local_leads.setdefault(key, {}).update(clean)
        return dict(_local_leads[key])

    names, values, sets = {"#u": "updated_at"}, {":u": int(time.time())}, ["#u = :u"]
    for i, (k, v) in enumerate(clean.items()):
        names[f"#k{i}"] = k
        values[f":v{i}"] = v
        sets.append(f"#k{i} = :v{i}")
    resp = _ddb().Table(Config.DYNAMODB_LEADS_TABLE).update_item(
        Key={Config.leads_key_attr(): key},
        UpdateExpression="SET " + ", ".join(sets),
        ExpressionAttributeNames=names,
        ExpressionAttributeValues=values,
        ReturnValues="ALL_NEW",
    )
    return resp.get("Attributes", {})

def get_lead(customer_email: str) -> dict:
    key = customer_key(customer_email)
    if Config.LOCAL_MODE:
        return dict(_local_leads.get(key, {}))
    return _ddb().Table(Config.DYNAMODB_LEADS_TABLE).get_item(
        Key={Config.leads_key_attr(): key}
    ).get("Item", {})

# ------------------------------------------------------------------ de-duplication
def release_message(message_id: str) -> None:
    """Undo claim_message (used when enqueueing failed, so a redelivery of the same email is not skipped)."""
    mid = message_key(message_id)
    if Config.LOCAL_MODE:
        _local_seen.discard(mid)
        return
    _ddb().Table(Config.DYNAMODB_SESSION_TABLE).delete_item(
        Key={Config.session_key_attr(): f"dedupe#{mid}"}
    )

def claim_message(message_id: str) -> bool:
    """True the first time an email Message-ID is seen (SES may invoke Lambda A more than once)."""
    mid = message_key(message_id)
    if Config.LOCAL_MODE:
        if mid in _local_seen:
            return False
        _local_seen.add(mid)
        return True
    attr = Config.session_key_attr()
    try:
        _ddb().Table(Config.DYNAMODB_SESSION_TABLE).put_item(
            Item={
                attr: f"dedupe#{mid}",
                "expires_at": int(time.time()) + DEDUPE_TTL_SECONDS,
            },
            ConditionExpression="attribute_not_exists(#k)",
            ExpressionAttributeNames={"#k": attr},
        )
        return True
    except ClientError as exc:
        if exc.response["Error"]["Code"] == "ConditionalCheckFailedException":
            return False
        raise

# ----------------------------------------------------------------- rate limiting
def _count(key: str, ttl: int) -> int:
    """Atomically add 1 to a counter and return the new value."""
    if Config.LOCAL_MODE:
        _local_rate[key] = _local_rate.get(key, 0) + 1
        return _local_rate[key]
    resp = _ddb().Table(Config.DYNAMODB_SESSION_TABLE).update_item(
        Key={Config.session_key_attr(): key},
        UpdateExpression="ADD #n :one SET expires_at = if_not_exists(expires_at, :exp)",
        ExpressionAttributeNames={"#n": "n"},
        ExpressionAttributeValues={":one": 1, ":exp": int(time.time()) + ttl},
        ReturnValues="UPDATED_NEW",
    )
    return int(resp["Attributes"]["n"])

def register_message(customer_email: str) -> str:
    """Count one inbound email for this customer in an hourly and a daily window.

    Returns "ok", "notify" (the first message over a limit: tell the customer once) or
    "drop" (further messages over the limit: ignore silently).
    """
    key = customer_key(customer_email)
    verdict = "ok"
    for seconds, limit in ((3600, Config.RATE_LIMIT_PER_HOUR), (86400, Config.RATE_LIMIT_PER_DAY)):
        count = _count(f"rate#{key}#{seconds}#{int(time.time()) // seconds}", seconds * 2)
        if count > limit + 1:
            verdict = "drop"
        elif count == limit + 1 and verdict == "ok":
            verdict = "notify"
    return verdict
