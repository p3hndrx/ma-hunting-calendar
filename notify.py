import logging
import os
import smtplib
from email.mime.text import MIMEText

logger = logging.getLogger(__name__)


def send_status_email(subject: str, body: str) -> None:
    """Send a plain-text status email via Gmail SMTP.
    No-op if GMAIL_USER / GMAIL_APP_PASSWORD / NOTIFY_EMAIL are not set.
    Generate an App Password at: https://myaccount.google.com/apppasswords
    """
    gmail_user   = os.environ.get("GMAIL_USER", "")
    app_password = os.environ.get("GMAIL_APP_PASSWORD", "")
    notify_email = os.environ.get("NOTIFY_EMAIL", gmail_user)

    if not gmail_user or not app_password or not notify_email:
        logger.info("Email notification skipped (GMAIL_USER/GMAIL_APP_PASSWORD/NOTIFY_EMAIL not configured)")
        return

    msg = MIMEText(body, "plain", "utf-8")
    msg["Subject"] = subject
    msg["From"]    = gmail_user
    msg["To"]      = notify_email

    try:
        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as smtp:
            smtp.login(gmail_user, app_password)
            smtp.sendmail(gmail_user, [notify_email], msg.as_string())
        logger.info("Status email sent to %s", notify_email)
    except Exception as e:
        logger.warning("Failed to send status email: %s", e)
