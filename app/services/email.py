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
from html import escape as _esc
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


def _console(to: str, subject: str, body: str) -> dict:
    print(
        f"\n──────── EMAIL (not sent — no provider / dev fallback) ────────\n"
        f"To:      {to}\nSubject: {subject}\n\n{body}\n"
        f"──────────────────────────────────────────────────────────────\n",
        flush=True,
    )
    return {"sent": False, "dev": True}


def send_email(to: str, subject: str, body: str, html: str | None = None) -> dict:
    """Send one email via the configured transport (Resend > SMTP), independent of DEV_MODE — real
    email works as soon as a provider is wired, without flipping the whole app out of dev mode. When
    NO provider is configured it prints to the console (so local dev stays self-serve — just don't set
    RESEND_API_KEY/SMTP_HOST locally). In dev mode a provider FAILURE (e.g. the Resend domain isn't
    verified yet) is tolerated and falls back to console so signup/OTP keeps working; production
    re-raises so a broken mail setup surfaces."""
    if not (settings.resend_api_key or settings.smtp_host):
        return _console(to, subject, body)
    try:
        if settings.resend_api_key:
            return _send_resend(to, subject, body, html)
        return _send_smtp(to, subject, body)
    except Exception:
        if not settings.dev_mode:
            raise                       # production: surface the failure (OTP delivery must be reliable)
        log.warning("email to %s failed in dev mode — falling back to console", to)
        return _console(to, subject, body)


def _best_effort(fn, *args, **kwargs) -> dict:
    """Run a notification send without letting a mail outage break the caller's transaction."""
    try:
        return fn(*args, **kwargs)
    except Exception as e:                            # noqa: BLE001 — notifications are non-critical
        log.error("email notification failed: %s", e)
        return {"sent": False, "error": str(e)}


def _html_email(heading: str, intro_html: str, *, extra_html: str = "",
                cta: tuple[str, str] | None = None) -> str:
    """Branded HTML wrapper (Vettalume header/footer, gold CTA). Table-based + inline styles for
    broad email-client support. `cta` = (label, url). Plain text is still sent as the fallback."""
    button = ""
    if cta:
        label, url = cta
        button = (f'<div style="margin:24px 0 6px;">'
                  f'<a href="{_esc(url)}" style="display:inline-block;background:#c9a24e;color:#1d1f24;'
                  f'font-weight:700;text-decoration:none;padding:12px 28px;border-radius:8px;'
                  f'font-size:15px;">{_esc(label)}</a></div>')
    return f"""<!doctype html>
<html><body style="margin:0;padding:0;background:#f2ecdd;">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background:#f2ecdd;font-family:Arial,Helvetica,sans-serif;padding:26px 12px;">
<tr><td align="center">
<table role="presentation" width="560" cellpadding="0" cellspacing="0" style="max-width:560px;width:100%;background:#ffffff;border-radius:16px;overflow:hidden;border:1px solid #e4ddc9;">
<tr><td style="background:#23262a;padding:20px 30px;">
<span style="font-size:21px;font-weight:800;color:#ffffff;letter-spacing:.3px;">Vetta<span style="color:#c9a24e;">lume</span></span>
</td></tr>
<tr><td style="padding:28px 30px 8px;">
<h1 style="margin:0 0 14px;font-size:21px;color:#1d1f24;">{heading}</h1>
<div style="font-size:15px;line-height:1.65;color:#4a4d54;">{intro_html}</div>
{extra_html}{button}
</td></tr>
<tr><td style="padding:20px 30px;border-top:1px solid #eee;font-size:12px;line-height:1.6;color:#8b8d92;">
{_esc(settings.public_name)} — Empowering Aspirants. Delivering Results.<br>
Questions? <a href="mailto:support@vettalume.com" style="color:#8a6d28;text-decoration:none;">support@vettalume.com</a>
&nbsp;·&nbsp;<a href="{_esc(settings.app_url)}" style="color:#8a6d28;text-decoration:none;">vettalume.com</a>
</td></tr>
</table></td></tr></table></body></html>"""


