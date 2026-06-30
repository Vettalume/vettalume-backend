"""The production fail-fast guard: with ENVIRONMENT=production the app must refuse insecure defaults
(forgeable JWT secret, dev-login on, X-Learner-Id on, wildcard CORS) and accept a hardened config."""
from app.config import Settings


def test_development_is_never_blocked():
    assert Settings().production_problems() == []


def test_production_rejects_all_insecure_defaults():
    problems = Settings(environment="production").production_problems()
    joined = " ".join(problems)
    assert "JWT_SECRET" in joined
    assert "DEV_MODE" in joined
    assert "REQUIRE_JWT" in joined
    assert "CORS_ORIGINS" in joined
    assert len(problems) == 4


def test_production_accepts_hardened_config():
    s = Settings(environment="production", jwt_secret="x" * 40, dev_mode=False,
                 require_jwt=True, cors_origins="https://app.vettalume.com,https://vettalume.com")
    assert s.production_problems() == []
    assert s.cors_origins_list == ["https://app.vettalume.com", "https://vettalume.com"]


def test_short_jwt_secret_is_rejected_in_production():
    s = Settings(environment="production", jwt_secret="too-short", dev_mode=False,
                 require_jwt=True, cors_origins="https://app.vettalume.com")
    assert any("JWT_SECRET" in p for p in s.production_problems())
