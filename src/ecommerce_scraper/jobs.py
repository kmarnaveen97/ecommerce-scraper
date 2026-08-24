from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from uuid import uuid4

from ecommerce_scraper.http_client import TargetAccessError
from ecommerce_scraper.models import Job, JobState, ScrapeRequest
from ecommerce_scraper.service import ScrapeService


class JobNotFoundError(KeyError):
    pass


class InMemoryJobManager:
    """MVP job manager. Replace with Redis/PostgreSQL before horizontal scaling."""

    def __init__(self, service: ScrapeService) -> None:
        self.service = service
        self._jobs: dict[str, Job] = {}
        self._lock = asyncio.Lock()

    async def create(self, request: ScrapeRequest) -> Job:
        job = Job(id=uuid4().hex, request=request)
        async with self._lock:
            self._jobs[job.id] = job
        return job.model_copy(deep=True)

    async def get(self, job_id: str) -> Job:
        async with self._lock:
            job = self._jobs.get(job_id)
            if not job:
                raise JobNotFoundError(job_id)
            return job.model_copy(deep=True)

    async def run(self, job_id: str) -> None:
        async with self._lock:
            job = self._jobs[job_id]
            job.state = JobState.RUNNING
            job.updated_at = datetime.now(UTC)
            request = job.request
        try:
            result = await self.service.scrape(request)
        except Exception as exc:
            async with self._lock:
                job = self._jobs[job_id]
                job.state = JobState.FAILED
                job.error = f"{type(exc).__name__}: {exc}"
                if isinstance(exc, TargetAccessError):
                    job.access_report = exc.report
                job.updated_at = datetime.now(UTC)
            return
        async with self._lock:
            job = self._jobs[job_id]
            job.state = JobState.COMPLETED
            job.result = result
            job.updated_at = datetime.now(UTC)
