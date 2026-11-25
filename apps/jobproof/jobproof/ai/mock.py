"""Deterministic offline report generator (AI_MOCK=1).

It rewrites the user's rough notes into report sections using simple, predictable rules.
It only rearranges and tidies what the user wrote - it never adds facts.
"""
import re

from .guard import HEDGE_RE

SHORTHAND = [
    (r"\bw/o\b", "without"), (r"\bw/", "with "), (r"\bb/c\b", "because"), (r"\bthru\b", "through"),
    (r"\bcust\b", "customer"), (r"\bpls\b", "please"), (r"\bapprox\.?(?=\s)", "approximately"),
    (r"\s&\s", " and "), (r"\bchk'?d\b", "checked"), (r"\brepl'?d\b", "replaced"),
    (r"\bok\b", "OK"),
]
ACRONYMS = ["gfci", "afci", "gfi", "hvac", "led", "pvc", "cpvc", "pex", "a/c", "hepa", "osb", "mdf", "ptfe"]

BASE_TO_PAST = {
    "replace": "replaced", "install": "installed", "test": "tested", "check": "checked", "clean": "cleaned",
    "repair": "repaired", "tighten": "tightened", "fix": "fixed", "paint": "painted", "patch": "patched",
    "mow": "mowed", "trim": "trimmed", "seal": "sealed", "inspect": "inspected", "remove": "removed",
    "flush": "flushed", "adjust": "adjusted", "swap": "swapped", "prime": "primed", "verify": "verified",
    "confirm": "confirmed", "clear": "cleared", "unclog": "unclogged", "wire": "wired", "mount": "mounted",
    "sand": "sanded", "caulk": "caulked", "vacuum": "vacuumed", "mop": "mopped", "wipe": "wiped",
    "rewire": "rewired", "reset": "reset", "run": "ran", "measure": "measured", "haul": "hauled",
    "blow": "blew", "bleed": "bled", "hang": "hung", "sweep": "swept", "dig": "dug", "lay": "laid",
    "build": "built", "rebuild": "rebuilt", "rake": "raked", "edge": "edged", "prune": "pruned",
    "weed": "weeded", "spray": "sprayed", "scrub": "scrubbed", "dust": "dusted", "polish": "polished",
    "grout": "grouted", "tape": "taped", "lubricate": "lubricated", "oil": "oiled", "drain": "drained",
    "snake": "snaked", "recharge": "recharged", "reconnect": "reconnected", "secure": "secured",
}
# Past tenses that don't end in -ed. "did" is left out on purpose ("did not replace" is not work done).
IRREGULAR_PAST = {"ran", "put", "set", "cut", "fit", "built", "made", "swept", "dug", "laid", "hung",
                  "took", "reset", "rebuilt", "spread", "found", "left", "got", "rewound", "shut",
                  "blew", "bled", "bent", "drew", "fed", "ground", "kept", "lit", "rehung", "re-hung",
                  "shook", "spun", "split", "stuck", "threw", "tore", "wound", "brought", "bought", "sank",
                  "redid", "rewrote"}
TEST_VERBS = {"tested", "checked", "verified", "confirmed", "measured", "ran", "monitored", "observed",
              "pressure-tested", "retested", "rechecked", "inspected"}
