"""Email delivery (Phase 16).

One tiny seam: send_email(). Transport is chosen at call time, best → fallback:
  1. Resend  — if RESEND_API_KEY is set (the production path; HTTP API, no SMTP ports needed).
  2. SMTP    — if SMTP_HOST is set (self-hosted / any provider that speaks SMTP).
  3. Dev     — otherwise print the message (including the OTP) to the server console so the entire
               signup flow is testable locally with no mail provider.

Resend is called over its REST API with the stdlib (urllib), matching services/payments.py — so there
is no extra dependency and no SMTP egress required (handy on hosts that block outbound port 587).

send_otp() propagates transport errors so signup can surface a "couldn't send code" failure and the
user can retry. The fire-and-forget notifications (welcome, payment receipt) are best-effort: a mail
outage must never roll back a completed verification or a captured payment, so they swallow + log.
"""
from __future__ import annotations

import json
import logging
import smtplib
import urllib.error
import urllib.request
from email.message import EmailMessage

from fastapi import HTTPException

from ..config import settings

log = logging.getLogger("vettalume.email")

_RESEND_API = "https://api.resend.com/emails"


def is_configured() -> bool:
    """True when a real transport (Resend or SMTP) is wired; False => dev-console mode."""
    return bool(settings.resend_api_key or settings.smtp_host)


def _send_resend(to: str, subject: str, body: str, html: str | None = None) -> dict:
    payload: dict = {"from": settings.mail_from, "to": [to], "subject": subject, "text": body}
    if html:
        payload["html"] = html
    req = urllib.request.Request(
        _RESEND_API, data=json.dumps(payload).encode(), method="POST",
        headers={"Authorization": f"Bearer {settings.resend_api_key}",
                 "Content-Type": "application/json",
                 # Resend sits behind Cloudflare, which blocks the default "Python-urllib" UA with a
                 # 403 (error code 1010). A normal UA header is required.
                 "User-Agent": "vettalume-backend/0.10 (+https://vettalume.com)"})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            resp = json.loads(r.read() or b"{}")
        return {"sent": True, "dev": False, "provider": "resend", "id": resp.get("id")}
    except urllib.error.HTTPError as e:              # Resend rejected the request (bad key/from/etc.)
        detail = e.read().decode()[:300]
        log.error("resend send to %s failed: %s %s", to, e.code, detail)
        raise HTTPException(502, {"error": "email_send_failed", "detail": detail})
    except urllib.error.URLError as e:               # couldn't reach Resend
        log.error("resend unreachable: %s", e.reason)
        raise HTTPException(502, {"error": "email_provider_unreachable", "detail": str(e.reason)})


def _send_smtp(to: str, subject: str, body: str) -> dict:
    msg = EmailMessage()
    msg["From"], msg["To"], msg["Subject"] = settings.mail_from, to, subject
    msg.set_content(body)
    with smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=20) as s:
        if settings.smtp_starttls:
            s.starttls()
        if settings.smtp_user:
            s.login(settings.smtp_user, settings.smtp_password)
        s.send_message(msg)
    return {"sent": True, "dev": False, "provider": "smtp"}


def send_email(to: str, subject: str, body: str, html: str | None = None) -> dict:
    """Send one email via the best configured transport. Raises HTTPException(502) if a configured
    provider fails (so OTP delivery failures surface); prints to console when nothing is configured."""
    # In development we never call an external provider — this dodges provider limits (e.g. Resend's
    # sandbox only mails your own verified address), so signup/OTP is fully self-serve with no mail
    # configured. The OTP itself is surfaced to the client by the auth layer. Production always sends
    # for real (production_problems() forces DEV_MODE off before a live deploy can boot).
    if settings.dev_mode:
        print(
            f"\n──────── EMAIL (dev mode — NOT actually sent) ────────\n"
            f"To:      {to}\nSubject: {subject}\n\n{body}\n"
            f"──────────────────────────────────────────────────────\n",
            flush=True,
        )
        return {"sent": False, "dev": True}
    if settings.resend_api_key:
        return _send_resend(to, subject, body, html)
    if settings.smtp_host:
        return _send_smtp(to, subject, body)
    # DEV fallback — DO NOT use in production. Lets you read the OTP from the uvicorn console.
    print(
        f"\n──────── EMAIL (dev mode — NOT actually sent) ────────\n"
        f"To:      {to}\nSubject: {subject}\n\n{body}\n"
        f"──────────────────────────────────────────────────────\n",
        flush=True,
    )
    log.warning("dev-email to %s: %s", to, subject)
    return {"sent": False, "dev": True}


def _best_effort(fn, *args, **kwargs) -> dict:
    """Run a notification send without letting a mail outage break the caller's transaction."""
    try:
        return fn(*args, **kwargs)
    except Exception as e:                            # noqa: BLE001 — notifications are non-critical
        log.error("email notification failed: %s", e)
        return {"sent": False, "error": str(e)}


def send_otp(to: str, name: str, code: str) -> dict:
    """OTP delivery. Propagates transport errors so /signup and /resend-otp can report the failure."""
    mins = max(1, settings.otp_ttl_seconds // 60)
    body = (f"Hi {name},\n\n"
            f"Your {settings.public_name} verification code is:\n\n    {code}\n\n"
            f"It expires in {mins} minutes. If you didn't request this, you can ignore this email.\n")
    return send_email(to, f"Your {settings.public_name} verification code", body)


def send_welcome(to: str, name: str) -> dict:
    """Best-effort welcome email (sent after verification / Google sign-up)."""
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
    return _best_effort(send_email, to, subject, body)


def send_payment_confirmation(to: str, name: str, *, plan_name: str, amount: float, currency: str,
                              months: int, access: list[dict], payment_id: str | None = None) -> dict:
    """Best-effort payment receipt, sent after a payment is verified/captured and access is granted.
    `access` is [{"exam", "expires_at"}] from billing.grant_subscription."""
    symbol = {"INR": "₹", "USD": "$", "EUR": "€", "GBP": "£"}.get((currency or "").upper(), "")
    amount_str = f"{symbol}{amount:,.2f}" if symbol else f"{amount:,.2f} {currency}"
    lines = "\n".join(
        f"  - {a.get('exam')}: access until {(a.get('expires_at') or '')[:10]}" for a in access
    ) or "  - Your access has been activated."
    term = f"{months} month{'s' if months != 1 else ''}"
    body = (
        f"Hi {name},\n\n"
        f"Thank you for your purchase — your payment was successful and your access is now active.\n\n"
        f"Payment summary\n"
        f"---------------\n"
        f"Plan:     {plan_name}\n"
        f"Amount:   {amount_str}\n"
        f"Duration: {term}\n"
        + (f"Payment ID: {payment_id}\n" if payment_id else "")
        + f"\nWhat you can now access:\n{lines}\n\n"
        f"You can review your subscriptions anytime from your account. If you have any questions about "
        f"your purchase, just reply to this email and our support team will help.\n\n"
        f"Happy learning,\n"
        f"Team {settings.public_name}\n\n"
        f"Email: support@vettalume.com\n"
        f"Website: www.vettalume.com\n"
    )
    return _best_effort(send_email, to, f"Payment confirmed — welcome to {plan_name}", body)
