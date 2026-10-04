import pytest
from pydantic import ValidationError

from config import Settings

TRACING_VARS = ["TRACING_ENABLED", "TRACING_SAMPLE_RATE", "LANGFUSE_SECRET_KEY"]


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    # Keep the developer's shell and .env out of these tests.
    for name in TRACING_VARS:
        monkeypatch.delenv(name, raising=False)


def load(**env) -> Settings:
    return Settings(_env_file=None, **env)


def test_tracing_is_off_by_default():
    settings = load()
    assert settings.tracing_enabled is False
    assert settings.tracing_sample_rate == 1.0


def test_tracing_turns_on_only_when_set_explicitly(monkeypatch):
    monkeypatch.setenv("TRACING_ENABLED", "true")
    monkeypatch.setenv("TRACING_SAMPLE_RATE", "0.1")
    settings = load()
    assert settings.tracing_enabled is True
    assert settings.tracing_sample_rate == 0.1


@pytest.mark.parametrize("rate", ["1.5", "-0.1"])
def test_sample_rate_outside_0_to_1_is_rejected(monkeypatch, rate):
    monkeypatch.setenv("TRACING_SAMPLE_RATE", rate)
    with pytest.raises(ValidationError):
        load()


def test_secret_keys_do_not_leak_in_repr(monkeypatch):
    monkeypatch.setenv("LANGFUSE_SECRET_KEY", "sk-lf-should-not-appear")
    settings = load()
    assert "sk-lf-should-not-appear" not in repr(settings)
    assert settings.langfuse_secret_key.get_secret_value() == "sk-lf-should-not-appear"
