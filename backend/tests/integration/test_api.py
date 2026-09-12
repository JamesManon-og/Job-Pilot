from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from pathlib import Path

import httpx
import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from jobpilot.api.app import create_app
from jobpilot.api.deps import AppState
from jobpilot.config import Settings
from jobpilot.database import create_session_factory
from jobpilot.database.repositories import ApplicationRepository, JobRepository, ResumeRepository
from jobpilot.domain import Application, ApplicationStatus, Job, JobSource, Resume, ResumeProfile


@pytest.fixture
def app_state(engine: AsyncEngine, tmp_path: Path) -> AppState:
    """AppState on the temp database, isolated from the real config.yaml and data dir."""
    state = AppState.__new__(AppState)
    state.settings = Settings(_env_file=None, project_root=tmp_path)  # type: ignore[call-arg]
    state.engine = engine
    state.session_factory = create_session_factory(engine)
    return state


@pytest.fixture
async def client(app_state: AppState) -> AsyncIterator[httpx.AsyncClient]:
    """ASGI test client wired to the temp-database engine (no network, no lifespan).

    Sends X-JobPilot-Client like the dashboard's JSON requests do; the CSRF
    tests below use a bare client to prove requests without it are refused.
    """
    app = create_app()
    app.state.jobpilot = app_state
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://test", headers={"X-JobPilot-Client": "test"}
    ) as http:
        yield http


@pytest.fixture
async def bare_client(app_state: AppState) -> AsyncIterator[httpx.AsyncClient]:
    app = create_app()
    app.state.jobpilot = app_state
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


class TestReviewWorkflowSafety:
    """Regression tests for the approval workflow (audit findings)."""

    async def test_approve_does_not_open_a_browser_or_submit(
        self, client: httpx.AsyncClient, engine: AsyncEngine
    ) -> None:
        """Bug: approve scheduled a headless autofill that then marked the
        application SUBMITTED although nothing was ever sent."""
        app_id, _ = await seed_application(engine)
        resp = await client.post(f"/api/review/{app_id}/approve")
        assert resp.status_code == 200
        detail = (await client.get(f"/api/applications/{app_id}")).json()["application"]
        assert detail["status"] == "approved"
        assert detail["submitted_at"] is None

    async def test_approving_twice_returns_409(
        self, client: httpx.AsyncClient, engine: AsyncEngine
    ) -> None:
        app_id, _ = await seed_application(engine)
        assert (await client.post(f"/api/review/{app_id}/approve")).status_code == 200
        assert (await client.post(f"/api/review/{app_id}/approve")).status_code == 409

    async def test_concurrent_approvals_exactly_one_succeeds(
        self, client: httpx.AsyncClient, engine: AsyncEngine
    ) -> None:
        """Two tabs clicking Approve at once: previously both returned 200 and
        each scheduled its own submission."""
        app_id, _ = await seed_application(engine)
        responses = await asyncio.gather(
            *(client.post(f"/api/review/{app_id}/approve") for _ in range(4))
        )
        assert sorted(r.status_code for r in responses) == [200, 409, 409, 409]

    async def test_rejecting_twice_returns_409(
        self, client: httpx.AsyncClient, engine: AsyncEngine
    ) -> None:
        app_id, _ = await seed_application(engine)
        assert (await client.post(f"/api/review/{app_id}/reject")).status_code == 200
        assert (await client.post(f"/api/review/{app_id}/reject")).status_code == 409

    async def test_editing_after_approval_is_refused(
        self, client: httpx.AsyncClient, engine: AsyncEngine
    ) -> None:
        app_id, _ = await seed_application(engine)
        await client.post(f"/api/review/{app_id}/approve")
        resp = await client.patch(f"/api/review/{app_id}", json={"cover_letter": "late edit"})
        assert resp.status_code == 409

    async def test_unapprove_allows_editing_again(
        self, client: httpx.AsyncClient, engine: AsyncEngine
    ) -> None:
        app_id, _ = await seed_application(engine)
        await client.post(f"/api/review/{app_id}/approve")
        assert (await client.post(f"/api/review/{app_id}/unapprove")).status_code == 200
        resp = await client.patch(f"/api/review/{app_id}", json={"cover_letter": "v2"})
        assert resp.status_code == 200

    async def test_approve_unknown_application_is_404(self, client: httpx.AsyncClient) -> None:
        assert (await client.post("/api/review/999/approve")).status_code == 404

    async def test_invalid_status_filter_is_422(self, client: httpx.AsyncClient) -> None:
        assert (await client.get("/api/review", params={"status": "bogus"})).status_code == 422

    async def test_review_actions_are_audit_logged(
        self, client: httpx.AsyncClient, engine: AsyncEngine
    ) -> None:
        """Bug: approve/reject wrote row.status directly with no event log entry."""
        app_id, _ = await seed_application(engine)
        await client.post(f"/api/review/{app_id}/approve")
        factory = create_session_factory(engine)
        async with factory() as session:
            events = await ApplicationRepository(session).list_events(app_id)
        assert any(
            e["event_type"] == "status_changed" and e["payload"]["to"] == "approved" for e in events
        )


