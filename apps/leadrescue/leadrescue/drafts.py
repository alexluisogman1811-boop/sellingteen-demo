"""Routes for customer message drafts.

Flow: generate (any role) -> edit (any role) -> approve (owner only, with an
explicit "I've reviewed this" confirmation) -> copy and send it yourself ->
mark as sent (logs it on the timeline). LeadRescue never sends anything.
Editing an approved draft sends it back to "draft" so it must be re-approved.
"""
from datetime import date, datetime, timezone
from .dates import local_today

from flask import Blueprint, abort, flash, g, redirect, render_template, url_for
from flask_login import current_user, login_required

from .drafting import build_context, describe, find_issues, generate_draft
from .drafting.providers import DraftRejected
from .extensions import db
from .forms import DraftApproveForm, DraftEditForm, DraftRequestForm
from .models import (
    DRAFT_STATUS_APPROVED,
    DRAFT_STATUS_DISCARDED,
    DRAFT_STATUS_DRAFT,
    DRAFT_STATUS_SENT,
    ROLE_OWNER,
    Activity,
    MessageDraft,
)
from .tenancy import current_business_id, get_lead_or_404, require_role

bp = Blueprint("drafts", __name__, url_prefix="/leads/<int:lead_id>/drafts")


def _get_draft_or_404(lead, draft_id: int) -> MessageDraft:
    draft = db.session.execute(
        db.select(MessageDraft).where(
            MessageDraft.id == draft_id,
            MessageDraft.lead_id == lead.id,
            MessageDraft.business_id == current_business_id(),
        )
    ).scalar_one_or_none()
    if draft is None:
        abort(404)
    return draft


def _context_for(lead, channel):
    return build_context(lead, channel, g.business.name, current_user.full_name)


@bp.route("", methods=["POST"])
@login_required
def create(lead_id):
    lead = get_lead_or_404(lead_id)
    form = DraftRequestForm()
    if not form.validate_on_submit():
        abort(400)
    ctx = _context_for(lead, form.channel.data)
    try:
        result = generate_draft(ctx)
    except DraftRejected:
        flash("A draft couldn't be generated for this lead. Please write the message yourself.", "error")
        return redirect(url_for("leads.detail", lead_id=lead.id))
    draft = MessageDraft(
        business_id=lead.business_id,
        lead_id=lead.id,
        channel=ctx.channel,
        subject=result.subject,
        body=result.body,
        provider=result.provider,
        warnings="\n".join(result.warnings),
        created_by_id=current_user.id,
    )
    db.session.add(draft)
    db.session.commit()
    flash("Draft created. It has NOT been sent. Review and edit it below.", "info")
    return redirect(url_for("drafts.view", lead_id=lead.id, draft_id=draft.id))


@bp.route("/<int:draft_id>", methods=["GET", "POST"])
@login_required
def view(lead_id, draft_id):
    lead = get_lead_or_404(lead_id)
    draft = _get_draft_or_404(lead, draft_id)
    form = DraftEditForm(obj=draft)
    editable = draft.status in (DRAFT_STATUS_DRAFT, DRAFT_STATUS_APPROVED)
    if form.is_submitted():
        if not editable:
            abort(400)
        if form.validate_on_submit():
            changed = form.body.data != draft.body or (form.subject.data or "") != draft.subject
            if changed:
                draft.body = form.body.data
                draft.subject = form.subject.data if draft.channel == "email" else ""
                if draft.status == DRAFT_STATUS_APPROVED:
                    draft.status = DRAFT_STATUS_DRAFT
                    draft.approved_by_id = None
                    draft.approved_at = None
                    flash("Draft saved. Because it changed, it needs owner approval again.", "info")
                else:
                    flash("Draft saved.", "success")
                db.session.commit()
            return redirect(url_for("drafts.view", lead_id=lead.id, draft_id=draft.id))
    ctx = _context_for(lead, draft.channel)
    review_notes = describe(find_issues(draft.body, ctx, draft.channel))
    if draft.subject:
        review_notes += describe(find_issues(draft.subject, ctx, "email"))
    return render_template(
        "drafts/view.html",
        lead=lead,
        draft=draft,
        form=form,
        approve_form=DraftApproveForm(),
        editable=editable,
        review_notes=list(dict.fromkeys(review_notes)),
        facts=ctx.facts(),
    )


@bp.route("/<int:draft_id>/approve", methods=["POST"])
@login_required
@require_role(ROLE_OWNER)
def approve(lead_id, draft_id):
    lead = get_lead_or_404(lead_id)
    draft = _get_draft_or_404(lead, draft_id)
    form = DraftApproveForm()
    if draft.status != DRAFT_STATUS_DRAFT:
        abort(400)
    if not form.validate_on_submit():
        flash("Tick the box to confirm you've reviewed the message before approving it.", "error")
        return redirect(url_for("drafts.view", lead_id=lead.id, draft_id=draft.id))
    draft.status = DRAFT_STATUS_APPROVED
    draft.approved_by_id = current_user.id
    draft.approved_at = datetime.now(timezone.utc)
    db.session.commit()
    flash("Approved. Copy the message and send it from your own phone or email, then mark it as sent.", "success")
    return redirect(url_for("drafts.view", lead_id=lead.id, draft_id=draft.id))


@bp.route("/<int:draft_id>/mark-sent", methods=["POST"])
@login_required
def mark_sent(lead_id, draft_id):
    lead = get_lead_or_404(lead_id)
    draft = _get_draft_or_404(lead, draft_id)
    if draft.status != DRAFT_STATUS_APPROVED:
        flash("Only messages approved by the owner can be marked as sent.", "error")
        return redirect(url_for("drafts.view", lead_id=lead.id, draft_id=draft.id))
    now = datetime.now(timezone.utc)
    draft.status = DRAFT_STATUS_SENT
    draft.sent_at = now
    db.session.add(Activity(
        business_id=lead.business_id, lead_id=lead.id, user_id=current_user.id,
        type=draft.channel, occurred_at=now,
        body=f"Sent approved {draft.channel_label.lower()} (sent by hand, outside LeadRescue):\n{draft.body}"[:2000],
    ))
    if lead.last_contact_date is None or lead.last_contact_date < local_today():
        lead.last_contact_date = local_today()
    if lead.status == "new":
        lead.status = "contacted"
    db.session.commit()
    flash("Marked as sent and added to the timeline.", "success")
    return redirect(url_for("leads.detail", lead_id=lead.id))


@bp.route("/<int:draft_id>/discard", methods=["POST"])
@login_required
def discard(lead_id, draft_id):
    lead = get_lead_or_404(lead_id)
    draft = _get_draft_or_404(lead, draft_id)
    if draft.status == DRAFT_STATUS_SENT:
        abort(400)
    draft.status = DRAFT_STATUS_DISCARDED
    db.session.commit()
    flash("Draft discarded.", "info")
    return redirect(url_for("leads.detail", lead_id=lead.id))
