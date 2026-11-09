"""The Job base class, its config model, the run context and the result.

A job is a class with a ``name`` and a ``run(ctx)`` method; drop the module into ``automation/jobs/`` (or list
its module in ``job_modules``) and it is discovered. See docs/writing-a-job.md.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING, Any, ClassVar, Generic, TypeVar

from pydantic import BaseModel, ConfigDict, Field, field_validator

from automation.config import check_channels

if TYPE_CHECKING:
    from automation.config import Secrets, Settings
    from automation.http import HttpClient
    from automation.llm import LlmClient
    from automation.notify.base import Dispatcher
    from automation.state import JobState


class JobConfig(BaseModel):
    """Settings every job has. Subclass it to add your job's own settings; unknown keys are errors."""

    model_config = ConfigDict(extra="forbid")

    enabled: bool = True
    schedule: str | None = Field(
        default=None, description="cron expression (5 fields); overrides the default"
    )
    notify: list[str] | None = Field(default=None, description="channels for this job's messages")
    max_runtime_minutes: int = Field(
        default=60, gt=0, description="run lock lifetime; see docs/reliability.md"
    )

    @field_validator("notify")
    @classmethod
    def _known_channels(cls, value: list[str] | None) -> list[str] | None:
        return None if value is None else check_channels(value)


C = TypeVar("C", bound=JobConfig)


@dataclass
class Context(Generic[C]):
    """Everything a run needs. Jobs get it as ``ctx``."""

    job: str
    run_id: str
    config: C
    settings: Settings
    secrets: Secrets
    http: HttpClient
    state: JobState
    notify: Dispatcher
    log: logging.Logger
    now: datetime
    dry_run: bool = False
    llm: LlmClient | None = None


@dataclass
class Result:
    """What a run did, in one line (``summary``) plus any numbers worth keeping (``data``)."""

    summary: str = ""
    data: dict[str, Any] = field(default_factory=dict)


class JobFailed(Exception):
    """Raise it to fail a run with a clear message (no traceback in the alert)."""


class Job(Generic[C]):
    name: ClassVar[str]
    description: ClassVar[str] = ""
    schedule: ClassVar[str | None] = None  # default cron expression; [jobs.<name>] schedule overrides it
    config_model: ClassVar[type[JobConfig]] = JobConfig

    def run(self, ctx: Context[C]) -> Result:
        raise NotImplementedError
