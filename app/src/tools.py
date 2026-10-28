"""Tools exposed to the Bedrock agent."""
import json
import logging
from datetime import date, datetime, timezone
from decimal import Decimal

from src.config import Config
from src.db import run_query
from src.logutil import mask_email
from src.memory import get_lead, save_lead_qualification
from src.sql_guard import SQLGuardException, sanitize_and_enforce_guardrails
from src.email_sender import send_email

logger = logging.getLogger(__name__)

MAX_CELL_CHARS = 200  # keep Bedrock token usage predictable

def _jsonable(value):
    if isinstance(value, Decimal):
        return int(value) if value == value.to_integral_value() else float(value)
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, bytes):
        return value.decode("utf-8", "replace")
    if isinstance(value, str) and len(value) > MAX_CELL_CHARS:
        return value[:MAX_CELL_CHARS] + "…"
    return value

# --------------------------------------------------------------- tool 1: SQL search
def tool_execute_property_search_sql(sql_query: str) -> dict:
    """Run a guarded, read-only SELECT against Aurora MySQL (max 5 rows)."""
    try:
        guarded = sanitize_and_enforce_guardrails(sql_query, Config.table())
    except SQLGuardException as exc:
        logger.warning("SQL rejected: %s | %s", exc, sql_query)
        return {"status": "error", "message": f"Query rejected: {exc} Rewrite it as a single SELECT on the listings table."}

    logger.info("Executing SQL: %s", guarded)
    try:
        rows = [{k: _jsonable(v) for k, v in r.items()} for r in run_query(guarded)]
    except Exception as exc:  # noqa: BLE001
        logger.exception("Database error")
        return {"status": "error", "message": f"Database error: {exc}. Check column names and fix the query."}

    return {"status": "success", "count": len(rows), "rows": rows}

# ------------------------------------------------------- tool 2: lead qualification
PURPOSES = ("sale", "rent")
HANDOFF_REASONS = ("viewing_request", "qualified", "wants_agent")

def tool_save_lead_qualification(customer_email: str, **fields) -> dict:
    purpose = fields.get("purpose")
    if purpose is not None:  # enforced here for both engines (Strands also validates the schema)
        fields["purpose"] = str(purpose).strip().lower()
        if fields["purpose"] not in PURPOSES:
            return {"status": "error", "message": "purpose must be either 'sale' or 'rent'. Call the tool again with a valid value."}
    allowed = {"budget", "city", "district", "bedrooms", "unit_type", "purpose", "timeline", "name", "notes"}
    data = {k: v for k, v in fields.items() if k in allowed}
    lead = save_lead_qualification(customer_email, **data)
    return {"status": "success", "message": "Lead profile updated.", "profile": json.loads(json.dumps(lead, default=_jsonable))}

# ------------------------------------------------------------ tool 3: sales handoff
def _handoff_text(customer_email: str, summary: str, reason: str, lead: dict) -> tuple[str, str]:
    """Return (summary line, profile line) for the sales team, one line each (template-safe)."""
    profile = " | ".join(
        f"{k}: {_jsonable(v)}"
        for k, v in lead.items()
        if k not in ("phone_number", "customer_email", Config.leads_key_attr(), "updated_at") and not k.startswith("handoff_")
    )
    summary_line = f"{summary} (trigger: {reason})" if reason else summary
    return summary_line, profile or "no details saved yet"

def _notify_sales(customer_email: str, summary_line: str, profile: str) -> None:
    """Send the lead to the sales team via SES email."""
    to_email = Config.SALES_TEAM_EMAIL
    if not to_email:
        logger.warning("SALES_TEAM_EMAIL is not configured. Handoff skipped.")
        return

    subject = f"New Qualified Lead - {customer_email}"
    message = (
        "NEW QUALIFIED LEAD\n\n"
        f"Customer Email: {customer_email}\n"
        f"Summary: {summary_line}\n\n"
        f"Profile:\n{profile.replace(' | ', chr(10))}\n\n"
        "Please contact the customer directly."
    )
    send_email(to_email, subject, message, is_handoff=True)

def tool_trigger_sales_handoff(customer_email: str, summary: str, reason: str = "") -> dict:
    """Notify the sales team once per customer. A failed send keeps the lead as 'pending'."""
    if not Config.SALES_TEAM_EMAIL and not Config.LOCAL_MODE:
        return {"status": "error", "message": "Sales team email is not configured."}

    reason = reason if reason in HANDOFF_REASONS else ""  # free text from the model never reaches the sales team
    lead = get_lead(customer_email)
    if lead.get("handoff_status") == "sent" or lead.get("handoff_at"):
        return {"status": "success", "message": "The sales team was already notified. Do not notify again; tell the customer a consultant will contact them shortly."}

    summary_line, profile = _handoff_text(customer_email, summary, reason, lead)
    try:
        _notify_sales(customer_email, summary_line, profile)
    except Exception:  # noqa: BLE001
        # Keep the lead: it stays in the leads table with handoff_status=pending.
        logger.exception("HANDOFF_FAILED for %s", mask_email(customer_email))
        save_lead_qualification(customer_email, handoff_status="pending", handoff_summary=summary_line[:900])
        return {
            "status": "success",
            "message": "The notification could not be delivered right now, but the lead is saved and the team will follow up. Tell the customer a consultant will contact them soon.",
        }

    save_lead_qualification(
        customer_email, handoff_status="sent", handoff_summary=summary_line[:900],
        handoff_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
    )
    return {"status": "success", "message": "Sales team notified. Tell the customer a consultant will contact them shortly."}
