"""Pre-written follow-up wording for the free, offline template provider.

Every sentence below only uses the facts in DraftContext. No sentence states
a price, date, appointment, guarantee or policy. Sentences that ask the customer
a question are used instead of statements whenever a fact is unknown.
"""
from .context import (
    STALENESS_RECENT,
    STALENESS_STALE,
    STALENESS_UNKNOWN,
    STALENESS_WEEK,
    DraftContext,
)

# status -> staleness -> core message. "{s}" is the fixed service phrase.
# "*" is the fallback for any staleness not listed.
CORE = {
    "new": {
        "*": "Thanks for reaching out about {s}. When would be a good time to talk about what you need?",
    },
    "contacted": {
        STALENESS_RECENT: "Thanks for talking with us about {s}. Would you like to set up a time for us to take a look?",
        STALENESS_WEEK: "I wanted to check back in about {s}. Are you still interested in having us take a look?",
        STALENESS_STALE: "It's been a while since we were last in touch about {s}. Are you still interested in having us take a look?",
        "*": "I wanted to follow up about {s}. Would you like to set up a time for us to take a look?",
    },
    "inspection_scheduled": {
        "*": "Just checking in about the inspection we have planned for {s}. If anything has changed or you have "
             "any questions, just let me know.",
    },
    "estimate_sent": {
        STALENESS_RECENT: "I wanted to make sure you received our estimate for {s}. Do you have any questions I can answer?",
        STALENESS_STALE: "It's been a while since we were last in touch about the estimate for {s}. Are you still "
                         "considering the project? If your plans have changed, that's no problem. Just let me know.",
        "*": "I'm following up on the estimate we sent for {s}. Do you have any questions, or is there anything "
             "you'd like us to go over again?",
    },
    "negotiating": {
        STALENESS_STALE: "Checking back in on our conversation about {s}. Is there anything else you need from us "
                         "to help you decide?",
        "*": "I wanted to follow up on our conversation about {s}. Is there anything else you need from us to "
             "help you decide?",
    },
    "won": {
        "*": "Thank you for choosing {us} for {s}. If you have any questions, just reply to this message.",
    },
    "lost": {
        "*": "Thanks again for considering {us} for {s}. If anything changes down the road, we'd be glad to help.",
    },
}

SUBJECTS = {
    "won": "Thank you",
    "lost": "Thanks for considering us",
}

GENERIC_CORE = "I wanted to follow up about {s}. Is there anything I can help with?"


def _intro(ctx: DraftContext) -> str:
    if ctx.sender_name and ctx.business_name:
        return f"This is {ctx.sender_name} with {ctx.business_name}."
    if ctx.business_name:
        return f"This is {ctx.business_name}."
    if ctx.sender_name:
        return f"This is {ctx.sender_name}."
    return ""


def render(ctx: DraftContext) -> tuple[str, str]:
    """Return (subject, body). Subject is empty for text messages."""
    by_status = CORE.get(ctx.status)
    if by_status is None:
        core = GENERIC_CORE
    else:
        core = by_status.get(ctx.staleness) or by_status["*"]
    us = ctx.business_name or "us"
    core = core.format(s=ctx.service.phrase, us=us)

    greeting = f"Hi {ctx.greeting_name}," if ctx.greeting_name else "Hi there,"
    intro = _intro(ctx)

    if ctx.channel == "email":
        lines = [greeting, "", " ".join(p for p in (intro, core) if p), "", "Thanks,"]
        sign = [p for p in (ctx.sender_name, ctx.business_name) if p]
        lines.extend(sign or ["Your roofing team"])
        subject = SUBJECTS.get(ctx.status) or f"Following up about {ctx.service.phrase}"
        return subject[0].upper() + subject[1:], "\n".join(lines)

    # Text messages run on from the greeting: "Hi Maria, this is Sam..."
    after_greeting = intro or core
    if not after_greeting.startswith(("I ", "I'")):
        after_greeting = after_greeting[0].lower() + after_greeting[1:]
    rest = [after_greeting, core] if intro else [after_greeting]
    body = " ".join([greeting] + rest)
    return "", body


# Exposed for tests: every combination the template provider can produce.
ALL_STALENESS = (STALENESS_RECENT, STALENESS_WEEK, STALENESS_STALE, STALENESS_UNKNOWN)
