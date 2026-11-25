from datetime import date

from flask_wtf import FlaskForm
from flask_wtf.file import FileAllowed, FileField, FileRequired
from wtforms import (
    BooleanField,
    DateField,
    DecimalField,
    EmailField,
    IntegerField,
    PasswordField,
    SelectField,
    StringField,
    TextAreaField,
)
from wtforms.validators import (
    DataRequired,
    Email,
    EqualTo,
    InputRequired,
    Length,
    NumberRange,
    Optional,
    Regexp,
    StopValidation,
    ValidationError,
)

from .dates import DEFAULT_TZ, US_TIMEZONES, local_today
from .models import ACTIVITY_TYPES, LEAD_SOURCES, LEAD_STATUSES, ROLE_ADMIN, ROLE_MEMBER

US_STATES = (
    "AL AK AZ AR CA CO CT DE DC FL GA HI ID IL IN IA KS KY LA ME MD MA MI MN MS MO MT NE NV NH NJ "
    "NM NY NC ND OH OK OR PA RI SC SD TN TX UT VT VA WA WV WI WY PR"
).split()

DATE_FORMATS = ["%Y-%m-%d", "%m/%d/%Y"]


def strip(value):
    return value.strip() if isinstance(value, str) else value


def upper_strip(value):
    return value.strip().upper() if isinstance(value, str) else value


def lower_strip(value):
    return value.strip().lower() if isinstance(value, str) else value


def reasonable_date(form, field):
    if field.data and not (date(1990, 1, 1) <= field.data <= date(2100, 12, 31)):
        raise ValidationError("Date must be between 1990 and 2100.")


def not_in_future(form, field):
    # Contact can't have happened after today (business-local date).
    if field.data and field.data > local_today():
        raise ValidationError("This date can't be in the future.")


def finite_number(form, field):
    # Decimal.is_finite() is safe for every Decimal, including signalling NaN
    # (math.isfinite raises on sNaN).
    if field.data is not None and not field.data.is_finite():
        raise StopValidation("Enter a normal number.")


class SignupForm(FlaskForm):
    full_name = StringField("Your name", validators=[DataRequired(), Length(max=120)], filters=[strip])
    business_name = StringField("Business name", validators=[Optional(), Length(max=120)], filters=[strip])
    email = EmailField("Email", validators=[DataRequired(), Email(check_deliverability=False), Length(max=254)], filters=[lower_strip])
    password = PasswordField("Password", validators=[DataRequired(), Length(min=10, max=128, message="Use 10 to 128 characters.")])
    confirm = PasswordField("Confirm password", validators=[DataRequired(), EqualTo("password", message="Passwords must match.")])
    access_code = StringField("Pilot access code", validators=[Optional(), Length(max=100)], filters=[strip])
    accept_terms = BooleanField(
        "I agree to the Terms of Service and have read the Privacy Notice.",
        validators=[DataRequired(message="You need to accept the terms to create an account.")],
    )


class LoginForm(FlaskForm):
    email = EmailField("Email", validators=[DataRequired(), Length(max=254)], filters=[lower_strip])
    password = PasswordField("Password", validators=[DataRequired(), Length(max=128)])


class LeadForm(FlaskForm):
    first_name = StringField("First name", validators=[DataRequired(), Length(max=80)], filters=[strip])
    last_name = StringField("Last name", validators=[Optional(), Length(max=80)], filters=[strip])
    phone = StringField(
        "Phone",
        validators=[Optional(), Length(max=30), Regexp(r"^[0-9+().\-\s]{7,30}$", message="Use digits, spaces and + ( ) - . only.")],
        filters=[strip],
    )
    email = EmailField("Email", validators=[Optional(), Email(check_deliverability=False), Length(max=254)], filters=[lower_strip])
    address = StringField("Street address", validators=[Optional(), Length(max=200)], filters=[strip])
    city = StringField("City", validators=[Optional(), Length(max=80)], filters=[strip])
    state = StringField("State", validators=[Optional(), Length(max=2)], filters=[upper_strip])
    zip_code = StringField(
        "ZIP", validators=[Optional(), Regexp(r"^\d{5}(-\d{4})?$", message="Use a 5-digit ZIP (or ZIP+4).")], filters=[strip]
    )
    source = SelectField("Lead source", choices=LEAD_SOURCES, default="other")
    status = SelectField("Status", choices=LEAD_STATUSES, default="new")
    job_type = StringField("Job type", validators=[Optional(), Length(max=120)], filters=[strip],
                           description="e.g. Full reroof, Leak repair, Storm damage")
    estimated_value = DecimalField(
        "Estimated job value ($)", places=2,
        validators=[Optional(), finite_number, NumberRange(min=0, max=10_000_000, message="Enter an amount from 0 to 10,000,000.")],
    )
    last_contact_date = DateField("Last contact date", format=DATE_FORMATS, validators=[Optional(), reasonable_date, not_in_future])
    next_follow_up_date = DateField("Next follow-up date", format=DATE_FORMATS, validators=[Optional(), reasonable_date])
    assigned_to_id = SelectField("Assigned to", coerce=int, default=0, validate_choice=True)
    notes = TextAreaField("Notes", validators=[Optional(), Length(max=5000)], filters=[strip])

    def validate_state(self, field):
        if field.data and field.data not in US_STATES:
            raise ValidationError("Use a 2-letter US state code, e.g. TX.")

    def validate_phone(self, field):
        if field.data and sum(ch.isdigit() for ch in field.data) < 7:
            raise ValidationError("Phone number needs at least 7 digits.")


