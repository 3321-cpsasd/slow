from datetime import timedelta
import asyncio
import time
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.ai.local_adapter import LocalDemoAdapter
from joserfc import jwt
from joserfc.jwk import RSAKey

from app.auth.oidc import OidcClient, OidcIdentity
from app.core.errors import AppError
from app.infrastructure.database import build_database
from app.infrastructure.tables import (
    Base,
    AuthSession,
    Book,
    BookProgress,
    Chapter,
    ChapterProgress,
    ContentVersion,
    LearningEvidence,
    LearningMemory,
    LearningPlan,
    LearningRun,
    LearningTask,
    PlanCreationRequest,
    QuizAttempt,
    QuizSet,
    Section,
    SectionProgress,
    Series,
    Shelf,
    User,
    now,
)
from app.main import create_app
from app.modules.learning.tasks import claim_task, complete_task
from app.modules.learning.rebuild import rebuild_user_projections
from app.modules.library.context import ActiveLearningContextResolver
from app.services.attachment_storage import LocalAttachmentStorage
from app.services.source_verifier import AcceptingSourceVerifier


class FakeOidcClient:
    def __init__(self):
        self.state = ""
        self.nonce = ""
        self.code_verifier = ""
        self.closed = False

    async def authorization_url(self, *, state, nonce, code_verifier):
        self.state = state
        self.nonce = nonce
        self.code_verifier = code_verifier
        return f"https://identity.example/authorize?state={state}"

    async def exchange(self, *, code, nonce, code_verifier):
        assert code == "valid-code"
        assert nonce == self.nonce
        assert code_verifier == self.code_verifier
        return OidcIdentity(
            issuer="https://identity.example",
            subject="subject-a",
            display_name="用户 A",
            email="a@example.com",
            email_verified=True,
        )

    async def close(self):
        self.closed = True


def test_oidc_client_validates_discovery_pkce_signature_and_nonce():
    issuer = "https://identity.example"
    key = RSAKey.generate_key(auto_kid=True)
    issued_at = int(time.time())
    id_token = jwt.encode(
        {"alg": "RS256", "kid": key.kid},
        {
            "iss": issuer,
            "sub": "subject-a",
            "aud": "client-a",
            "iat": issued_at,
            "exp": issued_at + 300,
            "nonce": "nonce-a",
            "name": "用户 A",
        },
        key,
        algorithms=["RS256"],
    )

    def handler(request: httpx.Request):
        if request.url.path.endswith("openid-configuration"):
            return httpx.Response(
                200,
                json={
                    "issuer": issuer,
                    "authorization_endpoint": f"{issuer}/authorize",
                    "token_endpoint": f"{issuer}/token",
                    "jwks_uri": f"{issuer}/jwks",
                },
            )
        if request.url.path == "/token":
            return httpx.Response(200, json={"id_token": id_token})
        if request.url.path == "/jwks":
            return httpx.Response(
                200,
                json={"keys": [key.as_dict(private=False)]},
            )
        raise AssertionError(f"unexpected OIDC request: {request.url}")

    async def scenario():
        client = OidcClient(
            issuer=issuer,
            client_id="client-a",
            client_secret="secret-a",
            redirect_uri="https://app.example/api/auth/callback",
            scopes="openid email profile",
            transport=httpx.MockTransport(handler),
        )
        authorization_url = await client.authorization_url(
            state="state-a",
            nonce="nonce-a",
            code_verifier="verifier-a",
        )
        query = parse_qs(urlparse(authorization_url).query)
        assert query["state"] == ["state-a"]
        assert query["nonce"] == ["nonce-a"]
        assert query["code_challenge_method"] == ["S256"]
        assert query["code_challenge"][0] != "verifier-a"

        identity = await client.exchange(
            code="code-a",
            nonce="nonce-a",
            code_verifier="verifier-a",
        )
        assert identity.subject == "subject-a"
        with pytest.raises(AppError) as raised:
            await client.exchange(
                code="code-a",
                nonce="wrong-nonce",
                code_verifier="verifier-a",
            )
        assert raised.value.code == "OIDC_NONCE_INVALID"
        await client.close()

    asyncio.run(scenario())


