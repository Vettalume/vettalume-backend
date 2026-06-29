"""Email delivery (Phase 16).

One tiny seam: send_email(). If SMTP is configured it sends; if not, it prints the message (including
the OTP) to the server console so the entire signup flow is testable locally with no mail provider.
Swap smtplib for SendGrid/SES/Resend by editing only the _send_smtp branch.
"""
from __future__ import annotations

import logging
import smtplib
from email.message import EmailMessage

from ..config import settings

log = logging.getLogger("vettalume.email")


def is_configured() -> bool:
    return bool(settings.smtp_host)


def send_email(to: str, subject: str, body: str) -> dict:
    if not is_configured():
        # DEV fallback — DO NOT use in production. Lets you read the OTP from the uvicorn console.
        print(
            f"\n──────── EMAIL (dev mode — NOT actually sent) ────────\n"
            f"To:      {to}\nSubject: {subject}\n\n{body}\n"
            f"──────────────────────────────────────────────────────\n",
            flush=True,
        )
        log.warning("dev-email to %s: %s", to, subject)
        return {"sent": False, "dev": True}
    msg = EmailMessage()
    msg["From"], msg["To"], msg["Subject"] = settings.smtp_from, to, subject
    msg.set_content(body)
    with smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=20) as s:
        if settings.smtp_starttls:
            s.starttls()
        if settings.smtp_user:
            s.login(settings.smtp_user, settings.smtp_password)
        s.send_message(msg)
    return {"sent": True, "dev": False}


def send_otp(to: str, name: str, code: str) -> dict:
    mins = max(1, settings.otp_ttl_seconds // 60)
    body = (f"Hi {name},\n\n"
            f"Your {settings.public_name} verification code is:\n\n    {code}\n\n"
            f"It expires in {mins} minutes. If you didn't request this, you can ignore this email.\n")
    return send_email(to, f"Your {settings.public_name} verification code", body)


def send_welcome(to: str, name: str) -> dict:
    subject = f"Welcome to {settings.public_name} - Your Journey to Success Starts Today!"
    body = (
        f"Dear {name},\n\n"
        f"Welcome to {settings.public_name}! We're excited to have you as part of our learning "
        f"community.\n\n"
        f"Whether you're preparing for CAT, GMAT, GRE, or other competitive exams, you've taken the "
        f"first step toward achieving your academic and career goals. Our mission is to help you "
        f"succeed with the right guidance, structured learning, and continuous support.\n\n"
        f"Here's what you'll get access to:\n\n"
        f"- Comprehensive study material designed by experts\n"
        f"- Full-length mock tests and sectional quizzes\n"
        f"- Performance analytics to track your progress\n"
        f"- Personalized recommendations based on your strengths and weaknesses\n"
        f"- Live classes, doubt-solving sessions, and mentorship\n"
        f"- A community of ambitious learners just like you\n\n"
        f"Get Started in 3 Simple Steps:\n\n"
        f"1. Complete your profile.\n"
        f"2. Take a diagnostic test to assess your current level.\n"
        f"3. Create your study plan and begin your preparation.\n\n"
        f"Remember, success isn't about studying harder - it's about studying smarter and staying "
        f"consistent. We're here to support you at every step of your journey.\n\n"
        f"If you ever need help, have questions, or simply want guidance, our support team is just an "
        f"email away.\n\n"
        f"We can't wait to celebrate your success!\n\n"
        f"Best wishes,\n"
        f"Team {settings.public_name}\n"
        f"Empowering Aspirants. Delivering Results.\n\n"
        f"Email: support@vettalume.com\n"
        f"Website: www.vettalume.com\n"
    )
    return send_email(to, subject, body)
