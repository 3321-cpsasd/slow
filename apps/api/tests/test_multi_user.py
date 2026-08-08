from datetime import timedelta
import asyncio
import json
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
from app.application.service import SlowService
from app.core.errors import AppError
from app.demo_personas import LOCAL_DEMO_PASSWORD, LOCAL_DEMO_PERSONAS
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
    LearningNote,
    LearningPlan,
    LearningRun,
    LearningTask,
    LocalCredential,
    PlanCreationRequest,
    QuizAttempt,
    QuizSet,
    Section,
    SectionProgress,
    Series,
    Shelf,
    User,
    UserOnboarding,
    UserProfile,
    UserProfileRevision,
    now,
)
from app.main import create_app
from app.modules.learning.tasks import (
    claim_task,
    complete_task,
    recoverable_task_ids,
)
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
    public_config = client.get("/api/auth/config")
    assert public_config.status_code == 200
    assert public_config.json()["mode"] == "oidc"
    assert public_config.json()["providerName"] == "统一身份账户"
    assert public_config.json()["privacyNotice"]["noticeVersion"] == "2026-08-08-r2"

    me = login(client, fake_oidc)
    assert me["user"]["name"] == "用户 A"
    assert me["mode"] == "oidc"
    assert me["csrfToken"]
    assert me["onboarding"]["required"] is True

    bootstrap = client.get("/api/bootstrap")
    assert bootstrap.status_code == 428
    assert bootstrap.json()["code"] == "PRIVACY_CONSENT_REQUIRED"

    consent = client.post(
        "/api/privacy/consent",
        headers={"X-CSRF-Token": me["csrfToken"]},
        json={"privacyAccepted": True, "trialAccepted": True},
    )
    assert consent.status_code == 200

    missing_profile_csrf = client.patch(
        "/api/onboarding/profile",
        json={"currentStep": "direction", "profession": "产品设计师"},
    )
    assert missing_profile_csrf.status_code == 403
    assert missing_profile_csrf.json()["code"] == "CSRF_INVALID"

    saved = client.patch(
        "/api/onboarding/profile",
        headers={"X-CSRF-Token": me["csrfToken"]},
        json={
            "currentStep": "direction",
            "profession": "产品设计师",
            "stage": "foundation",
        },
    )
    assert saved.status_code == 200
    assert saved.json()["currentStep"] == "direction"
    resumed = client.get("/api/onboarding")
    assert resumed.json()["profile"]["profession"] == "产品设计师"
    assert resumed.json()["profile"]["preferences"] == {
        "openingStyle": "auto",
        "explanationDensity": "auto",
        "formatPreferences": [],
        "interactionRhythm": "auto",
    }
    assert resumed.json()["required"] is True

    invalid_preferences = client.post(
        "/api/onboarding/profile/complete",
        headers={"X-CSRF-Token": me["csrfToken"]},
        json={
            "profession": "产品设计师",
            "stage": "foundation",
            "purpose": "系统学习信息可视化并用于作品集",
            "domains": ["信息可视化"],
            "preferences": {"openingStyle": "visual_learner"},
        },
    )
    assert invalid_preferences.status_code == 400
    assert invalid_preferences.json()["code"] == "INVALID_REQUEST"

    completed = client.post(
        "/api/onboarding/profile/complete",
        headers={"X-CSRF-Token": me["csrfToken"]},
        json={
            "profession": "产品设计师",
            "stage": "foundation",
            "purpose": "系统学习信息可视化并用于作品集",
            "domains": ["信息可视化", "交互设计"],
            "experience": "有产品设计基础",
            "preferences": {
                "openingStyle": "problem_first",
                "explanationDensity": "thorough",
                "formatPreferences": ["worked_example", "diagram"],
                "interactionRhythm": "balanced",
            },
        },
    )
    assert completed.status_code == 200
    assert completed.json()["required"] is False
    assert completed.json()["profile"]["preferences"] == {
        "openingStyle": "problem_first",
        "explanationDensity": "thorough",
        "formatPreferences": ["worked_example", "diagram"],
        "interactionRhythm": "balanced",
    }
    with client.app.state.sessions() as db:
        profile = db.get(UserProfile, me["user"]["id"])
        revision = db.scalar(
            select(UserProfileRevision).where(
                UserProfileRevision.user_id == me["user"]["id"]
            )
        )
        flow = db.scalar(
            select(UserOnboarding).where(
                UserOnboarding.user_id == me["user"]["id"]
            )
        )
        assert profile.version == 1
        assert json.loads(profile.preferences_json)["openingStyle"] == "problem_first"
        assert revision.source == "self_report"
        assert json.loads(revision.snapshot_json)["preferences"]["formatPreferences"] == [
            "worked_example",
            "diagram",
        ]
        assert flow.status == "completed"

    bootstrap = client.get("/api/bootstrap")
    assert bootstrap.status_code == 200
    assert bootstrap.json()["user"]["id"] == me["user"]["id"]
    assert bootstrap.json()["shelves"] == []

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
    assert logout.content == b""
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


