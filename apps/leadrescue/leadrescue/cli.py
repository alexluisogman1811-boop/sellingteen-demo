import os

import click
from flask import Flask

from .demo import add_demo_leads
from .drafting import build_context, generate_draft
from .drafting.providers import TemplateProvider
from .extensions import db
from .models import ROLE_OWNER, Business, Lead, Membership, MessageDraft, User, utcnow

DEMO_EMAIL = "demo@example.com"
DEMO_BUSINESS = "DEMO - Summit Ridge Roofing (sample data)"


def register_cli(app: Flask) -> None:
    @app.cli.command("seed-demo")
    @click.option("--reset", is_flag=True, help="Delete and recreate the demo workspace.")
    def seed_demo(reset):
        """Create a DEMO workspace with fictional leads and a demo login."""
        user = db.session.execute(db.select(User).where(User.email == DEMO_EMAIL)).scalar_one_or_none()
        if user and not reset:
            click.echo("Demo user already exists. Use --reset to recreate it.")
            return
        if user:
            for m in list(user.memberships):
                if m.business.is_demo:
                    db.session.delete(m.business)
            db.session.delete(user)
            db.session.commit()

        # The password is never generated-and-printed: terminal output often ends
        # up in shell history, CI logs or hosting dashboards.
        password = os.environ.get("DEMO_PASSWORD", "")
        if len(password) < 10:
            raise click.ClickException("Set DEMO_PASSWORD (at least 10 characters) in .env or your environment first.")
        user = User(email=DEMO_EMAIL, full_name="Demo Owner", terms_accepted_at=utcnow())
        user.set_password(password)
        business = Business(name=DEMO_BUSINESS, is_demo=True)
        db.session.add_all([user, business])
        db.session.add(Membership(user=user, business=business, role=ROLE_OWNER))
        db.session.commit()
        count = add_demo_leads(business, user)

        # One unapproved message draft, so the demo can show the review/approve step.
        lead = db.session.execute(
            db.select(Lead).where(Lead.business_id == business.id, Lead.status == "estimate_sent").order_by(Lead.id)
        ).scalars().first()
        if lead is not None:
            result = generate_draft(build_context(lead, "text", business.name, user.full_name), TemplateProvider())
            db.session.add(MessageDraft(
                business_id=business.id, lead_id=lead.id, channel="text", body=result.body,
                provider=result.provider, warnings="\n".join(result.warnings), created_by_id=user.id,
            ))
            db.session.commit()
        click.echo(f"Created demo workspace '{DEMO_BUSINESS}' with {count} DEMO leads and 1 draft message.")
        click.echo(f"Log in as {DEMO_EMAIL} with the DEMO_PASSWORD you set.")

    @app.cli.command("set-password")
    @click.argument("email")
    @click.password_option("--password", prompt="New password (input hidden)", confirmation_prompt=True)
    def set_password(email, password):
        """Support tool: set a user's password after verifying who they are by phone.

        The password is read from a hidden prompt (never a command-line argument, so it
        isn't saved in shell history), unlocks the account and signs out all devices."""
        user = db.session.execute(db.select(User).where(User.email == email.strip().lower())).scalar_one_or_none()
        if user is None:
            raise click.ClickException("No user with that email.")
        if len(password) < 10 or len(password) > 128:
            raise click.ClickException("Password must be 10 to 128 characters.")
        user.set_password(password)
        user.failed_logins = 0
        user.locked_until = None
        user.rotate_session_token()
        db.session.commit()
        click.echo("Password updated; the account is unlocked and all existing sessions were signed out.")

    @app.cli.command("reset-link")
    @click.argument("email")
    def reset_link(email):
        """Support tool: one-time, 24-hour password reset link (e.g. for an Owner who forgot theirs).

        Confirm who is asking first (see docs/SUPPORT.md). The link is printed once, so send it
        to the person yourself; LeadRescue sends nothing. Works without a password prompt, so it
        can also be run through the hosting package's manage.py."""
        from .passwords import create_reset_link
        user = db.session.execute(db.select(User).where(User.email == email.strip().lower())).scalar_one_or_none()
        if user is None:
            raise click.ClickException("No user with that email.")
        token = create_reset_link(user, business_id=None, created_by_id=None)
        db.session.commit()
        base = (app.config.get("APP_BASE_URL") or "").rstrip("/")
        path = f"/reset/{token}"
        hours = app.config.get("PASSWORD_RESET_HOURS", 24)
        if base:
            click.echo(f"One-time reset link for {user.email} (expires in {hours} hours):\n{base}{path}")
        else:
            click.echo(f"One-time reset path for {user.email} (expires in {hours} hours). Put your site address "
                       f"(including any /leadrescue prefix) in front of it:\n{path}")

    @app.cli.command("stats")
    def stats():
        """Print row counts (no customer data)."""
        for model in (User, Business, Membership, Lead):
            count = db.session.execute(db.select(db.func.count()).select_from(model)).scalar()
            click.echo(f"{model.__tablename__}: {count}")
