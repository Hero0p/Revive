"""The LLM's one job: writing the message body.

It does not decide who to contact, when, on what channel, or whether to
contact them at all. Rules decide that. This file turns a rule's
message_intent into a sentence, and anything it returns that fails validation
is thrown away in favour of the hand-written template.

Groq, openai/gpt-oss-20b at low reasoning effort. The model is small and fast on
purpose: the job is rephrasing eight known intents under hard constraints, and
the 2-second budget is only reachable with a fast model. Measured on this
prompt, all eight intents come back in 0.4-0.9s. The system is fully functional
with this file removed.
"""

import json

from app.config import (
    GROQ_API_KEY,
    LLM_MODEL,
    LLM_REASONING_EFFORT,
    LLM_TIMEOUT_SECONDS,
)
from app.messages import MessageContext
from app.models import Action, Case, Customer

REQUIRED_KEYS = {"body", "channel_fit", "mentions_urgency"}
MAX_BODY_CHARS = 480

# Chaos toggle, flipped by POST /api/chaos.
chaos_llm_down = False

SYSTEM_PROMPT = """You write short payment-recovery messages for an Indian \
e-commerce merchant. A customer's payment just failed and the merchant wants \
them to be able to finish the purchase.

Hard rules. Breaking any of these makes the message unusable:
1. Never create urgency. No countdowns, no expiry, no "act now", no warning \
that the order will be cancelled. Urgency is the most reliable phishing marker \
and it is what makes real messages look fake.
2. Never state an amount other than the exact order amount you are given.
3. Never ask for any information, and never use the words OTP, CVV, PIN or \
"card number" at all -- not even to describe what went wrong. A real merchant \
does not put those words in a message, so they read as phishing wherever they \
appear. Write "the bank verification step" instead of naming the OTP.
4. Always include the merchant-only details you are given (order ID, item \
names, time of the attempt) and the link, unchanged.
5. If mention_reason is false, do not state or hint at why the payment failed. \
It is embarrassing and it costs the sale.
6. If suggests_alt_method is true, the point of the message is that retrying \
the same card cannot work. Say so plainly and name UPI or another card.

Write in the requested language: en is English, hi is Hindi, hinglish is \
conversational Hindi written in Latin script. This is an email: one or two \
short sentences, no subject line -- the subject is set separately.

Reply with JSON only, no markdown fence, matching exactly:
{"body": string, "channel_fit": "email", \
"mentions_urgency": boolean, "rationale": string}

rationale is one short sentence for the merchant's audit log explaining the \
tone you chose. mentions_urgency must honestly report whether your own body \
contains urgency language."""


def write_message_llm(
    case: Case,
    action: Action,
    customer: Customer | None,
    ctx: MessageContext,
    mention_reason: bool = True,
) -> tuple[str, str, str] | None:
    """Returns (body, rationale, model) or None to fall back to the template."""
    if chaos_llm_down or not GROQ_API_KEY:
        return None

    try:
        from groq import Groq
    except ImportError:
        return None

    payload = {
        "root_cause": case.root_cause,
        "message_intent": action.message_intent,
        "amount": ctx.amount_display,
        "items": ctx.items_display,
        "order_id": ctx.order_id,
        "customer_first_name": ctx.customer_name,
        "attempt_time": ctx.attempt_time,
        "card_last4": ctx.card_last4,
        "merchant": ctx.merchant,
        "link": ctx.resume_url,
        "channel": action.channel,
        "language": (customer.language if customer else "en") or "en",
        "suggests_alt_method": bool(action.suggests_alt_method),
        "mention_reason": bool(mention_reason),
    }

    try:
        client = Groq(
            api_key=GROQ_API_KEY,
            timeout=LLM_TIMEOUT_SECONDS,
            max_retries=0,  # the budget is the budget
        )
        extra = {}
        if LLM_REASONING_EFFORT:
            # gpt-oss models think before answering. Left unbounded they spend
            # the whole token budget reasoning and return an empty body, which
            # fails JSON mode outright. "low" answers in well under a second.
            extra["reasoning_effort"] = LLM_REASONING_EFFORT

        response = client.chat.completions.create(
            model=LLM_MODEL,
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": json.dumps(payload, indent=2)},
            ],
            response_format={"type": "json_object"},
            temperature=0.4,
            max_tokens=500,
            **extra,
        )
    except Exception:  # noqa: BLE001 -- timeout, network, auth, rate limit: all fall back
        return None

    text = (response.choices[0].message.content or "") if response.choices else ""
    parsed = _parse(text)
    if parsed is None:
        return None

    return parsed["body"], parsed.get("rationale", ""), LLM_MODEL


def _parse(text: str) -> dict | None:
    """Strict: anything unexpected means we use the template instead."""
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.split("```")[1] if "```" in cleaned[3:] else cleaned[3:]
        cleaned = cleaned.removeprefix("json").strip()

    try:
        data = json.loads(cleaned)
    except (ValueError, TypeError):
        return None

    if not isinstance(data, dict) or not REQUIRED_KEYS.issubset(data):
        return None
    if not isinstance(data["body"], str) or not data["body"].strip():
        return None
    if len(data["body"]) > MAX_BODY_CHARS:
        return None
    if data["mentions_urgency"] is not False:
        return None  # it told us itself
    if data["channel_fit"] != "email":
        return None
    return data