def test_demo_auth_config_is_public_and_explicit():
    app = create_app(
        "sqlite+pysqlite:///:memory:",
        ai=LocalDemoAdapter(),
        auth_mode="demo",
        app_mode="development",
        runtime_settings_path=False,
    )
    with TestClient(app) as client:
        response = client.get("/api/auth/config")

    assert response.status_code == 200
    assert response.json()["mode"] == "demo"
    assert response.json()["providerName"] == ""
    assert response.json()["privacyNotice"]["noticeVersion"] == "2026-08-08-r2"


@pytest.fixture
def local_auth_client(tmp_path):
    app = create_app(
        "sqlite+pysqlite:///:memory:",
        ai=LocalDemoAdapter(),
        source_verifier=AcceptingSourceVerifier(),
        attachment_storage=LocalAttachmentStorage(tmp_path / "attachments"),
        auth_mode="local",
        app_mode="development",
        runtime_settings_path=False,
    )
    with TestClient(app) as client:
        yield client


def test_local_accounts_login_reuses_session_boundary_and_isolates_users(
    local_auth_client,
):
    config = local_auth_client.get("/api/auth/config")
    assert config.status_code == 200
    assert config.json()["mode"] == "local"
    assert config.json()["providerName"] == ""
    assert LOCAL_DEMO_PASSWORD not in config.text
    assert all(persona.username not in config.text for persona in LOCAL_DEMO_PERSONAS)

    first = LOCAL_DEMO_PERSONAS[0]
    logged_in = local_auth_client.post(
        "/api/auth/local/login",
        json={"username": first.username, "password": LOCAL_DEMO_PASSWORD},
    )
    assert logged_in.status_code == 200
    assert logged_in.json()["user"] == {
        "id": first.user_id,
        "name": first.display_name,
    }
    assert logged_in.json()["csrfToken"]
    assert "slow_session" in logged_in.cookies

    bootstrap = local_auth_client.get("/api/bootstrap")
    assert bootstrap.status_code == 200
    assert bootstrap.json()["user"]["id"] == first.user_id
    assert bootstrap.json()["shelves"][0]["name"] == first.shelf_name

    logout = local_auth_client.post(
        "/api/auth/logout",
        headers={"X-CSRF-Token": logged_in.json()["csrfToken"]},
    )
    assert logout.status_code == 204

    second = LOCAL_DEMO_PERSONAS[1]
    second_login = local_auth_client.post(
        "/api/auth/local/login",
        json={"username": second.username, "password": LOCAL_DEMO_PASSWORD},
    )
    assert second_login.status_code == 200
    second_bootstrap = local_auth_client.get("/api/bootstrap")
    assert second_bootstrap.json()["user"]["id"] == second.user_id
    assert second_bootstrap.json()["shelves"][0]["name"] == second.shelf_name
    assert second_bootstrap.json()["shelves"][0]["id"] != bootstrap.json()["shelves"][0]["id"]

    with local_auth_client.app.state.sessions() as db:
        credentials = db.scalars(select(LocalCredential)).all()
        assert len(credentials) == len(LOCAL_DEMO_PERSONAS)
        assert all(row.password_hash.startswith("$argon2id$") for row in credentials)
        assert all(row.password_hash != LOCAL_DEMO_PASSWORD for row in credentials)


