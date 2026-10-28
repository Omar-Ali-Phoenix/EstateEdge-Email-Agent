"""Fallback engine: raw Amazon Bedrock Converse API tool loop (no Strands needed).

Used only when AGENT_ENGINE=bedrock, or automatically if the Strands package cannot be imported.
"""
import logging
import time

import boto3

from src.config import Config, bedrock_client_config
from src.logutil import mask_email
from src.prompt import build_system_prompt
from src.tools import (
    tool_execute_property_search_sql,
    tool_save_lead_qualification,
    tool_trigger_sales_handoff,
)

logger = logging.getLogger(__name__)

_bedrock = None

def _client():
    global _bedrock
    if _bedrock is None:
        _bedrock = boto3.client(
            "bedrock-runtime",
            region_name=Config.AWS_REGION,
            config=bedrock_client_config(),
        )
    return _bedrock

def _tool_spec(name, description, properties, required=None):
    schema = {"type": "object", "properties": properties}
    if required:
        schema["required"] = required
    return {"toolSpec": {"name": name, "description": description, "inputSchema": {"json": schema}}}

TOOLS_SPEC = [
    _tool_spec(
        "execute_property_search_sql",
        "Run ONE read-only MySQL SELECT on the property listings table. Returns at most 5 rows.",
        {"sql_query": {"type": "string", "description": "A single MySQL SELECT statement."}},
        ["sql_query"],
    ),
    _tool_spec(
        "save_lead_qualification",
        "Save customer preferences extracted from the chat. Include only fields the customer actually stated.",
        {
            "budget": {"type": "number", "description": "Maximum budget in the configured currency"},
            "city": {"type": "string"},
            "district": {"type": "string"},
            "bedrooms": {"type": "integer"},
            "unit_type": {"type": "string", "description": "Apartment, Villa, Duplex, ..."},
            "purpose": {"type": "string", "enum": ["sale", "rent"]},
            "timeline": {"type": "string", "description": "When they want to buy/move, e.g. 'within 3 months'"},
            "name": {"type": "string"},
            "notes": {"type": "string"},
        },
    ),
    _tool_spec(
        "trigger_sales_handoff",
        "Notify the human sales team by email. Use when the customer asks for a viewing/agent or is fully qualified.",
        {
            "summary": {"type": "string", "description": "What the customer wants, shortlisted listing IDs, and next step."},
            "reason": {"type": "string", "enum": ["viewing_request", "qualified", "wants_agent"]},
        },
        ["summary"],
    ),
]

def _run_tool(name: str, args: dict, customer_email: str) -> dict:
    """Run a tool. Exceptions inside a tool are turned into a toolResult error so the model can retry."""
    try:
        if name == "execute_property_search_sql":
            return tool_execute_property_search_sql(args.get("sql_query", ""))
        if name == "save_lead_qualification":
            return tool_save_lead_qualification(customer_email, **args)
        if name == "trigger_sales_handoff":
            return tool_trigger_sales_handoff(customer_email, args.get("summary", ""), args.get("reason", ""))
        return {"status": "error", "message": f"Unknown tool {name}"}
    except Exception as exc:
        logger.exception("Tool %s failed", name)
        return {"status": "error", "message": f"Tool failed: {exc}"}

def _text_of(message: dict) -> str:
    return "".join(c["text"] for c in message.get("content", []) if "text" in c).strip()

def run(customer_email: str, history: list, user_text: str) -> str:
    """Run the tool loop for one message.

    Returns "" when the per-message time budget is exhausted (soft fail). Any other
    exception from Bedrock is re-raised so the worker lets SQS retry the message.
    """
    messages = history + [{"role": "user", "content": [{"text": user_text}]}]
    system = [{"text": build_system_prompt()}]
    deadline = time.monotonic() + Config.MESSAGE_BUDGET_SECONDS

    for _ in range(Config.MAX_TOOL_ROUNDS):
        if time.monotonic() > deadline:
            logger.warning("Time budget exhausted for %s", mask_email(customer_email))
            return ""
        resp = _client().converse(
            modelId=Config.BEDROCK_MODEL_ID,
            messages=messages,
            system=system,
            toolConfig={"tools": TOOLS_SPEC},
            inferenceConfig={"maxTokens": 1024, "temperature": 0.2},
        )
        out = resp["output"]["message"]
        messages.append(out)

        if resp.get("stopReason") != "tool_use":
            return _text_of(out)

        results = []
        for block in out["content"]:
            if "toolUse" not in block:
                continue
            call = block["toolUse"]
            logger.info("Tool call %s", call["name"])
            result = _run_tool(call["name"], call["input"], customer_email)
            results.append(
                {
                    "toolResult": {
                        "toolUseId": call["toolUseId"],
                        "content": [{"json": result}],
                        "status": "error" if result.get("status") == "error" else "success",
                    }
                }
            )
        messages.append({"role": "user", "content": results})
    return ""
