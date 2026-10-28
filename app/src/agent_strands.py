"""Main engine: Strands Agents SDK on Amazon Bedrock.

The three tools are the same plain functions the Bedrock fallback uses (src/tools.py).
They are wrapped per message so the customer's email is supplied by the code,
never chosen by the model.
"""
import json
import logging
import time
from typing import Literal

from strands import Agent, tool
from strands.hooks import BeforeModelCallEvent
from strands.models import BedrockModel
from strands.tools.executors import SequentialToolExecutor

from src.config import Config, bedrock_client_config
from src.logutil import mask_email
from src.prompt import build_system_prompt
from src.tools import (
    tool_execute_property_search_sql,
    tool_save_lead_qualification,
    tool_trigger_sales_handoff,
)

logger = logging.getLogger(__name__)

_model = None

class BudgetExceeded(Exception):
    """Raised when one message uses too many model rounds or too much time."""

def _get_model() -> BedrockModel:
    global _model
    if _model is None:
        _model = BedrockModel(
            model_id=Config.BEDROCK_MODEL_ID,
            region_name=Config.AWS_REGION,
            temperature=0.2,
            max_tokens=1024,
            streaming=False,
            boto_client_config=bedrock_client_config(),
        )
    return _model

def _json(result: dict) -> str:
    return json.dumps(result, ensure_ascii=False, default=str)

def build_tools(customer_email: str) -> list:
    """Create the three tools bound to this customer's email address."""

    @tool
    def execute_property_search_sql(sql_query: str) -> str:
        """Run ONE read-only MySQL SELECT on the property listings table. Returns at most 5 rows.

        Args:
            sql_query: A single MySQL SELECT statement.
        """
        return _json(tool_execute_property_search_sql(sql_query))

    @tool
    def save_lead_qualification(
        budget: float | None = None,
        city: str | None = None,
        district: str | None = None,
        bedrooms: int | None = None,
        unit_type: str | None = None,
        purpose: Literal["sale", "rent"] | None = None,
        timeline: str | None = None,
        name: str | None = None,
        notes: str | None = None,
    ) -> str:
        """Save customer preferences extracted from the chat. Include only fields the customer actually stated.

        Args:
            budget: Maximum budget in the configured currency.
            city: City the customer wants.
            district: District or compound.
            bedrooms: Number of bedrooms.
            unit_type: Apartment, Villa, Duplex, ...
            purpose: Either "sale" or "rent".
            timeline: When they want to buy or move, e.g. "within 3 months".
            name: The customer's name.
            notes: Anything else useful for the sales team.
        """
        fields = {
            k: v for k, v in dict(
                budget=budget, city=city, district=district, bedrooms=bedrooms, unit_type=unit_type,
                purpose=purpose, timeline=timeline, name=name, notes=notes,
            ).items() if v is not None
        }
        return _json(tool_save_lead_qualification(customer_email, **fields))

    @tool
    def trigger_sales_handoff(
        summary: str, reason: Literal["viewing_request", "qualified", "wants_agent"] | None = None
    ) -> str:
        """Notify the human sales team by email. Use when the customer asks for a viewing or an agent, or is fully qualified.

        Args:
            summary: What the customer wants, shortlisted listing IDs, and the next step.
            reason: Why the sales team is being notified.
        """
        return _json(tool_trigger_sales_handoff(customer_email, summary, reason or ""))

    return [execute_property_search_sql, save_lead_qualification, trigger_sales_handoff]

def _limit_budget(agent: Agent) -> None:
    """Stop the loop after MAX_TOOL_ROUNDS model calls or MESSAGE_BUDGET_SECONDS."""
    state = {"rounds": 0, "deadline": time.monotonic() + Config.MESSAGE_BUDGET_SECONDS}

    def check(event: BeforeModelCallEvent) -> None:
        state["rounds"] += 1
        if state["rounds"] > Config.MAX_TOOL_ROUNDS or time.monotonic() > state["deadline"]:
            raise BudgetExceeded(f"after {state['rounds'] - 1} model rounds")

    agent.add_hook(check, BeforeModelCallEvent)

def _is_budget_error(exc: BaseException) -> bool:
    seen = set()
    while exc is not None and id(exc) not in seen:
        if isinstance(exc, BudgetExceeded):
            return True
        seen.add(id(exc))
        exc = exc.__cause__ or exc.__context__
    return False

def run(customer_email: str, history: list, user_text: str) -> str:
    """Run the Strands agent for one message.

    Returns "" when the per-message budget is exhausted (a soft fail: send a fallback
    reply and do not retry). Any other exception (Bedrock throttling, network) is
    re-raised so the worker lets SQS retry the message.
    """
    try:
        agent = Agent(
            model=_get_model(),
            system_prompt=build_system_prompt(),
            messages=history,
            tools=build_tools(customer_email),
            tool_executor=SequentialToolExecutor(),
            callback_handler=None,
        )
        _limit_budget(agent)
        return str(agent(user_text)).strip()
    except Exception as exc:  # noqa: BLE001
        # Strands wraps exceptions raised inside hooks (EventLoopException), so look through the chain.
        if _is_budget_error(exc):
            logger.warning("Strands agent budget exhausted for %s", mask_email(customer_email))
            return ""
        raise