def test_fashion_to_ux_account_has_its_own_profile_and_rejects_wrong_password(
    local_auth_client,
):
    persona = next(
        item for item in LOCAL_DEMO_PERSONAS if item.username == "fashion-to-ux"
    )

    denied = local_auth_client.post(
        "/api/auth/local/login",
        json={"username": persona.username, "password": "wrong-password"},
    )
    assert denied.status_code == 401
    assert denied.json()["code"] == "LOCAL_LOGIN_INVALID"
    assert "slow_session" not in denied.cookies

    logged_in = local_auth_client.post(
        "/api/auth/local/login",
        json={"username": persona.username, "password": LOCAL_DEMO_PASSWORD},
    )
    assert logged_in.status_code == 200
    assert logged_in.json()["user"] == {
        "id": persona.user_id,
        "name": persona.display_name,
    }

    bootstrap = local_auth_client.get("/api/bootstrap")
    assert bootstrap.status_code == 200
    assert len(bootstrap.json()["shelves"]) == 1
    shelf = bootstrap.json()["shelves"][0]
    assert shelf["name"] == persona.shelf_name
    assert shelf["domain"] == persona.domain
    assert shelf["specialty"] == persona.specialty
    assert shelf["tags"] == list(persona.tags)


def test_fashion_to_ux_persona_targets_information_visualization_for_product_design():
    persona = next(
        item for item in LOCAL_DEMO_PERSONAS if item.username == "fashion-to-ux"
    )

    assert persona.display_name == "产品设计学习信息可视化"
    assert persona.scenario == "产品设计方向学习者系统学习信息可视化"
    assert persona.shelf_name == "信息可视化"
    assert persona.specialty == "产品设计与信息可视化"
    assert persona.tags == ("信息可视化", "产品设计", "数据表达")
    assert "服装" not in " ".join(
        (
            persona.display_name,
            persona.scenario,
            persona.shelf_name,
            persona.specialty,
            *persona.tags,
        )
    )