class ActivityForm(FlaskForm):
    type = SelectField("Type", choices=ACTIVITY_TYPES, default="call")
    body = TextAreaField("What happened?", validators=[Optional(), Length(max=2000)], filters=[strip])
    occurred_on = DateField("Date", format=DATE_FORMATS, default=local_today, validators=[InputRequired(), reasonable_date, not_in_future])
    next_follow_up_date = DateField("Next follow-up (optional)", format=DATE_FORMATS, validators=[Optional(), reasonable_date])


class SnoozeForm(FlaskForm):
    days = IntegerField("Days", validators=[InputRequired(), NumberRange(min=1, max=90)])


class StatusForm(FlaskForm):
    status = SelectField("Status", choices=LEAD_STATUSES)


class ImportForm(FlaskForm):
    file = FileField("CSV file", validators=[FileRequired(), FileAllowed(["csv"], "Only .csv files are allowed.")])


class InviteForm(FlaskForm):
    role = SelectField("Role", choices=[(ROLE_MEMBER, "Member"), (ROLE_ADMIN, "Admin")], default=ROLE_MEMBER)


class RoleForm(FlaskForm):
    role = SelectField("Role", choices=[(ROLE_MEMBER, "Member"), (ROLE_ADMIN, "Admin")])


class DraftRequestForm(FlaskForm):
    channel = SelectField("Send as", choices=[("text", "Text message"), ("email", "Email")], default="text")


class DraftEditForm(FlaskForm):
    subject = StringField("Email subject", validators=[Optional(), Length(max=150)], filters=[strip])
    body = TextAreaField("Message", validators=[DataRequired(), Length(max=2000)], filters=[strip])


class DraftApproveForm(FlaskForm):
    reviewed = BooleanField(
        "I've read this message and checked that every detail is correct.",
        validators=[DataRequired(message="Tick the box to confirm you've reviewed the message.")],
    )


class DeleteWorkspaceForm(FlaskForm):
    confirm_name = StringField("Type the business name to confirm", validators=[DataRequired(), Length(max=120)], filters=[strip])
    password = PasswordField("Your password", validators=[DataRequired(), Length(max=128)])


class DeleteAccountForm(FlaskForm):
    password = PasswordField("Your password", validators=[DataRequired(), Length(max=128)])


class BusinessForm(FlaskForm):
    name = StringField("Business name", validators=[DataRequired(), Length(max=120)], filters=[strip])
    timezone = SelectField(
        "Time zone",
        choices=[(tz, f"{label} ({tz})") for tz, label in US_TIMEZONES],
        default=DEFAULT_TZ,
        description="Used for \"today\", overdue and due-today follow-ups.",
    )


class ChangePasswordForm(FlaskForm):
    current_password = PasswordField("Current password", validators=[DataRequired(), Length(max=128)])
    password = PasswordField("New password", validators=[DataRequired(), Length(min=10, max=128, message="Use 10 to 128 characters.")])
    confirm = PasswordField("Confirm new password", validators=[DataRequired(), EqualTo("password", message="Passwords must match.")])


class ResetPasswordForm(FlaskForm):
    password = PasswordField("New password", validators=[DataRequired(), Length(min=10, max=128, message="Use 10 to 128 characters.")])
    confirm = PasswordField("Confirm new password", validators=[DataRequired(), EqualTo("password", message="Passwords must match.")])