class TestSecurity:
    async def test_cross_site_form_post_cannot_approve(
        self, bare_client: httpx.AsyncClient, engine: AsyncEngine
    ) -> None:
        """Bug: CORS doesn't stop a malicious page from *sending* a simple POST
        to localhost:8000 — any website could approve pending applications."""
        app_id, _ = await seed_application(engine)
        resp = await bare_client.post(
            f"/api/review/{app_id}/approve",
            headers={
                "Origin": "https://evil.example",
                "Content-Type": "application/x-www-form-urlencoded",
            },
        )
        assert resp.status_code == 403
        resp = await bare_client.post(f"/api/review/{app_id}/approve")  # no JSON, no header
        assert resp.status_code == 403
        detail = (await bare_client.get(f"/api/applications/{app_id}")).json()
        assert detail["application"]["status"] == "pending_review"

    async def test_foreign_origin_is_refused_even_with_json(
        self, bare_client: httpx.AsyncClient, engine: AsyncEngine
    ) -> None:
        app_id, _ = await seed_application(engine)
        resp = await bare_client.post(
            f"/api/review/{app_id}/reject",
            json={},
            headers={"Origin": "https://evil.example"},
        )
        assert resp.status_code == 403

    async def test_dashboard_origin_with_json_is_allowed(
        self, bare_client: httpx.AsyncClient, engine: AsyncEngine
    ) -> None:
        app_id, _ = await seed_application(engine)
        resp = await bare_client.post(
            f"/api/review/{app_id}/approve",
            json={},
            headers={"Origin": "http://localhost:3000"},
        )
        assert resp.status_code == 200

    async def test_unknown_host_header_is_refused(self, bare_client: httpx.AsyncClient) -> None:
        """DNS-rebinding guard: a page served from attacker.example resolving to
        127.0.0.1 would send Host: attacker.example."""
        resp = await bare_client.get("/api/stats", headers={"Host": "attacker.example"})
        assert resp.status_code == 400

    async def test_invalid_config_returns_clear_error(
        self, client: httpx.AsyncClient, app_state: AppState
    ) -> None:
        app_state.settings.preferences_path.parent.mkdir(parents=True, exist_ok=True)
        app_state.settings.preferences_path.write_text("max_applications_per_day: 0\n")
        await seed_resume_and_job_for_matches(app_state)
        resp = await client.get("/api/matches")
        assert resp.status_code == 500
        assert "max_applications_per_day" in resp.json()["detail"]


async def seed_resume_and_job_for_matches(state: AppState) -> None:
    async with state.session_factory() as session:
        await ResumeRepository(session).add(
            Resume(version="v1", file_path="/r.pdf", is_active=True)
        )
        await session.commit()


