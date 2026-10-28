"""Agent entry point: picks the engine, loads and saves chat history, sends the reply.

AGENT_ENGINE=strands (default) -> src/agent_strands.py  (Strands Agents SDK on Bedrock)
AGENT_ENGINE=bedrock           -> src/agent_bedrock.py  (raw Bedrock Converse loop, fallback)
If the Strands package cannot be imported, the Bedrock engine is used instead.
"""
import logging

from src.config import Config
from src.memory import get_session_history, save_session_history
from src.prompt import FALLBACK_REPLY
from src.email_sender import send_email

logger = logging.getLogger(__name__)

def _engine():
    """Return the module whose run(customer_email, history, text) answers one message."""
    if Config.AGENT_ENGINE == "strands":
        try:
            from src import agent_strands
            return agent_strands
        except ImportError:
            logger.exception("Strands is not installed - falling back to the Bedrock engine")
    from src import agent_bedrock
    return agent_bedrock

def save_conversation(customer_email: str, user_text: str, assistant_text: str) -> None:
    """Persist the completed turn. Called by the worker AFTER the reply has been sent.

    Saving after the send means a transient SES failure does not cause a duplicate
    turn in the history on SQS retry.
    """
    history = get_session_history(customer_email)
    turns = [{"role": m["role"], "text": m["content"][0]["text"]} for m in history]
    turns += [{"role": "user", "text": user_text}, {"role": "assistant", "text": assistant_text}]
    save_session_history(customer_email, turns)

def run_agent_loop(customer_email: str, user_text: str, send_reply: bool = True, save_history: bool = True) -> str:
    """Handle one inbound customer message and return the reply text.

    Exceptions from the engine (Bedrock throttling, network) are NOT swallowed: they
    propagate to the caller. The worker lets them raise so SQS retries and (after
    maxReceiveCount) the message reaches the DLQ.
    """
    history = get_session_history(customer_email)
    final_text = _engine().run(customer_email, list(history), user_text)

    if not final_text:
        final_text = FALLBACK_REPLY

    if save_history:
        turns = [{"role": m["role"], "text": m["content"][0]["text"]} for m in history]
        turns += [{"role": "user", "text": user_text}, {"role": "assistant", "text": final_text}]
        save_session_history(customer_email, turns)

    if send_reply:
        send_email(customer_email, "Re: Your property enquiry", final_text)

    return final_text
