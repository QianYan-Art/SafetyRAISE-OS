import pytest

from app.report_harness.execution import production_dependencies
from app.report_harness.test_database import validate_test_dsn


@pytest.mark.parametrize("dsn", [
    "",
    "host=example.com dbname=safetyraise_harness_test",
    "host=127.0.0.1 dbname=production",
    "host=127.0.0.1 dbname=safetyraise_harness_test hostaddr=8.8.8.8",
    "host=127.0.0.1 dbname=safetyraise_harness_test service=production",
])
def test_unsafe_test_database_is_rejected(dsn):
    with pytest.raises(ValueError):
        validate_test_dsn(dsn)


def test_loopback_test_database_is_allowed():
    assert validate_test_dsn("host=127.0.0.1 dbname=safetyraise_harness_test")


def test_database_address_cannot_be_overridden_by_environment(monkeypatch):
    monkeypatch.setenv("PGHOSTADDR", "203.0.113.1")
    from psycopg.conninfo import conninfo_to_dict
    result = conninfo_to_dict(validate_test_dsn("host=localhost dbname=safetyraise_harness_test"))
    assert result["hostaddr"] == "127.0.0.1"


@pytest.mark.parametrize("config", [
    {"execution_profile": "synthetic_test"},
    {"test_release_bindings": [{"approved": True}]},
    {"online_enabled": True},
])
def test_production_cannot_enable_unvalidated_execution(config):
    with pytest.raises(ValueError):
        production_dependencies(config)


def test_production_defaults_to_disabled():
    assert production_dependencies({}) is None


def test_data_only_configuration_does_not_enable_outbound():
    assert production_dependencies({"enabled": True, "online_enabled": False}) is None
