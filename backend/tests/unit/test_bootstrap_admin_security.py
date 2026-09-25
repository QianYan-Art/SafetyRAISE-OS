from contextlib import contextmanager
from types import SimpleNamespace

import pytest

from app.core.settings import DEFAULT_JWT_SECRET, AuthSettings
from app.services.auth_service import AuthService

STRONG_SECRET = "a" * 64


@pytest.mark.parametrize(
    ("secret", "password", "expected"),
    [
        (DEFAULT_JWT_SECRET, "", ["AUTH_JWT_SECRET", "BOOTSTRAP_ADMIN_PASSWORD"]),
        (STRONG_SECRET, "SafetyRaise@2026", ["BOOTSTRAP_ADMIN_PASSWORD"]),
        (STRONG_SECRET, "short1", ["BOOTSTRAP_ADMIN_PASSWORD"]),
        ("", "a-private-password", ["AUTH_JWT_SECRET"]),
        (STRONG_SECRET, "a-private-password", []),
    ],
)
def test_production_profile_rejects_public_or_missing_credentials(secret, password, expected):
    settings = AuthSettings(jwt_secret=secret, bootstrap_admin_password=password, require_strong_secret=True)
    problems = settings.startup_security_problems()
    assert [name for name in ("AUTH_JWT_SECRET", "BOOTSTRAP_ADMIN_PASSWORD") if any(name in p for p in problems)] == expected


def test_development_profile_does_not_block_startup():
    settings = AuthSettings(require_strong_secret=False)
    assert settings.bootstrap_admin_password == ""
    assert settings.startup_security_problems() == []


class RecordingCursor:
    def __init__(self, existing):
        self.existing = existing
        self.statements = []

    def execute(self, sql, params=None):
        self.statements.append(" ".join(sql.split()))

    def fetchone(self):
        return {"id": "admin"} if self.existing else None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class RecordingDatabase:
    def __init__(self, existing=False):
        self.cursor_obj = RecordingCursor(existing)
        self.commits = 0

    @contextmanager
    def connection(self):
        db = self

        class Connection:
            def cursor(self):
                return db.cursor_obj

            def commit(self):
                db.commits += 1

        yield Connection()


def make_service(password, database):
    settings = SimpleNamespace(auth=AuthSettings(jwt_secret=STRONG_SECRET, bootstrap_admin_password=password))
    return AuthService(settings, database)


def test_missing_password_skips_bootstrap_admin_instead_of_creating_one():
    database = RecordingDatabase()
    make_service("", database)
    assert not any(s.startswith("insert into users") for s in database.cursor_obj.statements)
    assert database.commits == 0


def test_configured_password_creates_bootstrap_admin_once():
    database = RecordingDatabase()
    make_service("a-private-password", database)
    assert any(s.startswith("insert into users") for s in database.cursor_obj.statements)
    assert database.commits == 1

    existing = RecordingDatabase(existing=True)
    make_service("a-private-password", existing)
    assert not any(s.startswith("insert into users") for s in existing.cursor_obj.statements)
