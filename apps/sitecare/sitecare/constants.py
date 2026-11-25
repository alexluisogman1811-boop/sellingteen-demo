"""Choice lists shared by validation, templates and reports.

Each mapping is key -> human label. Keys are what gets stored in the database.
"""

PLATFORMS = {
    "wordpress": "WordPress",
    "woocommerce": "WooCommerce",
    "shopify": "Shopify",
    "drupal": "Drupal",
    "joomla": "Joomla",
    "webflow": "Webflow",
    "squarespace": "Squarespace",
    "wix": "Wix",
    "laravel": "Laravel",
    "static": "Static / custom",
    "other": "Other",
}

PLANS = {
    "none": "No plan",
    "basic": "Basic",
    "standard": "Standard",
    "premium": "Premium",
    "custom": "Custom",
}

WEBSITE_STATUSES = {
    "active": "Active",
    "paused": "Paused",
    "archived": "Archived",
}

CATEGORIES = {
    "backup_verification": "Backup verification",
    "software_updates": "Software updates",
    "form_testing": "Form testing",
    "performance_check": "Performance check",
    "security_review": "Security review",
    "uptime_review": "Uptime / availability review",
    "content_update": "Content update",
    "seo_check": "SEO check",
    "other": "Other",
}

FREQUENCIES = {
    "daily": "Daily",
    "weekly": "Weekly",
    "monthly": "Monthly",
    "custom": "Custom interval",
}

# Results are what the person recording the work reported - never an automated verdict.
RESULTS = {
    "passed": "OK",
    "issues_found": "Issues found",
    "failed": "Failed",
    "not_applicable": "Not applicable",
}

# How a result is known. SiteCare Desk has no monitoring/backup/scan integrations, so
# nothing is ever "verified"; the key exists so reports can say so explicitly.
VERIFICATION = {
    "manual": "Manual record",
    "manual_evidence": "Manual record + evidence",
    "verified": "Verified by integration",
}
VERIFICATION_NOTE = (
    "Manual record: a person entered this result. Evidence files are stored as attached, "
    "but SiteCare Desk does not analyse them. No result was verified automatically - "
    "no backup, uptime or security-scan integration is configured."
)

OCCURRENCE_STATUSES = {
    "pending": "Pending",
    "completed": "Completed",
    "skipped": "Skipped",
}

SEVERITIES = {
    "low": "Low",
    "medium": "Medium",
    "high": "High",
    "critical": "Critical",
}

ISSUE_STATUSES = {
    "open": "Open",
    "in_progress": "In progress",
    "resolved": "Resolved",
    "wont_fix": "Won't fix",
}

ACTIVE_ISSUE_STATUSES = ("open", "in_progress")

SEVERITY_ORDER_SQL = (
    "CASE {col} WHEN 'critical' THEN 0 WHEN 'high' THEN 1 "
    "WHEN 'medium' THEN 2 ELSE 3 END"
)
