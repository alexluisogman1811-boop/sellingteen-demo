"""Which language the customer reply is written in.

The shared host lets the owner pick Thai with the cookie `st_lang` ("th"); the host translates the
page text itself, but the reply QuoteFlow writes is ours to write. Anything other than Thai - and any
call outside a web request (scripts, tests, background jobs) - is English.
"""
from flask import has_request_context, request

LANG_COOKIE = "st_lang"


def address_th(name):
    """'คุณ' + name: a space before a name in Latin letters ("คุณ Sarah"), joined for Thai ("คุณสมชาย")."""
    name = (name or "").strip()
    if not name:
        return ""
    return "คุณ" + name if "฀" <= name[0] <= "๿" else "คุณ " + name


def reply_language():
    """'th' when the owner chose Thai on the host, otherwise 'en'. Safe outside a request."""
    return "th" if has_request_context() and request.cookies.get(LANG_COOKIE) == "th" else "en"