def test_two_local_users_interleave_learning_tasks_without_cross_access(
    tmp_path,
):
    class FailFirstRemediationAi(LocalDemoAdapter):
        def __init__(self):
            self.failed_remediation = False

        async def lesson_content(self, request, memory, prior_questions=None):
            if request.get("remediationStrategy") and not self.failed_remediation:
                self.failed_remediation = True
                raise AppError(
                    "simulated non-retryable remediation failure",
                    code="SIMULATED_REMEDIATION_FAILURE",
                    retryable=False,
                )
            return await super().lesson_content(request, memory, prior_questions)

    app = create_app(
        f"sqlite+pysqlite:///{tmp_path / 'two-user-tasks.db'}",
        ai=FailFirstRemediationAi(),
        source_verifier=AcceptingSourceVerifier(),
        attachment_storage=LocalAttachmentStorage(
            tmp_path / "two-user-task-attachments"
        ),
        auth_mode="local",
        app_mode="development",
        runtime_settings_path=False,
    )

    def login_as(client, persona):
        client.cookies.clear()
        response = client.post(
            "/api/auth/local/login",
            json={
                "username": persona.username,
                "password": LOCAL_DEMO_PASSWORD,
            },
        )
        assert response.status_code == 200
        session = response.cookies.get("slow_session")
        assert session
        client.cookies.clear()
        return {
            "Cookie": f"slow_session={session}",
            "X-CSRF-Token": response.json()["csrfToken"],
        }

    def wait_for_owned_task(client, task_id, headers):
        for _ in range(400):
            response = client.get(
                f"/api/learning-tasks/{task_id}",
                headers=headers,
            )
            assert response.status_code == 200
            payload = response.json()
            if payload["status"] in {"succeeded", "failed"}:
                return payload
            time.sleep(0.01)
        raise AssertionError(f"task did not finish: {task_id}")

    with TestClient(app) as client:
        persona_a, persona_b = LOCAL_DEMO_PERSONAS[:2]
        headers_a = login_as(client, persona_a)
        headers_b = login_as(client, persona_b)

        bootstrap_a = client.get("/api/bootstrap", headers=headers_a).json()
        bootstrap_b = client.get("/api/bootstrap", headers=headers_b).json()

        def create_started_section(headers, bootstrap, key):
            created = client.post(
                "/api/plans",
                headers={**headers, "Idempotency-Key": key},
                json={
                    "shelfId": bootstrap["shelves"][0]["id"],
                    "topic": key,
                    "role": "学习者",
                    "experience": "测试",
                    "depth": "deep",
                },
            )
            assert created.status_code == 201
            task = wait_for_owned_task(
                client,
                created.json()["initializationTask"]["taskId"],
                headers,
            )
            assert task["status"] == "succeeded"
            section_id = task["result"]["targetSectionId"]
            return client.get(
                f"/api/sections/{section_id}",
                headers=headers,
            ).json()

        section_a = create_started_section(
            headers_a,
            bootstrap_a,
            "two-user-a",
        )
        section_b = create_started_section(
            headers_b,
            bootstrap_b,
            "two-user-b",
        )

        failed_quiz = client.post(
            f"/api/sections/{section_a['id']}/quiz",
            headers={**headers_a, "Idempotency-Key": "quiz-user-a"},
            json={
                "quizSetId": section_a["quiz"]["id"],
                "answers": [[] for _ in section_a["quiz"]["questions"]],
            },
        )
        passed_quiz = client.post(
            f"/api/sections/{section_b['id']}/quiz",
            headers={**headers_b, "Idempotency-Key": "quiz-user-b"},
            json={
                "quizSetId": section_b["quiz"]["id"],
                "answers": [[1] for _ in section_b["quiz"]["questions"]],
            },
        )
        assert failed_quiz.status_code == passed_quiz.status_code == 200

        task_a = failed_quiz.json()["workflowTasks"][0]
        tasks_b = passed_quiz.json()["workflowTasks"]
        terminal_a = wait_for_owned_task(
            client,
            task_a["taskId"],
            headers_a,
        )
        terminal_b = [
            wait_for_owned_task(client, task["taskId"], headers_b)
            for task in tasks_b
        ]
        assert terminal_a["status"] == "failed"
        assert all(task["status"] == "succeeded" for task in terminal_b)

        assert client.get(
            f"/api/learning-tasks/{task_a['taskId']}",
            headers=headers_b,
        ).status_code == 404
        assert client.post(
            f"/api/learning-tasks/{task_a['taskId']}/retry",
            headers=headers_b,
        ).status_code == 404
        for task in tasks_b:
            assert client.get(
                f"/api/learning-tasks/{task['taskId']}",
                headers=headers_a,
            ).status_code == 404
            assert client.post(
                f"/api/learning-tasks/{task['taskId']}/retry",
                headers=headers_a,
            ).status_code == 404

        retried = client.post(
            f"/api/learning-tasks/{task_a['taskId']}/retry",
            headers=headers_a,
        )
        assert retried.status_code == 200
        assert retried.json()["status"] == "pending"
        assert wait_for_owned_task(
            client,
            task_a["taskId"],
            headers_a,
        )["status"] == "succeeded"

        refreshed_a = client.get(
            f"/api/sections/{section_a['id']}",
            headers=headers_a,
        ).json()
        refreshed_b = client.get(
            f"/api/sections/{section_b['id']}",
            headers=headers_b,
        ).json()
        assert refreshed_a["remediations"]
        assert refreshed_a["workflowTasks"][0]["status"] == "succeeded"
        assert refreshed_b["note"]
        assert all(
            task["status"] == "succeeded"
            for task in refreshed_b["workflowTasks"]
        )

        with client.app.state.sessions() as db:
            tasks = db.scalars(
                select(LearningTask).where(
                    LearningTask.id.in_(
                        [task_a["taskId"], *[task["taskId"] for task in tasks_b]]
                    )
                )
            ).all()
            assert {task.user_id for task in tasks} == {
                persona_a.user_id,
                persona_b.user_id,
            }
            assert db.scalar(
                select(LearningNote).where(
                    LearningNote.user_id == persona_b.user_id,
                    LearningNote.section_id == section_b["id"],
                )
            )
            evidence_users = set(
                db.scalars(
                    select(LearningEvidence.user_id).where(
                        LearningEvidence.section_id.in_(
                            [section_a["id"], section_b["id"]]
                        )
                    )
                ).all()
            )
            assert evidence_users == {persona_a.user_id, persona_b.user_id}


