"""Report generation entry point.

AI_MOCK=1 (default): deterministic, offline generator in mock.py - no API key needed.
AI_MOCK=0: the provider in anthropic_provider.py. To swap providers, add a module with
a `generate(job_input, config) -> dict` function and select it in `_provider()`.

Whatever the provider returns passes through guard.enforce_grounding(), which drops
anything that introduces prices, warranties, numbers or materials the user never supplied.
"""
import logging
import re

from ..models import JOB_STATUSES, format_date
from .guard import HEDGE_RE, SECTIONS, enforce_grounding, normalise_content

log = logging.getLogger("jobproof.ai")


class AIError(Exception):
    """A generation failure with a message that is safe to show to the user."""

    def __init__(self, user_message):
        super().__init__(user_message)
        self.user_message = user_message


class MissingInformation(AIError):
    pass


def build_job_input(job, business, photos):
    """The only facts a provider may use. Nothing else about the account is sent."""
    stages = {"before": 0, "during": 0, "after": 0}
    for p in photos:
        stages[p["stage"]] += 1
    return {
        "business_name": business["name"] if business else "",
        "customer_name": job["customer_name"],
        "job_title": job["title"],
        "service_type": job["service_type"],
        "service_address": job["address"],
        "job_date": format_date(job["job_date"]),
        "technician": job["technician"],
        "job_status": JOB_STATUSES.get(job["status"], job["status"]),
        "notes": job["notes"],
        "materials": job["materials"],
        "photo_counts": stages,
    }


def source_text(job_input):
    parts = [str(v) for k, v in job_input.items() if k != "photo_counts"]
    return "\n".join(parts)


def _provider(config):
    if config.get("AI_MOCK", True):
        from . import mock
        return "mock", mock.generate
    from . import anthropic_provider
    return "anthropic", anthropic_provider.generate


def generate_report(job, business, photos, config):
    """Return (content_dict, provider_name). Raises AIError with a friendly message."""
    job_input = build_job_input(job, business, photos)
    if not job_input["notes"].strip():
        raise MissingInformation("Add job notes describing the work before generating a report.")

    name, generate = _provider(config)
    try:
        raw = generate(job_input, config)
        content = normalise_content(raw)
    except AIError:
        raise
    except Exception as e:  # never let a provider failure crash the request
        log.warning("Report generation failed (provider=%s, error=%s)", name, type(e).__name__)
        raise AIError("We couldn't generate the report right now. Your job details and photos are saved - "
                      "please try again in a minute.")
    content = enforce_grounding(content, source_text(job_input))
    if not content["summary"] and not content["work_completed"]:
        raise AIError("We couldn't find enough detail in your notes to write a report. "
                      "Add a sentence or two about what was done, then try again.")
    content["owner_warnings"] = owner_warnings(job_input["notes"], content)
    return content, name


def owner_warnings(notes, content):
    """Messages for the business owner only - never stored with the report or shown to the customer."""
    warnings = []
    kept = " ".join(content["follow_up"]).lower()  # recommendations may legitimately look ahead
    uncertain = [s.strip() for s in re.split(r"(?<=[.!?])\s+|\n+", notes)
                 if s.strip() and HEDGE_RE.search(s) and s.strip().rstrip(".!?").lower() not in kept]
    if uncertain:
        quoted = " ".join(f"“{s}”" for s in uncertain[:3])
        warnings.append(f"Some notes sound uncertain, so they were left out of the report: {quoted} "
                        "If the work was done, edit the notes to say so plainly and regenerate.")
    if not content["work_completed"]:
        warnings.append("No completed work was found in your notes, so the report has no “Work Completed” "
                        "section. Add what was done (for example “Replaced the shut-off valve”) and regenerate.")
    return warnings


__all__ = ["AIError", "MissingInformation", "generate_report", "build_job_input", "SECTIONS"]