@pytest.fixture
def oidc_client(tmp_path):
    fake_oidc = FakeOidcClient()
    storage = LocalAttachmentStorage(tmp_path / "attachments")
    app = create_app(
        "sqlite+pysqlite:///:memory:",
        LocalDemoAdapter(),
        AcceptingSourceVerifier(),
        storage,
        auth_mode="oidc",
        app_mode="test",
        oidc_client=fake_oidc,
    )
    with TestClient(app) as client:
        yield client, fake_oidc


def login(client: TestClient, oidc: FakeOidcClient):
    start = client.get(
        "/api/auth/login?return_to=/library",
        follow_redirects=False,
    )
    assert start.status_code in {302, 307}
    assert oidc.state
    callback = client.get(
        f"/api/auth/callback?code=valid-code&state={oidc.state}",
        follow_redirects=False,
    )
    assert callback.status_code in {302, 307}
    assert callback.headers["location"].endswith("/library")
    me = client.get("/api/auth/me")
    assert me.status_code == 200
    return me.json()


def add_foreign_series(db, *, user_id: str) -> Series:
    shelf = Shelf(
        id=f"shelf_{user_id}",
        user_id=user_id,
        name="其他人的书架",
        domain="测试",
    )
    db.add(shelf)
    db.flush()
    plan = LearningPlan(
        id=f"plan_{user_id}",
        shelf_id=shelf.id,
        topic="隔离",
        role="学习者",
        experience="测试",
        depth="overview",
        confidence="high",
        status="active",
    )
    db.add(plan)
    db.flush()
    series = Series(
        id=f"series_{user_id}",
        plan_id=plan.id,
        shelf_id=shelf.id,
        title="不可见系列",
        rationale="验证隔离",
    )
    db.add(series)
    db.flush()
    return series


def test_oidc_session_csrf_logout_and_user_isolation(oidc_client):
    client, fake_oidc = oidc_client
    me = login(client, fake_oidc)
    assert me["user"]["name"] == "用户 A"
    assert me["mode"] == "oidc"
    assert me["csrfToken"]

    bootstrap = client.get("/api/bootstrap")
    assert bootstrap.status_code == 200
    assert bootstrap.json()["user"]["id"] == me["user"]["id"]
    assert len(bootstrap.json()["shelves"]) == 1

    missing_csrf = client.post(
        "/api/shelves",
        json={"name": "新书架", "domain": "测试"},
    )
    assert missing_csrf.status_code == 403
    assert missing_csrf.json()["code"] == "CSRF_INVALID"

    created = client.post(
        "/api/shelves",
        json={"name": "新书架", "domain": "测试"},
        headers={"X-CSRF-Token": me["csrfToken"]},
    )
    assert created.status_code == 201

    with client.app.state.sessions() as db:
        user_b = User(id="user_b", name="用户 B")
        db.add(user_b)
        db.flush()
        foreign_series = add_foreign_series(db, user_id=user_b.id)
        db.commit()
        foreign_series_id = foreign_series.id

    hidden = client.get(f"/api/series/{foreign_series_id}")
    assert hidden.status_code == 404

    logout = client.post(
        "/api/auth/logout",
        headers={"X-CSRF-Token": me["csrfToken"]},
    )
    assert logout.status_code == 204
    assert client.get("/api/auth/me").status_code == 401


def test_oidc_callback_state_is_bound_to_the_login_browser(oidc_client):
    client, fake_oidc = oidc_client
    start = client.get("/api/auth/login", follow_redirects=False)
    assert start.status_code in {302, 307}
    client.cookies.clear()

    callback = client.get(
        f"/api/auth/callback?code=valid-code&state={fake_oidc.state}",
        follow_redirects=False,
    )

    assert callback.status_code == 400
    assert callback.json()["code"] == "OIDC_STATE_BROWSER_MISMATCH"


def test_disabled_user_revokes_active_session(oidc_client):
    client, fake_oidc = oidc_client
    me = login(client, fake_oidc)
    with client.app.state.sessions() as db:
        user = db.get(User, me["user"]["id"])
        user.status = "disabled"
        db.commit()

    denied = client.get("/api/bootstrap")
    assert denied.status_code == 403
    assert denied.json()["code"] == "ACCOUNT_DISABLED"
    with client.app.state.sessions() as db:
        auth_session = db.scalar(
            select(AuthSession).where(
                AuthSession.user_id == me["user"]["id"]
            )
        )
        assert auth_session.status == "revoked"


