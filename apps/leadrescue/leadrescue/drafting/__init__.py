"""Customer message drafting.

Drafts are suggestions only: LeadRescue never sends them. See providers.py for
the provider interface and guard.py for the content safety checks.
"""
from flask import current_app

from .context import DraftContext, build_context
from .guard import describe, find_issues
from .providers import PROVIDERS, AIProvider, DraftProvider, DraftRejected, DraftResult, TemplateProvider

__all__ = [
    "AIProvider", "DraftContext", "DraftProvider", "DraftRejected", "DraftResult", "TemplateProvider",
    "build_context", "describe", "find_issues", "generate_draft", "get_provider",
]


def get_provider() -> tuple[DraftProvider, str | None]:
    """Return (provider, warning). Unknown or unconfigured providers fall back to templates."""
    name = (current_app.config.get("DRAFT_PROVIDER") or "template").strip().lower()
    cls = PROVIDERS.get(name)
    if cls is None:
        return TemplateProvider(), f"Draft provider “{name}” isn't available, so the free template writer was used."
    try:
        return cls(), None
    except Exception:
        return TemplateProvider(), f"Draft provider “{name}” couldn't start, so the free template writer was used."


def generate_draft(ctx: DraftContext, provider: DraftProvider | None = None) -> DraftResult:
    warnings: list[str] = []
    if provider is None:
        provider, warn = get_provider()
        if warn:
            warnings.append(warn)
    try:
        result = provider.generate(ctx)
    except DraftRejected as exc:
        if isinstance(provider, TemplateProvider):
            raise
        warnings.append(f"The AI draft was rejected ({exc}), so the free template writer was used instead.")
        result = TemplateProvider().generate(ctx)
    result.warnings = list(ctx.warnings) + warnings + result.warnings
    return result