PRONOUN_LEAD = re.compile(
    r"^(?:(?:then|also|and|so|after that|afterwards|finally)[,]?\s+)*"
    r"(?:i|we|i've|we've|i have|we have|tech|technician|crew|the tech|the crew|our tech)\s+",
    re.IGNORECASE,
)
CUSTOMER_REPORT = re.compile(
    r"^(?:the\s+)?(?:customer|client|homeowner|tenant|owner|resident)s?\s+"
    r"(?:reported|said|stated|mentioned|complained|noticed|called|requested|asked|explained|wanted|needed|"
    r"wants|needs|would like)\b",
    re.IGNORECASE,
)
FINDING = re.compile(r"^(?:found|discovered|noticed|observed|there (?:was|were|is|are)|diagnosed)\b", re.IGNORECASE)
RECOMMEND = re.compile(
    r"\b(?:recommend(?:ed|s)?|suggest(?:ed|s)?|should (?:be|have|get|consider)|will need|needs? to be|"
    r"follow[- ]?up|return visit|come back|consider|advise[ds]? (?:to|that|the))\b",
    re.IGNORECASE,
)
CUSTOMER_NOTE = re.compile(
    r"\b(?:customer should|please|avoid|do not|don't|keep (?:the|it|area)|allow|let (?:it|the)|note:|fyi|"
    r"be aware|warning|caution|until|before using|wait)\b",
    re.IGNORECASE,
)
MATERIAL_LINE = re.compile(r"^(?:materials?|parts?|parts used|materials used|supplies)\s*[:\-]\s*(.+)$",
                           re.IGNORECASE)
CLAUSE_SPLIT = re.compile(r",\s*(?:and\s+|then\s+)?|;\s*|\s+then\s+|\s+and then\s+|\s+and\s+(?=(?:then\s+)?[a-z]+ed\b)",
                          re.IGNORECASE)


def _tidy(text):
    text = text.strip()
    for pattern, replacement in SHORTHAND:
        text = re.sub(pattern, replacement, text, flags=re.IGNORECASE)
    for word in ACRONYMS:
        text = re.sub(rf"(?<![\w/]){re.escape(word)}(?![\w/])", word.upper(), text, flags=re.IGNORECASE)
    text = re.sub(r"\s+", " ", text).strip(" .,;-")
    return text


def _cap(text):
    return text[:1].upper() + text[1:] if text else text


def _first_word(text):
    m = re.match(r"[A-Za-z\-']+", text)
    return m.group(0).lower() if m else ""


def _as_past(clause):
    """Turn 'replace pipe' into 'replaced pipe' for rough imperative notes."""
    word = _first_word(clause)
    if word in BASE_TO_PAST:
        return BASE_TO_PAST[word] + clause[len(word):]
    return clause


def _is_action(clause):
    word = _first_word(clause)
    return word.endswith("ed") or word in IRREGULAR_PAST or word in BASE_TO_PAST


def _sentences(notes):
    lines = []
    for raw_line in re.split(r"\n+", notes):
        raw_line = raw_line.strip().lstrip("-*•·").strip()
        if raw_line:
            lines.extend(s for s in re.split(r"(?<=[.!?])\s+", raw_line) if s.strip())
    return lines


def _split_list(text):
    return [_cap(_tidy(p)) for p in re.split(r"[,;]", text) if _tidy(p)]


def _join(items):
    if len(items) <= 1:
        return "".join(items)
    if len(items) == 2:
        return f"{items[0]} and {items[1]}"
    return ", ".join(items[:-1]) + f", and {items[-1]}"


def _lower_first(text):
    if len(text) > 1 and text[1].islower():
        return text[0].lower() + text[1:]
    return text