class TestStatsAndActions:
    async def test_matched_count_only_counts_the_active_resume(
        self, client: httpx.AsyncClient, engine: AsyncEngine
    ) -> None:
        """Bug: stats counted match rows for every resume version."""
        from jobpilot.database.repositories import MatchResultRepository
        from jobpilot.domain import MatchRecommendation, MatchResult

        job = await seed_job(engine)
        factory = create_session_factory(engine)
        async with factory() as session:
            old = await ResumeRepository(session).add(Resume(version="old", file_path="/a.pdf"))
            new = await ResumeRepository(session).add(
                Resume(version="new", file_path="/b.pdf", is_active=True)
            )
            for resume in (old, new):
                assert resume.id is not None and job.id is not None
                await MatchResultRepository(session).upsert(
                    MatchResult(
                        job_id=job.id,
                        resume_id=resume.id,
                        score=90,
                        recommendation=MatchRecommendation.STRONG_APPLY,
                    )
                )
            await session.commit()
        stats = (await client.get("/api/stats")).json()
        assert stats["total_matched"] == 1
        assert stats["strong_matches"] == 1

    async def test_scrape_action_refused_while_pipeline_runs(
        self, client: httpx.AsyncClient, app_state: AppState
    ) -> None:
        from jobpilot.locks import PIPELINE_LOCK, process_lock

        with process_lock(PIPELINE_LOCK, app_state.settings.locks_dir):
            resp = await client.post("/api/actions/scrape")
        assert resp.status_code == 409
        assert "already in progress" in resp.json()["detail"]


class TestPlatformsAPI:
    async def test_lists_adapters_with_session_state(
        self, client: httpx.AsyncClient, engine: AsyncEngine
    ) -> None:
        from jobpilot.database.repositories import PlatformSessionRepository
        from jobpilot.domain import JobSource, SessionStatus

        factory = create_session_factory(engine)
        async with factory() as session:
            await PlatformSessionRepository(session).record(
                JobSource.LINKEDIN, SessionStatus.NEEDS_LOGIN, detail="session expired"
            )
            await session.commit()

        rows = {p["platform"]: p for p in (await client.get("/api/platforms")).json()}
        assert {"remoteok", "linkedin", "jobstreet", "onlinejobs"} <= set(rows)
        assert rows["remoteok"]["needs_account"] is False
        assert rows["remoteok"]["session_status"] == "not_required"
        assert rows["remoteok"]["enabled"] is True  # default
        assert rows["linkedin"]["session_status"] == "needs_login"
        assert rows["linkedin"]["session_detail"] == "session expired"
        assert rows["linkedin"]["login_command"] == "jobpilot login linkedin"
        assert rows["linkedin"]["has_saved_login"] is False

    async def test_stats_include_the_pipeline_funnel_and_pause_flag(
        self, client: httpx.AsyncClient, engine: AsyncEngine, app_state: AppState
    ) -> None:
        from jobpilot.control import pause

        await seed_job(engine)
        stats = (await client.get("/api/stats")).json()
        assert stats["jobs_by_status"] == {"discovered": 1}
        assert stats["paused"] is False
        assert stats["daily_cap"] == 10 and stats["applications_today"] == 0

        pause(app_state.settings.data_dir, "manual stop")
        assert (await client.get("/api/stats")).json()["paused"] is True
        assert (await client.post("/api/actions/scrape")).status_code == 409

    async def test_jobs_can_be_filtered_by_pipeline_status(
        self, client: httpx.AsyncClient, engine: AsyncEngine
    ) -> None:
        from jobpilot.database.repositories import JobRepository
        from jobpilot.domain import JobStatus

        job = await seed_job(engine)
        assert job.id is not None
        factory = create_session_factory(engine)
        async with factory() as session:
            await JobRepository(session).set_status(job.id, JobStatus.SKIPPED, reason="duplicate")
            await session.commit()

        assert (await client.get("/api/jobs", params={"status": "discovered"})).json() == []
        skipped = (await client.get("/api/jobs", params={"status": "skipped"})).json()
        assert len(skipped) == 1
        assert skipped[0]["status_reason"] == "duplicate"
