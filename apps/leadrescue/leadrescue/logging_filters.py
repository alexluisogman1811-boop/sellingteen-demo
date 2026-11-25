import logging
import re

_INVITE_RE = re.compile(r"(/(?:join|reset)/)[^\s?\"']+")
_QUERY_RE = re.compile(r"(\?)[^\s\"']+")


def redact(text: str) -> str:
    """Remove invite and password-reset tokens and query strings (customer search terms, names,
    phone numbers in filters) from a log line."""
    text = _INVITE_RE.sub(r"\1[REDACTED]", text)
    return _QUERY_RE.sub(r"\1[REDACTED]", text)


class RedactSensitiveFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        try:
            message = record.getMessage()
        except Exception:
            return True
        cleaned = redact(message)
        if cleaned != message:
            record.msg = cleaned
            record.args = ()
        return True
