"""Agent entry point: picks the engine, loads and saves chat history, sends the reply.

AGENT_ENGINE=strands (default) -> src/agent_strands.py  (Strands Agents SDK on Bedrock)
AGENT_ENGINE=bedrock           -> src/agent_bedrock.py  (raw Bedrock Converse loop, fallback)
If the Strands package cannot be imported, the Bedrock engine is used instead.
"""
import logging
import re

from src.config import Config
from src.memory import get_session_history, save_session_history
from src.prompt import FALLBACK_REPLY
from src.email_sender import send_email

logger = logging.getLogger(__name__)

_ARABIC_RE = re.compile(r"[\u0600-\u06FF\u0750-\u077F\u08A0-\u08FF]")
_LATIN_RE = re.compile(r"[A-Za-z]")


def _language_directive(text: str) -> str:
    """Return an explicit, per-message reply-language instruction.

    The system prompt asks the model to match the customer's language, but across
    a multi-turn history the model tends to stick with the language the thread
    started in. Appending a deterministic directive to the CURRENT message - based
    on its own script - makes the model switch when the customer switches.
    Returns "" when the language is ambiguous (let the model decide).
    """
    ar = len(_ARABIC_RE.findall(text or ""))
    latin = len(_LATIN_RE.findall(text or ""))
    if ar > latin and ar > 0:
        return ("\n\n[Reply in Arabic. The customer wrote this message in Arabic; "
                "answer only in Arabic regardless of earlier messages.]")
    if latin > ar and latin > 0:
        return ("\n\n[Reply in English. The customer wrote this message in English; "
                "answer only in English regardless of earlier messages.]")
    return ""

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
    # Append a deterministic reply-language directive to the message the model
    # sees (not to what we store), so the agent follows the CURRENT message's
    # language even when earlier turns were in a different language.
    model_text = user_text + _language_directive(user_text)
    final_text = _engine().run(customer_email, list(history), model_text)

    if not final_text:
        final_text = FALLBACK_REPLY

    if save_history:
        turns = [{"role": m["role"], "text": m["content"][0]["text"]} for m in history]
        turns += [{"role": "user", "text": user_text}, {"role": "assistant", "text": final_text}]
        save_session_history(customer_email, turns)

    if send_reply:
        send_email(customer_email, "Re: Your property enquiry", final_text)

    return final_text