def test_production_refuses_demo_authentication():
    with pytest.raises(
        RuntimeError,
        match="Production mode cannot use demo authentication",
    ):
        create_app(
            "sqlite+pysqlite:///:memory:",
            auth_mode="demo",
            app_mode="production",
        )


def test_plan_idempotency_key_is_scoped_by_user():
    engine, sessions = build_database("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    with sessions() as db:
        db.add_all(
            [
                User(id="user_a", name="A"),
                User(id="user_b", name="B"),
            ]
        )
        db.flush()
        db.add_all(
            [
                PlanCreationRequest(
                    idempotency_key="same-client-key",
                    user_id="user_a",
                    request_hash="a" * 64,
                    status="pending",
                ),
                PlanCreationRequest(
                    idempotency_key="same-client-key",
                    user_id="user_b",
                    request_hash="b" * 64,
                    status="pending",
                ),
            ]
        )
        db.commit()
        rows = db.scalars(
            select(PlanCreationRequest).where(
                PlanCreationRequest.idempotency_key == "same-client-key"
            )
        ).all()
        assert {row.user_id for row in rows} == {"user_a", "user_b"}
    engine.dispose()


def build_task_graph(db):
    users = [User(id="user_a", name="A"), User(id="user_b", name="B")]
    db.add_all(users)
    db.flush()
    series_by_user = {}
    sections = {}
    for user in users:
        series = add_foreign_series(db, user_id=user.id)
        book = Book(
            id=f"book_{user.id}",
            series_id=series.id,
            shelf_id=f"shelf_{user.id}",
            position=1,
            title="书",
            topic="测试",
            description="测试",
            estimated_minutes=20,
        )
        chapter = Chapter(
            id=f"chapter_{user.id}",
            book_id=book.id,
            position=1,
            title="章",
            objective="测试",
        )
        section = Section(
            id=f"section_{user.id}",
            chapter_id=chapter.id,
            position=1,
            title="节",
            question="问题",
            objectives_json="[]",
        )
        db.add(book)
        db.flush()
        db.add(chapter)
        db.flush()
        db.add(section)
        db.flush()
        series_by_user[user.id] = series
        sections[user.id] = section
    run = LearningRun(
        id="run_a",
        user_id="user_a",
        series_id=series_by_user["user_a"].id,
        status="active",
    )
    db.add(run)
    db.flush()
    return run, sections


def test_task_aggregate_rejects_cross_series_section():
    engine, sessions = build_database("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    with sessions() as db:
        run, sections = build_task_graph(db)
        task = LearningTask(
            id="task_cross_series",
            learning_run_id=run.id,
            user_id="user_a",
            section_id=sections["user_b"].id,
            task_type="note_generation",
            idempotency_key="cross-series",
            trigger_id="trigger",
            status="pending",
        )
        db.add(task)
        db.commit()
        with pytest.raises(AppError) as raised:
            ActiveLearningContextResolver(db).resolve_learning_task(
                user_id="user_a",
                task_id=task.id,
            )
        assert raised.value.code == "TASK_AGGREGATE_MISMATCH"
    engine.dispose()


def test_initial_preload_rejects_cross_series_chapter():
    engine, sessions = build_database("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    with sessions() as db:
        run, sections = build_task_graph(db)
        task = LearningTask(
            id="task_cross_series_chapter",
            learning_run_id=run.id,
            user_id="user_a",
            section_id=None,
            task_type="initial_book_preload",
            idempotency_key="cross-series-chapter",
            trigger_id="trigger",
            payload_json='{"chapterId":"chapter_user_b"}',
            status="pending",
        )
        db.add(task)
        db.commit()
        with pytest.raises(AppError) as raised:
            ActiveLearningContextResolver(
                db
            ).resolve_chapter_learning_task(
                user_id="user_a",
                task_id=task.id,
                chapter_id=sections["user_b"].chapter_id,
            )
        assert raised.value.code == "TASK_AGGREGATE_MISMATCH"
    engine.dispose()


def test_composite_run_user_foreign_key_rejects_mismatch():
    engine, sessions = build_database("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    with sessions() as db:
        run, sections = build_task_graph(db)
        db.add(
            LearningTask(
                id="task_wrong_user",
                learning_run_id=run.id,
                user_id="user_b",
                section_id=sections["user_a"].id,
                task_type="note_generation",
                idempotency_key="wrong-user",
                trigger_id="trigger",
                status="pending",
            )
        )
        with pytest.raises(IntegrityError):
            db.commit()
    engine.dispose()


def test_expired_worker_cannot_commit_after_new_lease():
    engine, sessions = build_database("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    with sessions() as db:
        run, sections = build_task_graph(db)
        task = LearningTask(
            id="task_fenced",
            learning_run_id=run.id,
            user_id="user_a",
            section_id=sections["user_a"].id,
            task_type="note_generation",
            idempotency_key="fenced",
            trigger_id="trigger",
            status="pending",
        )
        db.add(task)
        db.commit()

        first = claim_task(db, task.id, lease_owner="worker_one")
        assert first is not None
        claimed = db.get(LearningTask, task.id)
        claimed.lease_expires_at = now() - timedelta(seconds=1)
        db.commit()
        second = claim_task(db, task.id, lease_owner="worker_two")
        assert second is not None
        assert second.lease_token != first.lease_token

        with pytest.raises(AppError) as raised:
            complete_task(db, first, {"writer": "old"})
        assert raised.value.code == "TASK_LEASE_LOST"
        completed = complete_task(db, second, {"writer": "new"})
        assert completed.status == "succeeded"
        assert '"new"' in completed.result_json
    engine.dispose()


def test_learning_projections_rebuild_from_attempts_and_evidence():
    engine, sessions = build_database("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    with sessions() as db:
        run, sections = build_task_graph(db)
        section = sections["user_a"]
        chapter = db.get(Chapter, section.chapter_id)
        book = db.get(Book, chapter.book_id)
        db.add(
            BookProgress(
                id="book_progress_a",
                learning_run_id=run.id,
                user_id="user_a",
                book_id=book.id,
                status="locked",
            )
        )
        db.add(
            ChapterProgress(
                id="chapter_progress_a",
                learning_run_id=run.id,
                user_id="user_a",
                chapter_id=chapter.id,
                status="locked",
            )
        )
        db.add(
            SectionProgress(
                id="section_progress_a",
                learning_run_id=run.id,
                user_id="user_a",
                section_id=section.id,
                status="locked",
            )
        )
        content = ContentVersion(
            id="content_a",
            section_id=section.id,
            version=1,
            blocks_json="[]",
            sources_json="[]",
            confidence="high",
        )
        db.add(content)
        db.flush()
        quiz = QuizSet(
            id="quiz_a",
            section_id=section.id,
            content_version_id=content.id,
            generation=1,
            questions_json="[]",
        )
        db.add(quiz)
        db.flush()
        db.add(
            QuizAttempt(
                id="attempt_a",
                quiz_set_id=quiz.id,
                learning_run_id=run.id,
                user_id="user_a",
                answers_json="[[1]]",
                results_json='[{"correct":true}]',
                passed=True,
            )
        )
        evidence = LearningEvidence(
            id="evidence_a",
            learning_run_id=run.id,
            user_id="user_a",
            shelf_id=book.shelf_id,
            series_id=book.series_id,
            book_id=book.id,
            chapter_id=chapter.id,
            section_id=section.id,
            concept="一致性",
            evidence_type="quiz",
            result_json="{}",
            mastery_delta=20,
        )
        db.add(evidence)
        db.add(
            LearningMemory(
                id="memory_wrong",
                user_id="user_a",
                shelf_id=book.shelf_id,
                concept="一致性",
                mastery_score=99,
                evidence_count=99,
                summary="corrupted",
            )
        )
        db.commit()

        report = rebuild_user_projections(db, user_id="user_a")
        assert report["sections"] == 1
        assert db.get(SectionProgress, "section_progress_a").status == "completed"
        assert db.get(ChapterProgress, "chapter_progress_a").status == "completed"
        assert db.get(BookProgress, "book_progress_a").status == "completed"
        memory = db.scalar(
            select(LearningMemory).where(
                LearningMemory.user_id == "user_a",
                LearningMemory.concept == "一致性",
            )
        )
        assert memory.mastery_score == 20
        assert memory.evidence_count == 1
    engine.dispose()