def send_otp(to: str, name: str, code: str) -> dict:
    """OTP delivery. Propagates transport errors so /signup and /resend-otp can report the failure."""
    mins = max(1, settings.otp_ttl_seconds // 60)
    body = (f"Hi {name},\n\n"
            f"Your {settings.public_name} verification code is:\n\n    {code}\n\n"
            f"It expires in {mins} minutes. If you didn't request this, you can ignore this email.\n")
    code_box = (f'<div style="margin:22px 0;text-align:center;">'
                f'<span style="display:inline-block;font-size:30px;font-weight:800;letter-spacing:8px;'
                f'color:#1d1f24;background:#f4ebd3;border:1px solid #e4ddc9;border-radius:10px;'
                f'padding:14px 26px;">{_esc(code)}</span></div>')
    html = _html_email(
        "Verify your email",
        f"Hi {_esc(name)}, use this code to verify your email and continue:",
        extra_html=code_box + f'<p style="font-size:13px;color:#8b8d92;margin:0;">This code expires in '
                              f'{mins} minutes. If you didn\'t request it, you can ignore this email.</p>')
    return send_email(to, f"Your {settings.public_name} verification code", body, html)


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
    bullets = (
        '<ul style="margin:14px 0;padding-left:20px;color:#4a4d54;font-size:14.5px;line-height:1.7;">'
        "<li>Comprehensive study material designed by experts</li>"
        "<li>Full-length mock tests and sectional quizzes</li>"
        "<li>Performance analytics to track your progress</li>"
        "<li>Personalized recommendations for your strengths &amp; weaknesses</li>"
        "</ul>"
        '<p style="font-size:14px;color:#4a4d54;margin:0 0 4px;"><b>Get started in 3 steps:</b> '
        "take the diagnostic, get your study plan, and begin.</p>"
    )
    html = _html_email(
        f"Welcome to {_esc(settings.public_name)}! 🎉",
        f"Dear {_esc(name)}, we're excited to have you. You've taken the first step toward your CAT / "
        f"GMAT / GRE goals — here's what you now have access to:",
        extra_html=bullets,
        cta=("Go to your dashboard", f"{settings.app_url}/dashboard"))
    return _best_effort(send_email, to, subject, body, html)


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
    access_rows = "".join(
        f'<tr><td style="padding:4px 0;color:#4a4d54;">{_esc(str(a.get("exam")))}</td>'
        f'<td style="padding:4px 0;text-align:right;color:#1d1f24;">until {_esc((a.get("expires_at") or "")[:10])}</td></tr>'
        for a in access) or '<tr><td style="padding:4px 0;color:#4a4d54;">Your access has been activated.</td></tr>'
    summary = (
        '<table role="presentation" width="100%" cellpadding="0" cellspacing="0" '
        'style="margin:16px 0;font-size:14.5px;background:#faf7ef;border:1px solid #e4ddc9;border-radius:10px;padding:14px 16px;">'
        f'<tr><td style="padding:4px 0;color:#8b8d92;">Plan</td><td style="padding:4px 0;text-align:right;font-weight:700;">{_esc(plan_name)}</td></tr>'
        f'<tr><td style="padding:4px 0;color:#8b8d92;">Amount</td><td style="padding:4px 0;text-align:right;font-weight:700;">{_esc(amount_str)}</td></tr>'
        f'<tr><td style="padding:4px 0;color:#8b8d92;">Duration</td><td style="padding:4px 0;text-align:right;">{_esc(term)}</td></tr>'
        + (f'<tr><td style="padding:4px 0;color:#8b8d92;">Payment&nbsp;ID</td><td style="padding:4px 0;text-align:right;font-size:12px;color:#8b8d92;">{_esc(payment_id)}</td></tr>' if payment_id else "")
        + '</table>'
        '<p style="font-size:14px;color:#4a4d54;margin:0 0 6px;"><b>What you can now access:</b></p>'
        '<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="font-size:14px;">'
        f'{access_rows}</table>'
    )
    html = _html_email(
        "Payment successful 🎉",
        f"Hi {_esc(name)}, thank you for your purchase — your payment was successful and your access is now active.",
        extra_html=summary,
        cta=("Go to your dashboard", f"{settings.app_url}/dashboard"))
    return _best_effort(send_email, to, f"Payment confirmed — welcome to {plan_name}", body, html)
