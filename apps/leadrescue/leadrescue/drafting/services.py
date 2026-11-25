"""Maps a free-text job type onto a small set of supported roofing services.

The job type is typed by staff (or imported from a CSV), so it is treated as
untrusted. It is only matched against keywords; its text is never copied into
a message. Each service has a fixed, pre-written phrase.
"""
import re
from dataclasses import dataclass


@dataclass(frozen=True)
class ServiceMatch:
    key: str
    label: str      # shown to the owner in "facts used"
    phrase: str     # the only wording that can appear in a message
    supported: bool
    warning: str | None = None


# Order matters: the first matching rule wins (more specific first).
_RULES = [
    ("storm", "Storm / hail damage", "the storm-related roof work you asked about",
     r"\b(storm|hail|wind)\b"),
    ("leak_repair", "Leak repair", "your roof leak",
     r"\bleak"),
    ("inspection", "Roof inspection", "your roof inspection",
     r"\binspect"),
    # Specific components come before "replacement" so that e.g. "gutter
    # replacement" or "skylight replacement" is never called a roof replacement.
    ("gutters", "Gutters", "your gutters",
     r"\b(gutter|downspout|fascia|soffit)"),
    ("component_repair", "Roof component work", "the work on your roof",
     r"\b(skylight|chimney|flashing|vent|boot)"),
    ("flat_roof", "Flat / low-slope roofing", "your flat roof",
     r"\b(flat|low[- ]slope|tpo|epdm|membrane|coating)"),
    ("metal", "Metal roofing", "your metal roof",
     r"\bmetal\b"),
    ("replacement", "Roof replacement", "your roof replacement",
     r"\b(re-?roof|replace|replacement|new roof|tear[- ]?off|full roof)"),
    ("repair", "Roof repair", "your roof repair",
     r"\b(repair|shingle|patch)"),
    ("roofing", "Roofing (general)", "your roof",
     r"\broof"),
]
_COMPILED = [(key, label, phrase, re.compile(pattern, re.IGNORECASE)) for key, label, phrase, pattern in _RULES]

GENERIC = ServiceMatch(
    key="unknown", label="Unknown", phrase="your project", supported=False,
)


def classify_service(job_type: str | None) -> ServiceMatch:
    text = (job_type or "").strip()
    if not text:
        return ServiceMatch(GENERIC.key, GENERIC.label, GENERIC.phrase, False,
                            "No service type on file, so the draft uses general wording (“your project”).")
    if len(text) > 120:
        text = text[:120]
    for key, label, phrase, rx in _COMPILED:
        if rx.search(text):
            return ServiceMatch(key, label, phrase, True)
    return ServiceMatch(GENERIC.key, GENERIC.label, GENERIC.phrase, False,
                        "The service type isn't a roofing service LeadRescue recognises, so the draft uses "
                        "general wording (“your project”). Edit the draft if you want to be specific.")
