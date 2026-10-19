# SPDX-License-Identifier: AGPL-3.0-or-later
"""Optional SMTP for self-service password reset links (stdlib smtplib)."""

from __future__ import annotations

import smtplib
from email.message import EmailMessage

from wwyd.config import Config


def send_reset_link(config: Config, to: str, url: str) -> None:
    msg = EmailMessage()
    msg["Subject"] = "Reset your wwyd password"
    msg["From"] = config.smtp_from
    msg["To"] = to
    msg.set_content(
        "Someone asked to reset the password of your wwyd account.\n\n"
        f"Open this link within 24 hours to choose a new password:\n{url}\n\n"
        "If this was not you, ignore this message; your password stays as it is.\n"
    )
    with smtplib.SMTP(config.smtp_host or "", config.smtp_port, timeout=15) as smtp:
        smtp.ehlo()
        if smtp.has_extn("starttls"):
            smtp.starttls()
            smtp.ehlo()
        if config.smtp_user:
            smtp.login(config.smtp_user, config.smtp_password or "")
        smtp.send_message(msg)
