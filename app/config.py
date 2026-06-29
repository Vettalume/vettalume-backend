from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    app_name: str = "Vettalume Backend"
    # Local dev runs on SQLite with zero config. Docker/prod set DATABASE_URL to Postgres explicitly
    # (see docker-compose.yml), so this default never reaches a real deployment.
    database_url: str = "sqlite+pysqlite:///./vettalume.db"
    redis_url: str = "redis://localhost:6379/0"  # wired for later phases; unused in Phase 0
    dev_mode: bool = True
    serve_only_approved: bool = True   # False (SERVE_ONLY_APPROVED=false) -> serve drafts too (testing)
    zpd_use_prereqs: bool = True       # False (ZPD_USE_PREREQS=false) -> ZPD ignores prerequisites
    enforce_entitlements: bool = False  # True -> billing guards bite (paid + free-tier limits); off keeps the demo open
    jwt_secret: str = "dev-insecure-change-me"   # MUST be overridden in production (env JWT_SECRET)
    jwt_expiry_seconds: int = 604800             # 7 days
    require_jwt: bool = False                    # True -> only Bearer JWT accepted (legacy X-Learner-Id disabled)
    admin_emails: str = ""                       # comma-separated admin emails (env ADMIN_EMAILS). Empty = secure default; bootstrap via scripts/create_admin.py

    # --- Razorpay (env: RAZORPAY_KEY_ID / RAZORPAY_KEY_SECRET / RAZORPAY_WEBHOOK_SECRET) ---
    # Use TEST keys (rzp_test_...) for development; LIVE keys (rzp_live_...) only in production.
    # Empty key_id => payment endpoints return a clear "not configured" error instead of crashing.
    razorpay_key_id: str = ""
    razorpay_key_secret: str = ""
    razorpay_webhook_secret: str = ""

    # --- student auth: sliding sessions, email OTP, Google sign-in ---
    session_inactivity_days: int = 2            # auto-logout after this many idle days (sliding window)
    otp_ttl_seconds: int = 600                  # an OTP code is valid for 10 minutes
    otp_resend_cooldown_seconds: int = 30       # minimum gap between OTP sends (the 30s resend timer)
    otp_max_attempts: int = 5                   # wrong tries before a code is burned

    # SMTP (env SMTP_HOST / SMTP_PORT / SMTP_USER / SMTP_PASSWORD / SMTP_FROM).
    # Empty host => DEV MODE: emails (including the OTP) are printed to the server console instead of
    # sent, so the whole signup flow is testable locally with no mail provider.
    smtp_host: str = ""
    smtp_port: int = 587
    smtp_user: str = ""
    smtp_password: str = ""
    smtp_from: str = "Vettalume <no-reply@vettalume.com>"
    smtp_starttls: bool = True

    # Google sign-in (env GOOGLE_CLIENT_ID). Empty => /auth/google returns a clear "not configured".
    google_client_id: str = ""

    public_name: str = "Vettalume"
    terms_url: str = "https://vettalume.com/terms"

    # --- connection pool (used for Postgres/any server DB; ignored for SQLite) ---
    # Total DB connections at peak ~= (web workers) x (db_pool_size + db_max_overflow). Keep that
    # under Postgres max_connections, or put pgbouncer in front to multiplex.
    db_pool_size: int = 5           # persistent connections kept open per worker process
    db_max_overflow: int = 10       # extra burst connections per worker beyond pool_size
    db_pool_timeout: int = 30       # seconds a request waits for a free connection before erroring
    db_pool_recycle: int = 1800     # recycle a connection after N seconds (avoids stale/closed sockets)


settings = Settings()