def analyse_notes(notes):
    out = {"issues": [], "findings": [], "work": [], "tests": [], "notes": [], "follow_up": [],
           "materials": [], "other": [], "uncertain": []}
    for sentence in _sentences(notes):
        s = _tidy(sentence)
        if not s:
            continue
        m = MATERIAL_LINE.match(s)
        if m:
            out["materials"].extend(_split_list(m.group(1)))
            continue
        if CUSTOMER_REPORT.match(s):
            out["issues"].append(_cap(s))
            continue
        if RECOMMEND.search(s):
            out["follow_up"].append(_cap(PRONOUN_LEAD.sub("", s)))
            continue
        if HEDGE_RE.search(s):
            out["uncertain"].append(sentence.strip())  # guesses are left out; the owner is told
            continue
        if CUSTOMER_NOTE.search(s) and not _is_action(PRONOUN_LEAD.sub("", s)):
            out["notes"].append(_cap(s))
            continue
        body = PRONOUN_LEAD.sub("", s)
        if FINDING.match(body):
            out["findings"].append(_cap(body))
            if re.search(r"and\s+[a-z]+ed", body, re.IGNORECASE):
                out["work"].append(_cap(body))   # "found X and repaired it" is also work done
            continue
        clauses = []
        for piece in CLAUSE_SPLIT.split(body):
            piece = _as_past(PRONOUN_LEAD.sub("", _tidy(piece)))
            if not piece:
                continue
            if clauses and not _is_action(piece):
                clauses[-1] = f"{clauses[-1]}, {piece}"   # e.g. "installed faucet, chrome finish"
            elif clauses and " " not in clauses[-1]:
                clauses[-1] = f"{clauses[-1]} and {piece}"  # "drained and flushed tank" stays together
            else:
                clauses.append(piece)
        for clause in clauses:
            if not _is_action(clause):
                out["other"].append(_cap(clause))
            elif _first_word(clause) in TEST_VERBS:
                out["tests"].append(_cap(clause))
            else:
                out["work"].append(_cap(clause))
    return out


def _opening_sentence(job_input, status):
    """'On October 20, 2026, Torres Plumbing completed the Kitchen Sink Repair job for Sarah Johnson at
    48 Willow Lane.' Every part comes from the job or business profile; missing parts are left out."""
    business = (job_input.get("business_name") or "").strip()
    service = job_input["service_type"].strip()
    title = job_input["job_title"].strip()
    job = f"the {title} job"
    if service and service.lower() not in title.lower():
        job += f" ({service.lower()} service)"
    verb = "completed" if status == "Completed" else "performed"
    where = f" at {job_input['service_address']}" if job_input["service_address"] else ""
    when = f"On {job_input['job_date']}, " if job_input["job_date"] else ""
    if business:
        sentence = f"{when}{business} {verb} {job} for {job_input['customer_name']}{where}"
    else:
        subject = job if when else _cap(job)
        sentence = f"{when}{subject} was {verb} for {job_input['customer_name']}{where}"
    if status == "In Progress":
        sentence += " (work in progress)"
    return sentence + "."


def generate(job_input, _config=None):
    facts = analyse_notes(job_input["notes"])
    if job_input.get("materials"):
        explicit = [m for line in job_input["materials"].splitlines() for m in _split_list(line)]
        facts["materials"] = explicit + [m for m in facts["materials"] if m not in explicit]

    work = facts["work"]
    actor = job_input["technician"].strip() or "The technician"

    status = job_input["job_status"]
    summary = [_opening_sentence(job_input, status)]
    for issue in facts["issues"][:2]:
        text = issue if issue.lower().startswith("the ") else "The " + _lower_first(issue)
        summary.append(text.rstrip(".") + ".")
    for finding in facts["findings"][:1]:  # all findings are listed in their own section
        summary.append(finding.rstrip(".") + ".")
    if facts["work"]:
        summary.append(f"{actor} {_join([_lower_first(w) for w in facts['work'][:3]])}.")
    if facts["tests"]:
        tests = _join([_lower_first(t) for t in facts["tests"][:3]])
        if facts["work"]:
            who = "the technician" if actor == "The technician" else actor
            summary.append(f"Afterward, {who} {tests}.")
        else:
            summary.append(f"{actor} {tests}.")

    customer_notes = list(facts["notes"])
    if status == "In Progress":
        customer_notes.append(f"This job is marked {status.lower()}; this report covers the work recorded so far.")
    # Statements that aren't clearly completed work (e.g. "Leak not fixed") are passed on as notes,
    # never listed as work completed.
    customer_notes.extend(facts["other"])

    return {
        "summary": " ".join(summary),
        "reported_issue": [i.rstrip(".") for i in facts["issues"]],
        "findings": [f.rstrip(".") for f in facts["findings"]],
        "work_completed": work,
        "materials": facts["materials"],
        "testing": facts["tests"],
        "customer_notes": customer_notes,
        "follow_up": facts["follow_up"],
    }
