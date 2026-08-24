from __future__ import annotations

import asyncio
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request, status

from ecommerce_scraper.jobs import InMemoryJobManager, JobNotFoundError
from ecommerce_scraper.models import Job, JobAccepted, JobState, ScrapeRequest, ScrapeResult

router = APIRouter(prefix="/api/v1")


def get_job_manager(request: Request) -> InMemoryJobManager:
    manager = request.app.state.job_manager
    if not isinstance(manager, InMemoryJobManager):
        raise RuntimeError("Job manager is not initialized")
    return manager


JobManagerDependency = Annotated[InMemoryJobManager, Depends(get_job_manager)]


@router.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@router.post("/jobs", response_model=JobAccepted, status_code=status.HTTP_202_ACCEPTED)
async def create_job(
    payload: ScrapeRequest,
    manager: JobManagerDependency,
) -> JobAccepted:
    job = await manager.create(payload)
    asyncio.create_task(manager.run(job.id), name=f"scrape-job-{job.id}")
    return JobAccepted(id=job.id, state=job.state, status_url=f"/api/v1/jobs/{job.id}")


@router.get("/jobs/{job_id}", response_model=Job)
async def get_job(
    job_id: str,
    manager: JobManagerDependency,
) -> Job:
    try:
        return await manager.get(job_id)
    except JobNotFoundError as exc:
        raise HTTPException(status_code=404, detail="Job not found") from exc


@router.get("/jobs/{job_id}/result", response_model=ScrapeResult)
async def get_job_result(
    job_id: str,
    manager: JobManagerDependency,
) -> ScrapeResult:
    try:
        job = await manager.get(job_id)
    except JobNotFoundError as exc:
        raise HTTPException(status_code=404, detail="Job not found") from exc
    if job.state == JobState.FAILED:
        raise HTTPException(status_code=422, detail=job.error or "Scrape job failed")
    if job.state != JobState.COMPLETED or not job.result:
        raise HTTPException(status_code=409, detail=f"Job is {job.state.value}")
    return job.result
