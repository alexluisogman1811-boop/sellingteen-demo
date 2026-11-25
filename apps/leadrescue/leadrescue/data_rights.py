"""Data export and deletion (privacy notice: "Your choices").

* Owners can download everything in their workspace as CSV files in a zip.
* Owners can permanently delete their workspace (leads, activity, drafts,
  invitations, memberships) after typing its name and re-entering their password.
* Any user can delete their own account, unless they still own a workspace.
"""
import csv
import io
import zipfile
from datetime import timezone

from flask import Blueprint, Response, abort, flash, g, redirect, render_template, session, url_for
from flask_login import current_user, login_required, logout_user

from . import audit
from .csv_io import export_leads, sanitize_cell
from .dates import local_today
from .extensions import db
from .forms import DeleteAccountForm, DeleteWorkspaceForm
from .models import ROLE_OWNER, Activity, AuditEvent, Business, Lead, Membership, MessageDraft, User
from .tenancy import SESSION_KEY, current_business_id, require_role

bp = Blueprint("data", __name__)


def _csv(header: list[str], rows) -> str:
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(header)
    for row in rows:
        writer.writerow([sanitize_cell(v) for v in row])
    return buf.getvalue()


def _iso(dt):
    if dt is None:
        return ""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.isoformat()


def build_workspace_export(business_id: int) -> bytes:
    leads = db.session.execute(
        db.select(Lead).where(Lead.business_id == business_id).order_by(Lead.id)
    ).scalars().all()
    lead_names = {l.id: l.full_name for l in leads}
    activities = db.session.execute(
        db.select(Activity).where(Activity.business_id == business_id).order_by(Activity.id)
    ).scalars().all()
    drafts = db.session.execute(
        db.select(MessageDraft).where(MessageDraft.business_id == business_id).order_by(MessageDraft.id)
    ).scalars().all()
    members = db.session.execute(
        db.select(Membership).where(Membership.business_id == business_id).order_by(Membership.id)
    ).scalars().all()
    business = db.session.get(Business, business_id)
    events = db.session.execute(
        db.select(AuditEvent).where(AuditEvent.business_id == business_id).order_by(AuditEvent.id)
    ).scalars().all()

    files = {
        "leads.csv": export_leads(leads),
        "activities.csv": _csv(
            ["lead", "type", "body", "occurred_at_utc", "by"],
            [(lead_names.get(a.lead_id, ""), a.type, a.body, _iso(a.occurred_at), a.user.full_name if a.user else "")
             for a in activities],
        ),
        "message_drafts.csv": _csv(
            ["lead", "channel", "status", "subject", "body", "created_at_utc", "approved_at_utc", "sent_at_utc"],
            [(lead_names.get(d.lead_id, ""), d.channel, d.status, d.subject, d.body,
              _iso(d.created_at), _iso(d.approved_at), _iso(d.sent_at)) for d in drafts],
        ),
        "team.csv": _csv(["name", "email", "role"], [(m.user.full_name, m.user.email, m.role) for m in members]),
        "workspace.csv": _csv(
            ["business_name", "created_at_utc", "time_zone", "paid_until"],
            [(business.name, _iso(business.created_at), business.timezone or "", business.paid_until.isoformat() if business.paid_until else "")],
        ),
        "audit_log.csv": _csv(
            ["when_utc", "who", "action", "detail"],
            [(_iso(e.created_at), e.user_name, e.action_label, e.detail) for e in events],
        ),
        "README.txt": (
            "LeadRescue workspace export\n"
            f"Exported: {local_today().isoformat()}\n\n"
            "leads.csv can be re-imported into LeadRescue (Leads > Import CSV).\n"
            "Cells that start with = + - @ are prefixed with ' so spreadsheets don't run them as formulas.\n"
            "This file contains your customers' personal information. Store it securely.\n"
        ),
    }
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, text in files.items():
            zf.writestr(name, text)
    return out.getvalue()


@bp.route("/workspace/data")
@login_required
def page():
    if g.membership is None:
        return redirect(url_for("data.account"))
    return render_template("data/workspace_data.html", delete_form=DeleteWorkspaceForm())


@bp.route("/workspace/export.zip")
@login_required
@require_role(ROLE_OWNER)
def export_workspace():
    data = build_workspace_export(current_business_id())
    audit.record(current_business_id(), "workspace_export", "All workspace data (zip of CSV files).")
    db.session.commit()
    filename = f"leadrescue-export-{local_today().isoformat()}.zip"
    return Response(data, mimetype="application/zip",
                    headers={"Content-Disposition": f'attachment; filename="{filename}"'})


@bp.route("/workspace/delete", methods=["POST"])
@login_required
@require_role(ROLE_OWNER)
def delete_workspace():
    form = DeleteWorkspaceForm()
    business = g.business
    if not form.validate_on_submit():
        flash("Fill in both fields to delete the workspace.", "error")
        return redirect(url_for("data.page"))
    if form.confirm_name.data != business.name or not current_user.check_password(form.password.data):
        flash("The business name or password didn't match. Nothing was deleted.", "error")
        return redirect(url_for("data.page"))
    name = business.name
    db.session.delete(business)  # cascades to leads, activities, drafts, invitations, memberships
    db.session.commit()
    session.pop(SESSION_KEY, None)
    flash(f"Workspace “{name}” and all its data were permanently deleted.", "info")
    return redirect(url_for("main.dashboard"))


@bp.route("/account", methods=["GET", "POST"])
@login_required
def account():
    form = DeleteAccountForm()
    owned = db.session.execute(
        db.select(Business).join(Membership).where(Membership.user_id == current_user.id, Membership.role == ROLE_OWNER)
    ).scalars().all()
    if form.validate_on_submit():
        if owned:
            abort(400)
        if not current_user.check_password(form.password.data):
            flash("That password isn't right. Your account was not deleted.", "error")
            return redirect(url_for("data.account"))
        user = db.session.get(User, current_user.id)
        logout_user()
        session.clear()
        db.session.delete(user)  # memberships cascade; authored records keep the text but lose the name
        db.session.commit()
        flash("Your account was permanently deleted.", "info")
        return redirect(url_for("auth.login"))
    return render_template("data/account.html", form=form, owned=owned)