def test_local_login_rejects_wrong_password_and_locks_repeated_failures(
    local_auth_client,
):
    persona = LOCAL_DEMO_PERSONAS[2]
    for _ in range(5):
        denied = local_auth_client.post(
            "/api/auth/local/login",
            json={"username": persona.username, "password": "wrong-password"},
        )
        assert denied.status_code == 401
        assert denied.json()["code"] == "LOCAL_LOGIN_INVALID"
        assert "slow_session" not in denied.cookies

    still_locked = local_auth_client.post(
        "/api/auth/local/login",
        json={"username": persona.username, "password": LOCAL_DEMO_PASSWORD},
    )
    assert still_locked.status_code == 401
    assert still_locked.json()["code"] == "LOCAL_LOGIN_INVALID"
    with local_auth_client.app.state.sessions() as db:
        credential = db.scalar(
            select(LocalCredential).where(
                LocalCredential.username == persona.username,
            )
        )
        assert credential.locked_until is not None


def test_production_refuses_local_authentication():
    with pytest.raises(
        RuntimeError,
        match="Production mode cannot use local authentication",
    ):
        create_app(
            "sqlite+pysqlite:///:memory:",
            auth_mode="local",
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


def test_expired_worker_cannot_persist_domain_rows_after_takeover():
    engine, sessions = build_database("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    with sessions() as db:
        run, sections = build_task_graph(db)
        task = LearningTask(
            id="task_domain_fence",
            learning_run_id=run.id,
            user_id="user_a",
            section_id=sections["user_a"].id,
            task_type="note_generation",
            idempotency_key="domain-fence",
            trigger_id="trigger",
            status="pending",
        )
        db.add(task)
        db.commit()

        stale_context = claim_task(db, task.id, lease_owner="worker_one")
        claimed = db.get(LearningTask, task.id)
        claimed.lease_expires_at = now() - timedelta(seconds=1)
        db.commit()
        assert claim_task(db, task.id, lease_owner="worker_two")

        SlowService(
            db,
            LocalDemoAdapter(),
            AcceptingSourceVerifier(),
            scope=stale_context,
        )
        db.add(
            LearningNote(
                id="note_from_stale_worker",
                learning_run_id=run.id,
                section_id=sections["user_a"].id,
                user_id="user_a",
                ai_content_json="{}",
                user_content_json="{}",
            )
        )
        with pytest.raises(AppError) as raised:
            db.commit()
        assert raised.value.code == "TASK_LEASE_LOST"
        db.rollback()
        assert db.get(LearningNote, "note_from_stale_worker") is None
    engine.dispose()


def test_expired_exhausted_task_becomes_observable_failure():
    engine, sessions = build_database("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    with sessions() as db:
        run, sections = build_task_graph(db)
        task = LearningTask(
            id="task_exhausted",
            learning_run_id=run.id,
            user_id="user_a",
            section_id=sections["user_a"].id,
            task_type="note_generation",
            idempotency_key="exhausted",
            trigger_id="trigger",
            status="running",
            attempt_count=3,
            max_attempts=3,
            lease_owner="dead-worker",
            lease_token="dead-token",
            lease_expires_at=now() - timedelta(seconds=1),
        )
        db.add(task)
        db.commit()

        assert recoverable_task_ids(db) == []
        db.refresh(task)
        assert task.status == "failed"
        assert task.error_code == "LEARNING_TASK_RETRY_EXHAUSTED"
        assert task.lease_owner is None
        assert task.lease_token is None
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
