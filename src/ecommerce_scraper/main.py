from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from ecommerce_scraper import __version__
from ecommerce_scraper.api import router
from ecommerce_scraper.config import get_settings
from ecommerce_scraper.jobs import InMemoryJobManager
from ecommerce_scraper.service import ScrapeService


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()
    app.state.job_manager = InMemoryJobManager(ScrapeService(settings))
    yield


app = FastAPI(
    title="E-commerce Scraper API",
    description="Discover public e-commerce categories and sample normalized products.",
    version=__version__,
    lifespan=lifespan,
)
app.include_router(router)


@app.get("/")
async def root() -> dict[str, str]:
    return {
        "name": "ecommerce-scraper",
        "version": __version__,
        "docs": "/docs",
        "health": "/api/v1/health",
    }
