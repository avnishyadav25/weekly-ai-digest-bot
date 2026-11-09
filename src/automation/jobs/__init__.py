"""Job discovery: every ``Job`` subclass with a ``name`` in this package, plus the modules in ``job_modules``."""

from __future__ import annotations

import importlib
import pkgutil
from collections.abc import Iterable
from types import ModuleType

from automation.jobs.base import Context, Job, JobConfig, JobFailed, Result

__all__ = ["Context", "Job", "JobConfig", "JobFailed", "Result", "discover_jobs", "jobs_in_module"]


def jobs_in_module(module: ModuleType) -> list[type[Job]]:
    found = []
    for obj in vars(module).values():
        if (
            isinstance(obj, type)
            and issubclass(obj, Job)
            and obj is not Job
            and obj.__module__ == module.__name__
            and isinstance(getattr(obj, "name", None), str)
        ):
            found.append(obj)
    return found


def discover_jobs(extra_modules: Iterable[str] = ()) -> dict[str, type[Job]]:
    """Import the job modules and return ``{job name: job class}``, sorted by name."""
    modules = [
        f"{__name__}.{info.name}" for info in pkgutil.iter_modules(__path__) if info.name not in {"base"}
    ]
    registry: dict[str, type[Job]] = {}
    for module_name in [*modules, *extra_modules]:
        for job in jobs_in_module(importlib.import_module(module_name)):
            if job.name in registry and registry[job.name] is not job:
                raise ValueError(
                    f"two jobs are named {job.name!r}: {registry[job.name].__module__} and {module_name}"
                )
            registry[job.name] = job
    return dict(sorted(registry.items()))
