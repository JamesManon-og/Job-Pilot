from __future__ import annotations

from collections.abc import AsyncIterator

import httpx
import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from jobpilot.api.app import create_app
from jobpilot.api.deps import AppState
from jobpilot.config import get_settings
from jobpilot.database import create_session_factory
from jobpilot.database.repositories import ApplicationRepository, JobRepository, ResumeRepository
from jobpilot.domain import Application, ApplicationStatus, Job, JobSource, Resume, ResumeProfile


@pytest.fixture
async def client(engine: AsyncEngine) -> AsyncIterator[httpx.AsyncClient]:
    """ASGI test client wired to the temp-database engine (no network, no lifespan)."""
    app = create_app()
    state = AppState.__new__(AppState)
    state.settings = get_settings()
    state.engine = engine
    state.session_factory = create_session_factory(engine)
    app.state.jobpilot = state

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as http:
        yield http


async def seed_job(engine: AsyncEngine) -> Job:
    factory = create_session_factory(engine)
    async with factory() as session:
        job, _ = await JobRepository(session).upsert(
            Job(
                title="Frontend Engineer",
                company="Acme",
                application_url="https://a.com/1",
                source=JobSource.REMOTEOK,
                technologies=["react"],
            )
        )
        await session.commit()
    return job


class TestAPI:
    async def test_health(self, client: httpx.AsyncClient) -> None:
        response = await client.get("/health")
        assert response.status_code == 200
        assert response.json()["status"] == "ok"

    async def test_stats_empty_db(self, client: httpx.AsyncClient) -> None:
        response = await client.get("/api/stats")
        assert response.status_code == 200
        data = response.json()
        assert data["total_jobs"] == 0
        assert data["applications_by_status"] == {}

    async def test_jobs_list_and_search(
        self, client: httpx.AsyncClient, engine: AsyncEngine
    ) -> None:
        await seed_job(engine)
        assert (await client.get("/api/jobs")).json()[0]["title"] == "Frontend Engineer"
        assert (await client.get("/api/jobs", params={"search": "frontend"})).json() != []
        assert (await client.get("/api/jobs", params={"search": "zzz"})).json() == []

    async def test_job_detail_404(self, client: httpx.AsyncClient) -> None:
        assert (await client.get("/api/jobs/999")).status_code == 404

    async def test_matches_requires_active_resume(self, client: httpx.AsyncClient) -> None:
        assert (await client.get("/api/matches")).status_code == 409

    async def test_matches_with_resume(
        self, client: httpx.AsyncClient, engine: AsyncEngine
    ) -> None:
        await seed_job(engine)
        factory = create_session_factory(engine)
        async with factory() as session:
            await ResumeRepository(session).add(
                Resume(
                    version="v1",
                    file_path="/r.pdf",
                    profile=ResumeProfile(technologies=["react"]),
                    is_active=True,
                )
            )
            await session.commit()
        response = await client.get("/api/matches")
        assert response.status_code == 200
        assert response.json() == []  # no match results yet

    async def test_scrape_runs_empty(self, client: httpx.AsyncClient) -> None:
        assert (await client.get("/api/scrape-runs")).json() == []

    async def test_applications_empty(self, client: httpx.AsyncClient) -> None:
        assert (await client.get("/api/applications")).json() == []


async def seed_application(engine: AsyncEngine) -> tuple[int, int]:
    """Create a job + resume + pending_review application. Return (app_id, job_id)."""
    factory = create_session_factory(engine)
    async with factory() as session:
        job, _ = await JobRepository(session).upsert(
            Job(
                title="Backend Dev",
                company="TestCo",
                application_url="https://test.co/apply",
                source=JobSource.REMOTEOK,
            )
        )
        resume = await ResumeRepository(session).add(
            Resume(version="v1", file_path="/r.pdf", is_active=True)
        )
        await session.commit()

    assert job.id is not None and resume.id is not None
    async with factory() as session:
        app = await ApplicationRepository(session).create(
            Application(
                job_id=job.id,
                resume_id=resume.id,
                status=ApplicationStatus.PENDING_REVIEW,
                cover_letter="Dear hiring manager...",
                answers={"Why us?": "Great mission."},
            )
        )
        await session.commit()
    assert app.id is not None
    return app.id, job.id


class TestReviewAPI:
    async def test_review_queue_empty(self, client: httpx.AsyncClient) -> None:
        resp = await client.get("/api/review")
        assert resp.status_code == 200
        assert resp.json() == []

    async def test_review_queue_returns_pending(
        self, client: httpx.AsyncClient, engine: AsyncEngine
    ) -> None:
        app_id, _ = await seed_application(engine)
        resp = await client.get("/api/review")
        assert resp.status_code == 200
        items = resp.json()
        assert len(items) == 1
        assert items[0]["application_id"] == app_id
        assert items[0]["company"] == "TestCo"

    async def test_edit_materials(self, client: httpx.AsyncClient, engine: AsyncEngine) -> None:
        app_id, _ = await seed_application(engine)
        resp = await client.patch(
            f"/api/review/{app_id}",
            json={"cover_letter": "Updated letter."},
        )
        assert resp.status_code == 200
        assert resp.json()["status"] == "updated"

    async def test_reject_application(self, client: httpx.AsyncClient, engine: AsyncEngine) -> None:
        app_id, _ = await seed_application(engine)
        resp = await client.post(f"/api/review/{app_id}/reject")
        assert resp.status_code == 200
        assert resp.json()["status"] == "rejected"
        queue = (await client.get("/api/review")).json()
        assert len(queue) == 0

    async def test_approve_application(
        self, client: httpx.AsyncClient, engine: AsyncEngine
    ) -> None:
        app_id, _ = await seed_application(engine)
        resp = await client.post(f"/api/review/{app_id}/approve")
        assert resp.status_code == 200
        assert resp.json()["status"] == "approved"

    async def test_approve_already_rejected_fails(
        self, client: httpx.AsyncClient, engine: AsyncEngine
    ) -> None:
        app_id, _ = await seed_application(engine)
        await client.post(f"/api/review/{app_id}/reject")
        resp = await client.post(f"/api/review/{app_id}/approve")
        assert resp.status_code == 409

    async def test_edit_nonexistent_returns_404(self, client: httpx.AsyncClient) -> None:
        resp = await client.patch("/api/review/999", json={"cover_letter": "x"})
        assert resp.status_code == 404
