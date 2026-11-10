"""Settings: non-secret settings from a TOML file, secrets and overrides from the environment.

Precedence, highest first: environment variables, the ``.env`` file in the working directory, the TOML file,
the defaults below. Secrets (webhook URLs, passwords, tokens, API keys) are read only from the environment or
``.env``; a secret-looking key in the TOML file is an error, so it cannot end up in version control by accident.

Relative paths in the TOML file (state path, fixtures folder, CSV sources) are relative to the directory you run
``automation`` from.
"""

from __future__ import annotations

import re
import tomllib
from collections.abc import Iterator
from contextvars import ContextVar
from pathlib import Path
from typing import Any, Literal

from pydantic import (
    AliasChoices,
    BaseModel,
    ConfigDict,
    Field,
    PrivateAttr,
    SecretStr,
    ValidationError,
    field_validator,
)
from pydantic_settings import (
    BaseSettings,
    PydanticBaseSettingsSource,
    SettingsConfigDict,
    TomlConfigSettingsSource,
)

# Environment variables read in this module. .env.example lists every one of them.
CONFIG_ENV = "AUTOMATION_CONFIG"  # path of the TOML file (default: ./config.toml when it exists)
ENV_PREFIX = "AUTOMATION_"  # AUTOMATION_<SECTION>__<KEY> overrides a TOML value, e.g. AUTOMATION_LOG__LEVEL
OVERRIDE_EXAMPLES = ("AUTOMATION_LOG__LEVEL", "AUTOMATION_LOG__FORMAT", "AUTOMATION_STATE__PATH")
SLACK_WEBHOOK_URL = "SLACK_WEBHOOK_URL"
SMTP_PASSWORD = "SMTP_PASSWORD"  # noqa: S105  (the name of a variable, not a password)
GITHUB_TOKEN = "GITHUB_TOKEN"  # noqa: S105  (the name of a variable, not a token)
ANTHROPIC_API_KEY = "ANTHROPIC_API_KEY"
OPENAI_API_KEY = "OPENAI_API_KEY"
DEEPSEEK_API_KEY = "DEEPSEEK_API_KEY"
# The names avnishyadav.com uses for its Gmail fallback: EMAIL_PASS works as SMTP_PASSWORD, and EMAIL_USER fills in
# [notify.email] username, sender and recipients when the config leaves them empty.
EMAIL_USER = "EMAIL_USER"
EMAIL_PASS = "EMAIL_PASS"  # noqa: S105  (the name of a variable, not a password)

DEFAULT_CONFIG_FILE = Path("config.toml")
DEFAULT_ENV_FILE = Path(".env")
CHANNELS = ("console", "slack", "email")

# Keys that must never be in the TOML file: the key's last segment is one of these words or ends with "_<word>".
_SECRET_KEY = re.compile(
    r"^(?:.*[_-])?(password|passwd|pass|secret|secret_?key|token|api_?key|webhook|webhook_?url|private_?key)$",
    re.IGNORECASE,
)

_toml_file: ContextVar[Path | None] = ContextVar("automation_toml_file", default=None)


class ConfigError(Exception):
    """The configuration is missing, unreadable or invalid. The message says which file and key."""


class _Section(BaseModel):
    model_config = ConfigDict(extra="forbid")


class LogSettings(_Section):
    level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"
    format: Literal["json", "text"] = "json"

    @field_validator("level", mode="before")
    @classmethod
    def _upper(cls, value: Any) -> Any:
        return value.upper() if isinstance(value, str) else value


class StateSettings(_Section):
    path: Path = Path("state/automation.db")


class HttpSettings(_Section):
    timeout_seconds: float = Field(default=10.0, gt=0)
    retries: int = Field(default=3, ge=0, le=10)
    backoff_seconds: float = Field(default=0.5, ge=0)
    backoff_max_seconds: float = Field(default=30.0, ge=0)
    user_agent: str = "python-automation/1.0"


class EmailSettings(_Section):
    host: str | None = None
    port: int = 587
    security: Literal["starttls", "ssl", "none"] = "starttls"
    username: str | None = None
    sender: str | None = None
    recipients: list[str] = Field(default_factory=list)
    timeout_seconds: float = Field(default=30.0, gt=0)


class NotifySettings(_Section):
    default: list[str] = Field(default_factory=lambda: ["console"])
    on_failure: list[str] = Field(default_factory=lambda: ["console"])
    repeat_failure_alert_minutes: int = Field(default=60, ge=0)
    email: EmailSettings = Field(default_factory=EmailSettings)

    @field_validator("default", "on_failure")
    @classmethod
    def _known_channels(cls, value: list[str]) -> list[str]:
        return check_channels(value)


class LlmSettings(_Section):
    provider: Literal["anthropic", "openai", "deepseek"] | None = None
    model: str | None = None
    max_tokens: int = Field(default=16000, gt=0)
    effort: Literal["low", "medium", "high", "xhigh", "max", "none"] = "low"  # "none": leave it out
    fallbacks: bool = True
    timeout_seconds: float = Field(default=120.0, gt=0)


