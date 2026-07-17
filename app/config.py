from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    app_name: str = "Vettalume Backend"
    # "development" (default, dev-friendly) or "production". In production the app fail-fast refuses to
    # boot with insecure defaults (see production_problems()), so a misconfigured deploy never serves
    # traffic with a forgeable JWT secret or the spoofable X-Learner-Id header enabled.
    environment: str = "development"
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
    # Idle (sliding) window: no activity for this long -> auto-logout (covers sleep / 24h away).
    session_inactivity_days: int = 1
    # Absolute cap from login: you must sign in again after this many days no matter how active.
    session_max_days: int = 7
    otp_ttl_seconds: int = 600                  # an OTP code is valid for 10 minutes
    otp_resend_cooldown_seconds: int = 30       # minimum gap between OTP sends (the 30s resend timer)
    otp_max_attempts: int = 5                   # wrong tries before a code is burned

    # Email transport, best → fallback (see services/email.py):
    #   1. Resend  — set RESEND_API_KEY (production path; HTTP API, no SMTP egress needed).
    #   2. SMTP    — set SMTP_HOST (any SMTP provider / self-hosted).
    #   3. neither — DEV MODE: emails (including the OTP) are printed to the server console so the
    #                whole signup flow is testable locally with no mail provider.
    # RESEND_API_KEY is a re_... key from resend.com. The sender domain must be verified in Resend;
    # for a quick test with no domain, use MAIL_FROM="Vettalume <onboarding@resend.dev>".
    resend_api_key: str = ""
    smtp_host: str = ""
    smtp_port: int = 587
    smtp_user: str = ""
    smtp_password: str = ""
    smtp_from: str = "Vettalume <no-reply@vettalume.com>"   # legacy alias; MAIL_FROM overrides it
    mail_from_addr: str = Field(default="", validation_alias="MAIL_FROM")  # preferred. Empty => smtp_from
    smtp_starttls: bool = True

    @property
    def mail_from(self) -> str:
        """The From header used by every transport. MAIL_FROM wins; otherwise the legacy SMTP_FROM."""
        return self.mail_from_addr or self.smtp_from

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
    # Pre-ping does a "SELECT 1" on every connection checkout to catch dead sockets (e.g. after Neon
    # free-tier auto-suspend) — safe but adds one round trip per request. Turn OFF only once the app and
    # DB are co-located (round trip ~5ms) or the DB no longer auto-suspends.
    db_pool_pre_ping: bool = True

    # /learn/overview caches the static per-exam shape (sections/chapters/concepts + item lists) this
    # long, so each request only fetches the caller's own state. Admin content edits appear after at
    # most this many seconds. Set 0 to disable the cache.
    overview_cache_ttl_seconds: int = 60

    # CORS allowed origins, comma-separated (env CORS_ORIGINS). "*" is convenient for dev but is rejected
    # in production by production_problems() because "*" + allow_credentials is both invalid (browsers
    # block it) and unsafe. Set e.g. CORS_ORIGINS="https://app.vettalume.com,https://vettalume.com".
    cors_origins: str = "*"

    @property
    def is_production(self) -> bool:
        return self.environment.strip().lower() == "production"

    @property
    def cors_origins_list(self) -> list[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]

    def production_problems(self) -> list[str]:
        """Insecure settings that must NOT ship to production. Empty list == safe to boot. Checked at
        startup (main.lifespan) so a misconfigured production deploy fails fast instead of serving."""
        problems: list[str] = []
        if not self.is_production:
            return problems
        if self.jwt_secret == "dev-insecure-change-me" or len(self.jwt_secret) < 32:
            problems.append("JWT_SECRET must be set to a strong random value (>=32 chars), not the dev default")
        if self.dev_mode:
            problems.append("DEV_MODE must be false (it enables passwordless /auth/dev-login)")
        if not self.require_jwt:
            problems.append("REQUIRE_JWT must be true (else the spoofable X-Learner-Id header impersonates any account)")
        if "*" in self.cors_origins_list:
            problems.append("CORS_ORIGINS must list explicit origins, not '*' (incompatible with credentialed requests and unsafe)")
        return problems


settings = Settings()
