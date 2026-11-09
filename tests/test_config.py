from __future__ import annotations

from pathlib import Path

import pytest

from automation.config import ConfigError, Secrets, load_secrets, load_settings
from automation.jobs import discover_jobs
from automation.runner import validate_section
from tests.conftest import EXAMPLE_CONFIG


def test_defaults_without_a_config_file():
    settings = load_settings(env_file=None)
    assert settings.config_file is None
    assert settings.timezone == "UTC"
    assert settings.notify.default == ["console"]
    assert settings.http.retries == 3
    assert settings.state.path == Path("state/automation.db")
    assert settings.jobs == {}


def test_config_toml_in_the_working_directory_is_found(write_config):
    write_config('timezone = "Europe/London"\n')
    settings = load_settings(env_file=None)
    assert settings.config_file == Path("config.toml")
    assert settings.timezone == "Europe/London"


def test_example_config_is_valid_for_every_job():
    settings = load_settings(EXAMPLE_CONFIG, env_file=None)
    registry = discover_jobs()
    assert set(settings.jobs) == set(registry)
    for name, job in registry.items():
        cfg = validate_section(job, settings, name)
        assert cfg.enabled


def test_environment_overrides_toml_and_keeps_the_rest(write_config, monkeypatch):
    path = write_config('[log]\nlevel = "WARNING"\nformat = "text"\n')
    monkeypatch.setenv("AUTOMATION_LOG__LEVEL", "debug")
    settings = load_settings(path, env_file=None)
    assert settings.log.level == "DEBUG"
    assert settings.log.format == "text"


def test_dotenv_file_supplies_config_path_and_secrets(tmp_path, write_config):
    write_config('timezone = "Asia/Kolkata"\n', name="other.toml")
    (tmp_path / ".env").write_text(
        "AUTOMATION_CONFIG=other.toml\nSLACK_WEBHOOK_URL=https://example.com/hook\nSMTP_PASSWORD=\n",
        encoding="utf-8",
    )
    settings = load_settings()
    secrets = load_secrets()
    assert settings.config_file == Path("other.toml")
    assert settings.timezone == "Asia/Kolkata"
    assert secrets.slack_webhook_url is not None
    assert secrets.slack_webhook_url.get_secret_value() == "https://example.com/hook"
    assert secrets.smtp_password is None  # empty values count as unset


def test_secrets_are_never_printed(monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "value-that-must-not-leak")
    secrets = Secrets(_env_file=None)
    assert "value-that-must-not-leak" not in repr(secrets)
    assert "value-that-must-not-leak" not in str(secrets.model_dump())


@pytest.mark.parametrize(
    "toml",
    [
        '[notify]\nslack_webhook_url = "https://example.com"\n',
        '[notify.email]\npassword = "x"\n',
        '[jobs.github-weekly]\ngithub_token = "x"\n',
        '[llm]\napi_key = "x"\n',
    ],
)
def test_secret_keys_in_toml_are_rejected(write_config, toml):
    with pytest.raises(ConfigError, match="secrets do not belong in the config file"):
        load_settings(write_config(toml), env_file=None)


def test_max_tokens_is_not_mistaken_for_a_secret(write_config):
    settings = load_settings(write_config("[llm]\nmax_tokens = 2000\n"), env_file=None)
    assert settings.llm.max_tokens == 2000


@pytest.mark.parametrize(
    ("toml", "message"),
    [
        ("timezon = 'UTC'\n", "unknown setting"),
        ("[log]\nlevle = 'INFO'\n", "log.levle"),
        ("[notify]\ndefault = ['pager']\n", "unknown channel"),
        ("timezone = 'Mars/Olympus'\n", "unknown time zone"),
        ("[http]\nretries = -1\n", "http.retries"),
        ("not toml at all [\n", "invalid TOML"),
    ],
)
def test_invalid_config_names_the_problem(write_config, toml, message):
    with pytest.raises(ConfigError, match=message):
        load_settings(write_config(toml), env_file=None)


def test_missing_config_file_is_an_error(tmp_path):
    with pytest.raises(ConfigError, match="config file not found"):
        load_settings(tmp_path / "nope.toml", env_file=None)


def test_invalid_job_section_names_the_key(make_settings):
    settings = make_settings("[jobs.uptime-check]\nurls = []\nfrequency = 5\n")
    job = discover_jobs()["uptime-check"]
    with pytest.raises(ConfigError) as info:
        validate_section(job, settings, "uptime-check")
    assert "[jobs.uptime-check]" in str(info.value)
    assert "urls" in str(info.value)
    assert "frequency" in str(info.value)