class Settings(BaseSettings):
    """Everything except secrets. Build it with :func:`load_settings`."""

    model_config = SettingsConfigDict(
        env_prefix=ENV_PREFIX,
        env_nested_delimiter="__",
        env_file=DEFAULT_ENV_FILE,
        env_ignore_empty=True,
        extra="ignore",
    )

    timezone: str = "UTC"
    fixtures_dir: Path = Path("examples/fixtures")
    job_modules: list[str] = Field(default_factory=list)
    log: LogSettings = Field(default_factory=LogSettings)
    state: StateSettings = Field(default_factory=StateSettings)
    http: HttpSettings = Field(default_factory=HttpSettings)
    notify: NotifySettings = Field(default_factory=NotifySettings)
    llm: LlmSettings = Field(default_factory=LlmSettings)
    jobs: dict[str, dict[str, Any]] = Field(default_factory=dict)

    _config_file: Path | None = PrivateAttr(default=None)

    @property
    def config_file(self) -> Path | None:
        """The TOML file these settings were loaded from, or None when only defaults and env were used."""
        return self._config_file

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        sources: list[PydanticBaseSettingsSource] = [init_settings, env_settings, dotenv_settings]
        toml_file = _toml_file.get()
        if toml_file is not None:
            sources.append(TomlConfigSettingsSource(settings_cls, toml_file=toml_file))
        return tuple(sources)

    @field_validator("timezone")
    @classmethod
    def _known_timezone(cls, value: str) -> str:
        from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ValueError(
                f"unknown time zone {value!r} (use an IANA name such as 'Europe/London')"
            ) from exc
        return value


class Secrets(BaseSettings):
    """Secrets, read only from the environment or ``.env``. Values are ``SecretStr`` and never printed."""

    model_config = SettingsConfigDict(env_file=DEFAULT_ENV_FILE, env_ignore_empty=True, extra="ignore")

    slack_webhook_url: SecretStr | None = Field(default=None, validation_alias=SLACK_WEBHOOK_URL)
    smtp_password: SecretStr | None = Field(
        default=None, validation_alias=AliasChoices(SMTP_PASSWORD, EMAIL_PASS)
    )
    email_user: str | None = Field(default=None, validation_alias=EMAIL_USER)
    github_token: SecretStr | None = Field(default=None, validation_alias=GITHUB_TOKEN)
    anthropic_api_key: SecretStr | None = Field(default=None, validation_alias=ANTHROPIC_API_KEY)
    openai_api_key: SecretStr | None = Field(default=None, validation_alias=OPENAI_API_KEY)
    deepseek_api_key: SecretStr | None = Field(default=None, validation_alias=DEEPSEEK_API_KEY)


class _Locator(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix=ENV_PREFIX, env_file=DEFAULT_ENV_FILE, env_ignore_empty=True, extra="ignore"
    )

    config: Path | None = None  # read from AUTOMATION_CONFIG


def check_channels(channels: list[str]) -> list[str]:
    unknown = [c for c in channels if c not in CHANNELS]
    if unknown:
        raise ValueError(f"unknown channel(s) {', '.join(unknown)}; use {', '.join(CHANNELS)}")
    return channels


def find_config_file(explicit: Path | None = None, env_file: Path | None = DEFAULT_ENV_FILE) -> Path | None:
    """The TOML file to load: ``--config``, else ``AUTOMATION_CONFIG``, else ``./config.toml`` if present."""
    if explicit is not None:
        if not explicit.is_file():
            raise ConfigError(f"config file not found: {explicit}")
        return explicit
    located = _Locator(_env_file=env_file).config
    if located is not None:
        if not located.is_file():
            raise ConfigError(f"config file not found: {located} (from {CONFIG_ENV})")
        return located
    return DEFAULT_CONFIG_FILE if DEFAULT_CONFIG_FILE.is_file() else None


def read_toml(path: Path) -> dict[str, Any]:
    try:
        with path.open("rb") as handle:
            return tomllib.load(handle)
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"{path}: invalid TOML: {exc}") from exc
    except OSError as exc:
        raise ConfigError(f"{path}: cannot read: {exc}") from exc


def _walk_keys(data: Any, prefix: str = "") -> Iterator[str]:
    if isinstance(data, dict):
        for key, value in data.items():
            dotted = f"{prefix}{key}"
            yield dotted
            yield from _walk_keys(value, f"{dotted}.")


def check_toml(data: dict[str, Any], path: Path) -> None:
    """Reject secret-looking keys and unknown top-level keys before pydantic sees the file."""
    secret_keys = [key for key in _walk_keys(data) if _SECRET_KEY.search(key.rsplit(".", 1)[-1])]
    if secret_keys:
        raise ConfigError(
            f"{path}: {', '.join(secret_keys)}: secrets do not belong in the config file; "
            "set them as environment variables (see .env.example)"
        )
    known = set(Settings.model_fields)
    unknown = sorted(set(data) - known)
    if unknown:
        raise ConfigError(
            f"{path}: unknown setting(s) {', '.join(unknown)}; known: {', '.join(sorted(known))}"
        )


def format_validation_error(exc: ValidationError, source: str) -> str:
    lines = [f"{source}: invalid configuration"]
    for err in exc.errors():
        where = ".".join(str(part) for part in err["loc"]) or "(top level)"
        lines.append(f"  {where}: {err['msg']}")
    return "\n".join(lines)


def load_settings(config_file: Path | None = None, *, env_file: Path | None = DEFAULT_ENV_FILE) -> Settings:
    """Load settings from the TOML file, ``.env`` and the environment. Raises :class:`ConfigError`."""
    path = find_config_file(config_file, env_file)
    if path is not None:
        check_toml(read_toml(path), path)
    token = _toml_file.set(path)
    try:
        settings = Settings(_env_file=env_file)
    except ValidationError as exc:
        raise ConfigError(format_validation_error(exc, str(path or "settings"))) from exc
    finally:
        _toml_file.reset(token)
    settings._config_file = path
    return settings


def load_secrets(*, env_file: Path | None = DEFAULT_ENV_FILE) -> Secrets:
    return Secrets(_env_file=env_file)
