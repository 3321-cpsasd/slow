import asyncio
from contextlib import asynccontextmanager
from contextlib import suppress
import hmac
import json
import logging
from pathlib import Path
from urllib.parse import urlparse
from uuid import uuid4
from fastapi import Depends, FastAPI, Header, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, Response, StreamingResponse
from sqlalchemy import select
from sqlalchemy.orm import Session
from .auth.context import Principal, UserScope, demo_user_scope
from .auth.profile import ProfileService
from .auth.privacy import PrivacyService, privacy_notice
from .auth.local import LocalCredentialService
from .auth.oidc import OidcClient
from .auth.password import PasswordCredentialService
from .auth.recovery import AccountRecoveryService
from .auth.registration import claim_alpha_registration
from .auth.service import IdentityService, OidcStateService, SessionService, token_hash
from .ai.openai_adapter import OpenAiAdapter
from .ai.anthropic_adapter import AnthropicAdapter
from .ai.fallback_adapter import FallbackAiAdapter
from .ai.local_adapter import LocalDemoAdapter
from .ai.port import ProviderCapabilities
from .ai.metering import AiUsageRecorder
from .api.schemas import AccountExitCreate, AiRuntimeUpdate, AskMeDiscussionAction, AskMeDiscussionTurnCreate, AskMeReply, AskRequest, AttachmentSubmit, ChapterCreate, ChapterOrder, ChapterUpdate, DailyModeUpdate, FeedbackCreate, LearningPreferenceDecisionCreate, LearningPreferenceEvidenceCreate, MissionAdoptionCreate, MissionVersionCreate, NoteReviewSupplementCreate, NoteUpdate, PasswordLogin, PasswordRecoveryReset, PasswordRegistration, PersonalPresentationAdopt, PlanCreate, PrivacyConsentCreate, ProductEventBatch, ProfileComplete, ProfileDraftUpdate, QaClassificationUpdate, QuizSubmit, RecoveryCodeRotate, ResumeUpdate, ReviewSubmit, ShelfCreate, StudyActivityHeartbeat
from .application.service import DEMO_USER_ID, SlowService
from .core.config import settings
from .core.errors import AppError
from .demo_personas import LOCAL_DEMO_PERSONAS
from .infrastructure.database import build_database
from .infrastructure.tables import Base, LearningTask, QuizAttempt, Remediation, Shelf, User, now
from .modules.learning.tasks import claim_task, heartbeat_task, recoverable_task_ids
from .modules.learning.study_activity import StudyActivityService
from .modules.feedback.service import FeedbackService
from .modules.telemetry.service import ProductEventService
from .modules.library.context import ActiveLearningContextResolver
from .modules.preferences.service import LearningPreferenceService, PersonalPresentationService
from .services.source_verifier import ModelOnlySourcePolicy
from .services.attachment_storage import LocalAttachmentStorage
from .services.runtime_settings import RuntimeSettingsStore

DEFAULT_PROVIDER_CAPABILITIES = ProviderCapabilities(
    protocol=settings.ai_provider_protocol,
    api_mode=(
        "messages"
        if settings.ai_provider_protocol == "anthropic"
        else settings.openai_api_mode
    ),
    structured_output=True,
    streaming=True,
    reasoning_mode=settings.openai_reasoning_mode,
)

logger = logging.getLogger(__name__)
LEARNING_TASK_CONCURRENCY = 2


def provider_capabilities(config: dict) -> ProviderCapabilities:
    return ProviderCapabilities(
        protocol=config["provider_protocol"],
        api_mode=(
            "messages"
            if config["provider_protocol"] == "anthropic"
            else config["api_mode"]
        ),
        structured_output=True,
        streaming=True,
        reasoning_mode=config["reasoning_mode"],
    )


def build_provider_adapter(
    api_key: str,
    model: str,
    base_url: str,
    capabilities: ProviderCapabilities,
):
    if capabilities.protocol == "anthropic":
        return AnthropicAdapter(
            api_key,
            model,
            base_url,
            capabilities=capabilities,
        )
    return OpenAiAdapter(
        api_key,
        model,
        base_url,
        capabilities=capabilities,
        request_timeout_seconds=settings.ai_request_timeout_seconds,
    )


def fallback_model_profiles(config: dict) -> list[dict]:
    enabled_bundled_models = {
        item.strip()
        for item in settings.ai_fallback_models.split(",")
        if item.strip()
    }
    configured = config.get("fallbacks")
    if configured:
        profiles = list(configured)
    elif (
        config.get("provider_protocol") != "openai"
        or "aliyuncs.com" not in str(config.get("base_url") or "")
    ):
        profiles = []
    else:
        profiles = []
        for model in (
            item.strip()
            for item in settings.ai_fallback_models.split(",")
            if item.strip()
        ):
            profiles.append({
                "model": model,
                "providerProtocol": "openai",
                "apiMode": (
                    "responses"
                    if model == "qwen3.8-max-preview"
                    else "chat_completions"
                ),
                "reasoningMode": (
                    "required" if model == "kimi/kimi-k3" else "optional"
                ),
                "apiKey": (
                    settings.qwen38_api_key
                    if model == "qwen3.8-max-preview"
                    else settings.kimi_k3_api_key
                    if model == "kimi/kimi-k3"
                    else ""
                ),
                "baseUrl": (
                    settings.qwen38_base_url
                    if model == "qwen3.8-max-preview"
                    else settings.kimi_k3_base_url
                    if model == "kimi/kimi-k3"
                    else ""
                ),
            })
    primary_model = str(config.get("provider_model") or "").strip()
    normalized = []
    seen = set()
    for profile in profiles:
        model = str(profile.get("model") or "").strip()
        if (
            model in {"qwen3.8-max-preview", "kimi/kimi-k3"}
            and model not in enabled_bundled_models
        ):
            continue
        if not model or model == primary_model or model in seen:
            continue
        seen.add(model)
        normalized_profile = {**profile, "model": model}
        if model == "qwen3.8-max-preview":
            # This endpoint defaults to unbounded thinking in Responses mode.
            # Use the compatible chat path so Slow can enforce a small,
            # controlled thinking budget and still receive final JSON content.
            normalized_profile.update({
                "apiMode": "chat_completions",
                "reasoningMode": "required",
            })
        normalized.append(normalized_profile)
    return normalized


def build_runtime_adapter(config: dict):
    primary_capabilities = provider_capabilities(config)
    primary = build_provider_adapter(
        config["api_key"],
        config["provider_model"],
        config["base_url"],
        primary_capabilities,
    )
    adapters = [primary]
    for fallback in fallback_model_profiles(config):
        model = str(fallback["model"])
        if model == config["provider_model"]:
            continue
        fallback_config = {
            "provider_protocol": fallback.get("providerProtocol", "openai"),
            "api_mode": fallback["apiMode"],
            "reasoning_mode": fallback["reasoningMode"],
        }
        adapters.append(build_provider_adapter(
            fallback.get("apiKey") or config["api_key"],
            model,
            fallback.get("baseUrl") or config["base_url"],
            provider_capabilities(fallback_config),
        ))
    return FallbackAiAdapter(adapters) if len(adapters) > 1 else primary


def managed_source_verifier(adapter):
    # The MVP default is deliberately no-network. Rights-grounded material is
    # a separate reviewed workflow and must be injected explicitly; provider
    # availability never enables web fetching or claim verification.
    return ModelOnlySourcePolicy()


def create_app(
    database_url: str | None = None,
    ai=None,
    source_verifier=None,
    attachment_storage=None,
    runtime_settings_path=None,
    *,
    auth_mode: str | None = None,
    app_mode: str | None = None,
    registration_mode: str | None = None,
    alpha_registration_code: str | None = None,
    alpha_registration_daily_limit: int | None = None,
    oidc_client=None,
):
    effective_auth_mode = auth_mode or settings.auth_mode
    effective_app_mode = app_mode or settings.app_mode
    effective_registration_mode = registration_mode or settings.registration_mode
    effective_alpha_registration_code = (
        settings.alpha_registration_code
        if alpha_registration_code is None
        else alpha_registration_code
    )
    effective_alpha_registration_daily_limit = (
        settings.alpha_registration_daily_limit
        if alpha_registration_daily_limit is None
        else alpha_registration_daily_limit
    )
    if effective_app_mode == "production" and effective_auth_mode == "demo":
        raise RuntimeError("Production mode cannot use demo authentication")
    if effective_app_mode == "production" and effective_auth_mode == "local":
        raise RuntimeError("Production mode cannot use local authentication")
    if effective_app_mode == "production" and settings.password_escrow_enabled:
        raise RuntimeError("Production mode cannot enable password escrow")
    if (
        effective_registration_mode != "closed"
        and effective_auth_mode != "password"
    ):
        raise RuntimeError("Registration requires password authentication")
    if (
        effective_registration_mode == "alpha"
        and not effective_alpha_registration_code
    ):
        raise RuntimeError("Alpha registration requires an access code")
    engine, sessions = build_database(database_url or settings.database_url)
    usage_recorder = AiUsageRecorder(sessions)
    if runtime_settings_path is False:
        runtime_store = None
    elif runtime_settings_path is not None:
        runtime_store = RuntimeSettingsStore(Path(runtime_settings_path))
    elif ai is None and database_url is None:
        runtime_store = RuntimeSettingsStore(settings.runtime_ai_config_path)
    else:
        runtime_store = None

    saved_runtime = runtime_store.load() if runtime_store else None
    environment_runtime = {
        "mode": "provider" if settings.openai_api_key else "demo",
        "api_key": settings.openai_api_key,
        "base_url": settings.openai_base_url,
        "provider_model": settings.openai_model,
        "provider_protocol": settings.ai_provider_protocol,
        "api_mode": (
            "messages"
            if settings.ai_provider_protocol == "anthropic"
            else settings.openai_api_mode
        ),
        "reasoning_mode": settings.openai_reasoning_mode,
        "fallbacks": [],
    }
    configured_runtime = saved_runtime or environment_runtime
    configured_runtime["fallbacks"] = fallback_model_profiles(configured_runtime)
    configured_capabilities = provider_capabilities(configured_runtime)
    if ai is not None:
        adapter = ai
        initial_runtime = {
            "mode": "injected",
            "api_key": "",
            "base_url": "",
            "provider_model": adapter.model,
            "capabilities": getattr(
                adapter,
                "capabilities",
                DEFAULT_PROVIDER_CAPABILITIES,
            ),
            "fallbacks": [],
        }
    else:
        adapter = (
            build_runtime_adapter(configured_runtime)
            if configured_runtime["mode"] == "provider"
            else LocalDemoAdapter()
        )
        initial_runtime = {
            "mode": configured_runtime["mode"],
            "api_key": configured_runtime["api_key"],
            "base_url": configured_runtime["base_url"],
            "provider_model": configured_runtime["provider_model"],
            "capabilities": configured_capabilities,
            "fallbacks": configured_runtime["fallbacks"],
        }
    if hasattr(adapter, "set_usage_recorder"):
        adapter.set_usage_recorder(usage_recorder)
    verifier = source_verifier or managed_source_verifier(adapter)
    storage = attachment_storage or LocalAttachmentStorage(settings.attachment_storage_dir, settings.attachment_max_bytes)
    configured_oidc = oidc_client
    if effective_auth_mode == "oidc" and configured_oidc is None:
        configured_oidc = OidcClient(
            issuer=settings.oidc_issuer,
            client_id=settings.oidc_client_id,
            client_secret=settings.oidc_client_secret,
            redirect_uri=settings.oidc_redirect_uri,
            scopes=settings.oidc_scopes,
        )
        configured_oidc.validate_configuration()
    worker_id = f"worker_{uuid4().hex}"

    async def task_heartbeat_loop(
        context,
        stopped: asyncio.Event,
    ):
        while not stopped.is_set():
            try:
                await asyncio.wait_for(stopped.wait(), timeout=30)
                break
            except TimeoutError:
                with sessions() as heartbeat_db:
                    if not heartbeat_task(heartbeat_db, context):
                        break

    async def execute_learning_task(task_id: str, app: FastAPI):
        if app.state.learning_task_stop.is_set():
            return
        with sessions() as db:
            context = claim_task(
                db,
                task_id,
                lease_owner=worker_id,
            )
            if not context:
                return
            worker_service = SlowService(
                db,
                app.state.ai,
                app.state.source_verifier,
                app.state.attachment_storage,
                scope=context,
            )
            heartbeat_stop = asyncio.Event()
            heartbeat = asyncio.create_task(
                task_heartbeat_loop(context, heartbeat_stop)
            )
            try:
                with usage_recorder.attributed(context.principal):
                    await worker_service.execute_learning_task(context)
            except Exception:
                logger.exception(
                    "Learning task worker recovered after task %s failed",
                    task_id,
                )
            finally:
                heartbeat_stop.set()
                heartbeat.cancel()
                with suppress(asyncio.CancelledError):
                    await heartbeat

    async def learning_task_worker(app: FastAPI):
        while not app.state.learning_task_stop.is_set():
            app.state.learning_task_wakeup.clear()
            while not app.state.learning_task_stop.is_set():
                with sessions() as db:
                    task_ids = recoverable_task_ids(db)
                if not task_ids:
                    break
                await asyncio.gather(*(
                    execute_learning_task(task_id, app)
                    for task_id in task_ids[:LEARNING_TASK_CONCURRENCY]
                ))
            if app.state.learning_task_stop.is_set():
                break
            try:
                await asyncio.wait_for(
                    app.state.learning_task_wakeup.wait(),
                    timeout=1,
                )
            except TimeoutError:
                pass

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        Base.metadata.create_all(engine)
        if effective_auth_mode == "demo":
            with sessions() as db:
                startup_service = SlowService(
                    db,
                    adapter,
                    verifier,
                    storage,
                    scope=demo_user_scope(DEMO_USER_ID),
                )
                startup_service.ensure_demo_seed()
        elif effective_auth_mode == "local":
            with sessions() as db:
                LocalCredentialService(db).ensure_seed_accounts()
                for persona in LOCAL_DEMO_PERSONAS:
                    SlowService(
                        db,
                        adapter,
                        verifier,
                        storage,
                        scope=demo_user_scope(persona.user_id),
                    ).ensure_demo_seed()
        app.state.sessions, app.state.ai, app.state.source_verifier, app.state.attachment_storage = sessions, adapter, verifier, storage
        app.state.ai_usage_recorder = usage_recorder
        app.state.ai_runtime = initial_runtime
        app.state.runtime_store = runtime_store
        app.state.retired_ai = []
        app.state.runtime_verifier_managed = source_verifier is None
        app.state.learning_task_wakeup = asyncio.Event()
        app.state.learning_task_stop = asyncio.Event()
        worker = asyncio.create_task(learning_task_worker(app))
        app.state.learning_task_wakeup.set()
        yield
        app.state.learning_task_stop.set()
        app.state.learning_task_wakeup.set()
        worker.cancel()
        with suppress(asyncio.CancelledError):
            await worker
        seen = set()
        for candidate in [app.state.ai, *app.state.retired_ai]:
            if id(candidate) not in seen and hasattr(candidate, "close"):
                seen.add(id(candidate))
                await candidate.close()
        if configured_oidc is not None and hasattr(configured_oidc, "close"):
            await configured_oidc.close()
        engine.dispose()

    app = FastAPI(title="Slow API", version="0.1.0", lifespan=lifespan)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=[settings.web_origin],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["Content-Type", "Idempotency-Key", "X-CSRF-Token", "X-Filename"],
    )
    app.state.auth_mode = effective_auth_mode
    app.state.app_mode = effective_app_mode
    app.state.registration_mode = effective_registration_mode
    app.state.alpha_registration_code = effective_alpha_registration_code
    app.state.alpha_registration_daily_limit = (
        effective_alpha_registration_daily_limit
    )
    app.state.oidc = configured_oidc

    @app.exception_handler(AppError)
    async def app_error(_request, error):
        return JSONResponse(
            status_code=error.status,
            content={
                "code": error.code,
                "message": str(error),
                "error": str(error),
                "retryable": error.retryable,
                "operationId": error.operation_id,
                "details": error.details,
            },
        )

    @app.exception_handler(ValueError)
    async def value_error(_request, error):
        return JSONResponse(
            status_code=400,
            content={
                "code": "INVALID_INPUT",
                "message": str(error),
                "error": str(error),
                "retryable": False,
                "operationId": None,
            },
        )

    @app.exception_handler(RequestValidationError)
    async def validation_error(_request, error):
        details = []
        for item in error.errors():
            sanitized = dict(item)
            sanitized.pop("input", None)
            if "ctx" in sanitized:
                sanitized["ctx"] = {
                    key: str(value)
                    for key, value in sanitized["ctx"].items()
                }
            details.append(sanitized)
        return JSONResponse(
            status_code=400,
            content={
                "code": "INVALID_REQUEST",
                "message": "请求参数无效",
                "error": "请求参数无效",
                "retryable": False,
                "operationId": None,
                "details": details,
            },
        )

    def db(request: Request):
        session = request.app.state.sessions()
        try: yield session
        finally: session.close()

    def privacy_required(request: Request) -> bool:
        return request.app.state.auth_mode in {"password", "oidc"}

    async def current_scope(
        request: Request,
        session: Session = Depends(db),
    ):
        if request.app.state.auth_mode == "demo":
            scope = demo_user_scope(DEMO_USER_ID)
        else:
            auth = SessionService(
                session,
                ttl_seconds=settings.session_ttl_seconds,
                idle_timeout_seconds=settings.session_idle_timeout_seconds,
            )
            scope, auth_session = auth.authenticate(
                request.cookies.get(settings.session_cookie_name)
            )
            if request.method not in {"GET", "HEAD", "OPTIONS"}:
                auth.require_csrf(
                    auth_session,
                    request.headers.get("X-CSRF-Token"),
                )
            request.state.auth_session = auth_session
        consent_exempt_paths = {
            "/api/auth/me",
            "/api/auth/logout",
            "/api/auth/password/recovery-code/rotate",
            "/api/privacy",
            "/api/privacy/consent",
            "/api/account/exit",
        }
        if privacy_required(request) and request.url.path not in consent_exempt_paths:
            PrivacyService(session, scope.user_id).require_current(required=True)
        with request.app.state.ai_usage_recorder.attributed(
            scope.principal
        ):
            yield scope

    def service(
        request: Request,
        session: Session = Depends(db),
        scope: UserScope = Depends(current_scope),
    ):
        ProfileService(session, scope.user_id).require_complete()
        return SlowService(
            session,
            request.app.state.ai,
            request.app.state.source_verifier,
            request.app.state.attachment_storage,
            scope=scope,
        )

    def require_local_runtime_access(request: Request):
        host = request.client.host if request.client else ""
        if host not in {"127.0.0.1", "::1", "localhost", "testclient"}:
            raise AppError("运行时 AI 设置仅允许从本机访问", code="AI_RUNTIME_LOCAL_ONLY", status=403)

    def runtime_status(request: Request):
        runtime = request.app.state.ai_runtime
        capabilities = getattr(
            request.app.state.ai,
            "capabilities",
            runtime["capabilities"],
        )
        return {
            "mode": runtime["mode"],
            "configured": bool(request.app.state.ai.configured),
            "model": request.app.state.ai.model,
            "providerModel": runtime["provider_model"],
            "fallbackModels": [
                item["model"] for item in runtime.get("fallbacks", [])
            ],
            "baseUrl": runtime["base_url"],
            "apiKeyStored": bool(runtime["api_key"]),
            "ephemeral": request.app.state.runtime_store is None,
            "apiMode": capabilities.api_mode,
            "providerProtocol": capabilities.protocol,
            "reasoningMode": capabilities.reasoning_mode,
            "structuredOutput": capabilities.structured_output,
            "streaming": capabilities.streaming,
        }

    async def attachment_body(request: Request):
        declared = request.headers.get("content-length")
        if declared and int(declared) > settings.attachment_max_bytes:
            raise AppError("附件超过大小限制", code="ATTACHMENT_TOO_LARGE", status=413)
        chunks, size = [], 0
        async for chunk in request.stream():
            size += len(chunk)
            if size > settings.attachment_max_bytes:
                raise AppError("附件超过大小限制", code="ATTACHMENT_TOO_LARGE", status=413)
            chunks.append(chunk)
        return b"".join(chunks)

    @app.get("/api/health")
    def health(request: Request): return {"ok": True, "aiConfigured": request.app.state.ai.configured, "model": request.app.state.ai.model}

    def safe_return_to(value: str) -> str:
        if not value.startswith("/") or value.startswith("//"):
            return "/"
        return value

    def set_session_cookies(
        response: Response,
        request: Request,
        *,
        raw_token: str,
        csrf_token: str,
    ) -> None:
        cookie_secure = (
            settings.session_cookie_secure
            or request.app.state.app_mode == "production"
        )
        response.set_cookie(
            settings.session_cookie_name,
            raw_token,
            max_age=settings.session_ttl_seconds,
            httponly=True,
            secure=cookie_secure,
            samesite="lax",
            path="/",
        )
        response.set_cookie(
            "slow_csrf",
            csrf_token,
            max_age=settings.session_ttl_seconds,
            httponly=False,
            secure=cookie_secure,
            samesite="lax",
            path="/",
        )

    @app.get("/api/auth/config")
    def auth_config(request: Request):
        registration_mode = (
            request.app.state.registration_mode
            if request.app.state.auth_mode == "password"
            else "closed"
        )
        response = {
            "mode": request.app.state.auth_mode,
            "registrationMode": registration_mode,
            "registrationCodeRequired": registration_mode == "alpha",
            "providerName": (
                settings.oidc_provider_name
                if request.app.state.auth_mode == "oidc"
                else ""
            ),
            "privacyNotice": privacy_notice(),
        }
        return response

    @app.get("/api/auth/login")
    async def auth_login(
        request: Request,
        return_to: str = "/",
        session: Session = Depends(db),
    ):
        destination = safe_return_to(return_to)
        if request.app.state.auth_mode == "demo":
            return RedirectResponse(f"{settings.web_origin}{destination}")
        if request.app.state.auth_mode in {"local", "password"}:
            raise AppError(
                "账号密码请在登录页输入",
                code="PASSWORD_LOGIN_FORM_REQUIRED",
                status=400,
            )
        state, login = OidcStateService(session).create(
            return_to=destination,
        )
        authorization_url = await request.app.state.oidc.authorization_url(
            state=state,
            nonce=login.nonce,
            code_verifier=login.code_verifier,
        )
        response = RedirectResponse(authorization_url)
        response.set_cookie(
            "slow_oidc_state",
            state,
            max_age=600,
            httponly=True,
            secure=(
                settings.session_cookie_secure
                or request.app.state.app_mode == "production"
            ),
            samesite="lax",
            path="/api/auth/callback",
        )
        return response

    def password_user_response(
        request: Request,
        user: User,
        session: Session,
        *,
        mode: str,
        status_code: int = 200,
        extra: dict | None = None,
    ):
        session_service = SessionService(
            session,
            ttl_seconds=settings.session_ttl_seconds,
            idle_timeout_seconds=settings.session_idle_timeout_seconds,
        )
        session_service.revoke(
            request.cookies.get(settings.session_cookie_name)
        )
        auth_session, raw_token, csrf_token = session_service.issue(user)
        SlowService(
            session,
            request.app.state.ai,
            request.app.state.source_verifier,
            request.app.state.attachment_storage,
            scope=UserScope(
                Principal(
                    actor_kind="user",
                    actor_id=user.id,
                    subject_user_id=user.id,
                    session_id=auth_session.id,
                )
            ),
        ).ensure_user()
        payload = {
            "authenticated": True,
            "mode": mode,
            "user": {"id": user.id, "name": user.name},
            "csrfToken": csrf_token,
            "privacy": PrivacyService(session, user.id).state(
                required=privacy_required(request)
            ),
            "onboarding": ProfileService(session, user.id).state(),
        }
        if extra:
            payload.update(extra)
        response = JSONResponse(payload, status_code=status_code)
        set_session_cookies(
            response,
            request,
            raw_token=raw_token,
            csrf_token=csrf_token,
        )
        return response

    def password_login_response(
        request: Request,
        body: PasswordLogin,
        session: Session,
        *,
        mode: str,
    ):
        credential_service = (
            LocalCredentialService(session)
            if mode == "local"
            else PasswordCredentialService(session)
        )
        user = credential_service.authenticate(
            username=body.username,
            password=body.password.get_secret_value(),
        )
        return password_user_response(
            request,
            user,
            session,
            mode=mode,
        )

    @app.post("/api/auth/password/login")
    def password_auth_login(
        request: Request,
        body: PasswordLogin,
        session: Session = Depends(db),
    ):
        if request.app.state.auth_mode != "password":
            raise AppError(
                "当前未启用正式账号密码登录",
                code="PASSWORD_AUTH_NOT_ENABLED",
                status=404,
            )
        return password_login_response(
            request,
            body,
            session,
            mode="password",
        )

    @app.post("/api/auth/password/register")
    def password_auth_register(
        request: Request,
        body: PasswordRegistration,
        session: Session = Depends(db),
    ):
        if (
            request.app.state.auth_mode != "password"
            or request.app.state.registration_mode == "closed"
        ):
            raise AppError(
                "Alpha 注册当前未开放",
                code="REGISTRATION_NOT_AVAILABLE",
                status=404,
            )
        if request.app.state.registration_mode == "alpha":
            supplied_code = (
                body.alpha_code.get_secret_value() if body.alpha_code else ""
            )
            if not hmac.compare_digest(
                request.app.state.alpha_registration_code,
                supplied_code,
            ):
                raise AppError(
                    "Alpha 访问码无效",
                    code="ALPHA_ACCESS_CODE_INVALID",
                    status=403,
                )
        quota_date = now().date().isoformat()
        password = body.password.get_secret_value()
        try:
            if request.app.state.registration_mode == "alpha":
                claim_alpha_registration(
                    session,
                    quota_date=quota_date,
                    daily_limit=request.app.state.alpha_registration_daily_limit,
                )
            user = PasswordCredentialService(session).create_account(
                username=body.username,
                display_name=body.username.strip(),
                password=password,
                registration_source=(
                    "alpha_self_service"
                    if request.app.state.registration_mode == "alpha"
                    else "open_self_service"
                ),
                registration_quota_date=(
                    quota_date
                    if request.app.state.registration_mode == "alpha"
                    else None
                ),
                commit=False,
            )
            recovery_code = AccountRecoveryService(session).issue(
                user_id=user.id,
                commit=False,
            )
            session.commit()
        except Exception:
            session.rollback()
            raise
        return password_user_response(
            request,
            user,
            session,
            mode="password",
            status_code=201,
            extra={"recoveryCode": recovery_code},
        )

    @app.post("/api/auth/password/recover")
    def password_auth_recover(
        request: Request,
        body: PasswordRecoveryReset,
        session: Session = Depends(db),
    ):
        if request.app.state.auth_mode != "password":
            raise AppError(
                "当前未启用正式账号密码登录",
                code="PASSWORD_AUTH_NOT_ENABLED",
                status=404,
            )
        replacement = AccountRecoveryService(session).reset_password(
            username=body.username,
            recovery_code=body.recovery_code.get_secret_value(),
            new_password=body.new_password.get_secret_value(),
        )
        return {
            "reset": True,
            "recoveryCode": replacement,
        }

    @app.post("/api/auth/password/recovery-code/rotate")
    def rotate_password_recovery_code(
        request: Request,
        body: RecoveryCodeRotate,
        scope: UserScope = Depends(current_scope),
        session: Session = Depends(db),
    ):
        if request.app.state.auth_mode != "password":
            raise AppError(
                "当前账号不使用恢复码",
                code="ACCOUNT_RECOVERY_NOT_AVAILABLE",
                status=404,
            )
        PasswordCredentialService(session).verify_current_password(
            user_id=scope.user_id,
            password=body.current_password.get_secret_value(),
        )
        return {
            "recoveryCode": AccountRecoveryService(session).issue(
                user_id=scope.user_id,
            )
        }

    @app.post("/api/auth/local/login")
    def local_auth_login(
        request: Request,
        body: PasswordLogin,
        session: Session = Depends(db),
    ):
        if request.app.state.auth_mode != "local":
            raise AppError(
                "当前未启用本地账号登录",
                code="LOCAL_AUTH_NOT_ENABLED",
                status=404,
            )
        return password_login_response(
            request,
            body,
            session,
            mode="local",
        )

    @app.get("/api/auth/callback")
    async def auth_callback(
        request: Request,
        code: str,
        state: str,
        session: Session = Depends(db),
    ):
        if request.app.state.auth_mode != "oidc":
            raise AppError(
                "当前未启用 OIDC 登录",
                code="OIDC_NOT_ENABLED",
                status=404,
            )
        browser_state = request.cookies.get("slow_oidc_state", "")
        if not browser_state or not hmac.compare_digest(browser_state, state):
            raise AppError(
                "登录请求与当前浏览器不匹配",
                code="OIDC_STATE_BROWSER_MISMATCH",
                status=400,
            )
        login = OidcStateService(session).consume(state)
        identity = await request.app.state.oidc.exchange(
            code=code,
            nonce=login.nonce,
            code_verifier=login.code_verifier,
        )
        user = IdentityService(session).resolve_or_create(
            issuer=identity.issuer,
            subject=identity.subject,
            display_name=identity.display_name,
            email=identity.email,
            email_verified=identity.email_verified,
        )
        session_service = SessionService(
            session,
            ttl_seconds=settings.session_ttl_seconds,
            idle_timeout_seconds=settings.session_idle_timeout_seconds,
        )
        session_service.revoke(
            request.cookies.get(settings.session_cookie_name)
        )
        auth_session, raw_token, csrf_token = session_service.issue(user)
        user_scope = UserScope(
            Principal(
                actor_kind="user",
                actor_id=user.id,
                subject_user_id=user.id,
                session_id=auth_session.id,
            )
        )
        SlowService(
            session,
            request.app.state.ai,
            request.app.state.source_verifier,
            request.app.state.attachment_storage,
            scope=user_scope,
        ).ensure_user()
        response = RedirectResponse(
            f"{settings.web_origin}{safe_return_to(login.return_to)}"
        )
        set_session_cookies(
            response,
            request,
            raw_token=raw_token,
            csrf_token=csrf_token,
        )
        response.delete_cookie(
            "slow_oidc_state",
            path="/api/auth/callback",
        )
        return response

    @app.get("/api/auth/me")
    def auth_me(
        request: Request,
        scope: UserScope = Depends(current_scope),
        session: Session = Depends(db),
    ):
        user = session.get(User, scope.user_id)
        if not user:
            raise AppError(
                "当前用户不存在",
                code="AUTH_USER_MISSING",
                status=401,
            )
        csrf_token = request.cookies.get("slow_csrf", "")
        if request.app.state.auth_mode != "demo":
            auth_session = request.state.auth_session
            if (
                not csrf_token
                or auth_session.csrf_token_hash != token_hash(csrf_token)
            ):
                csrf_token = ""
        return {
            "authenticated": True,
            "mode": request.app.state.auth_mode,
            "user": {"id": user.id, "name": user.name},
            "csrfToken": csrf_token,
            "privacy": PrivacyService(session, user.id).state(
                required=privacy_required(request)
            ),
            "onboarding": ProfileService(session, user.id).state(),
        }

    @app.get("/api/privacy/notice")
    def get_privacy_notice():
        return privacy_notice()

    @app.get("/api/privacy")
    def get_privacy_state(
        request: Request,
        scope: UserScope = Depends(current_scope),
        session: Session = Depends(db),
    ):
        return PrivacyService(session, scope.user_id).state(
            required=privacy_required(request)
        )

    @app.post("/api/privacy/consent")
    def accept_privacy_consent(
        body: PrivacyConsentCreate,
        scope: UserScope = Depends(current_scope),
        session: Session = Depends(db),
    ):
        return PrivacyService(session, scope.user_id).accept(
            privacy_accepted=body.privacy_accepted,
            trial_accepted=body.trial_accepted,
        )

    @app.post("/api/account/exit", status_code=202)
    def request_account_exit(
        body: AccountExitCreate,
        request: Request,
        scope: UserScope = Depends(current_scope),
        session: Session = Depends(db),
    ):
        receipt = PrivacyService(session, scope.user_id).request_exit(
            confirmation=body.confirmation,
            reason=body.reason,
        )
        response = JSONResponse(receipt, status_code=202)
        response.delete_cookie(settings.session_cookie_name, path="/")
        response.delete_cookie("slow_csrf", path="/")
        return response

    @app.post("/api/events/batch", status_code=202)
    def append_product_events(
        body: ProductEventBatch,
        scope: UserScope = Depends(current_scope),
        session: Session = Depends(db),
    ):
        return ProductEventService(session, scope.user_id).append(body.events)

    @app.post("/api/study-activity/heartbeat", status_code=202)
    def record_study_activity(
        body: StudyActivityHeartbeat,
        scope: UserScope = Depends(current_scope),
        session: Session = Depends(db),
    ):
        ProfileService(session, scope.user_id).require_complete()
        return StudyActivityService(
            session,
            user_id=scope.user_id,
        ).heartbeat(body)

    @app.get("/api/study-activity/today")
    def study_activity_today(
        timezone_name: str = Query(alias="timezone", min_length=1, max_length=64),
        scope: UserScope = Depends(current_scope),
        session: Session = Depends(db),
    ):
        ProfileService(session, scope.user_id).require_complete()
        return StudyActivityService(
            session,
            user_id=scope.user_id,
        ).today(timezone_name)

    @app.post("/api/learning-preferences/evidence", status_code=202)
    def record_learning_preference_evidence(
        body: LearningPreferenceEvidenceCreate,
        scope: UserScope = Depends(current_scope),
        session: Session = Depends(db),
    ):
        ProfileService(session, scope.user_id).require_complete()
        context = ActiveLearningContextResolver(session).resolve_section(
            user_id=scope.user_id,
            section_id=body.section_id,
        )
        return LearningPreferenceService(session, scope.user_id).record(
            body,
            shelf_id=context.shelf.id,
        )

    @app.post("/api/learning-preferences/decisions", status_code=201)
    def decide_learning_preference(
        body: LearningPreferenceDecisionCreate,
        scope: UserScope = Depends(current_scope),
        session: Session = Depends(db),
    ):
        ProfileService(session, scope.user_id).require_complete()
        if body.scope_kind == "shelf":
            shelf = session.get(Shelf, body.shelf_id)
            if not shelf or shelf.user_id != scope.user_id:
                raise AppError(
                    "找不到这个书架",
                    code="PREFERENCE_DECISION_SHELF_NOT_FOUND",
                    status=404,
                )
        return LearningPreferenceService(session, scope.user_id).decide(body)

    @app.post("/api/sections/{section_id}/personal-presentation", status_code=201)
    def adopt_personal_presentation(
        section_id: str,
        body: PersonalPresentationAdopt,
        scope: UserScope = Depends(current_scope),
        session: Session = Depends(db),
    ):
        ProfileService(session, scope.user_id).require_complete()
        context = ActiveLearningContextResolver(session).resolve_section(
            user_id=scope.user_id,
            section_id=section_id,
        )
        override = PersonalPresentationService(session, scope.user_id).adopt(
            body,
            section_id=section_id,
        )
        projection = LearningPreferenceService(
            session, scope.user_id
        ).record_adoption(
            body,
            section_id=section_id,
            shelf_id=context.shelf.id,
            presentation=override,
        )
        return {"id": override.id, "status": "active", "projection": projection}

    @app.delete("/api/sections/{section_id}/personal-presentation/{block_id}", status_code=204)
    def restore_personal_presentation(
        section_id: str,
        block_id: str,
        content_version_id: str = Query(alias="contentVersionId"),
        scope: UserScope = Depends(current_scope),
        session: Session = Depends(db),
    ):
        ActiveLearningContextResolver(session).resolve_section(
            user_id=scope.user_id,
            section_id=section_id,
        )
        PersonalPresentationService(session, scope.user_id).restore(
            content_version_id=content_version_id,
            block_id=block_id,
        )

    @app.post("/api/auth/logout", status_code=204)
    def auth_logout(
        request: Request,
        scope: UserScope = Depends(current_scope),
        session: Session = Depends(db),
    ):
        del scope
        response = Response(status_code=204)
        if request.app.state.auth_mode != "demo":
            SessionService(
                session,
                ttl_seconds=settings.session_ttl_seconds,
                idle_timeout_seconds=settings.session_idle_timeout_seconds,
            ).revoke(request.cookies.get(settings.session_cookie_name))
            response.delete_cookie(
                settings.session_cookie_name,
                path="/",
            )
            response.delete_cookie("slow_csrf", path="/")
        return response

    @app.get("/api/onboarding")
    def onboarding(
        scope: UserScope = Depends(current_scope),
        session: Session = Depends(db),
    ):
        return ProfileService(session, scope.user_id).state()

    @app.patch("/api/onboarding/profile")
    def save_onboarding_profile_draft(
        body: ProfileDraftUpdate,
        scope: UserScope = Depends(current_scope),
        session: Session = Depends(db),
    ):
        values = body.model_dump(
            exclude={"current_step"},
            exclude_none=True,
        )
        return ProfileService(session, scope.user_id).save_draft(
            current_step=body.current_step,
            values=values,
        )

    @app.post("/api/onboarding/profile/complete")
    def complete_onboarding_profile(
        body: ProfileComplete,
        scope: UserScope = Depends(current_scope),
        session: Session = Depends(db),
    ):
        return ProfileService(session, scope.user_id).complete(
            body.model_dump()
        )

    @app.put("/api/profile")
    def update_profile(
        body: ProfileComplete,
        scope: UserScope = Depends(current_scope),
        session: Session = Depends(db),
    ):
        return ProfileService(session, scope.user_id).complete(
            body.model_dump(),
            source="self_correction",
        )["profile"]

    @app.get("/api/runtime/ai")
    def get_runtime_ai(request: Request):
        require_local_runtime_access(request)
        return runtime_status(request)

    @app.put("/api/runtime/ai")
    async def update_runtime_ai(body: AiRuntimeUpdate, request: Request):
        require_local_runtime_access(request)
        current = request.app.state.ai_runtime
        if body.mode == "demo":
            candidate = LocalDemoAdapter()
            next_runtime = {**current, "mode": "demo", "capabilities": candidate.capabilities}
        else:
            api_key = body.api_key.get_secret_value().strip() if body.api_key else current["api_key"]
            if not api_key:
                raise AppError("请填写 API Key", code="AI_RUNTIME_KEY_REQUIRED", status=400)
            base_url = body.base_url.strip()
            if base_url:
                parsed = urlparse(base_url)
                local_http = parsed.scheme == "http" and parsed.hostname in {"127.0.0.1", "::1", "localhost"}
                if parsed.scheme != "https" and not local_http:
                    raise AppError("Base URL 必须使用 HTTPS；本机服务可使用 HTTP", code="AI_RUNTIME_BASE_URL_INVALID", status=400)
            capabilities = ProviderCapabilities(
                protocol=body.provider_protocol,
                api_mode=(
                    "messages"
                    if body.provider_protocol == "anthropic"
                    else body.api_mode
                ),
                structured_output=True,
                streaming=True,
                reasoning_mode=body.reasoning_mode,
            )
            fallback_profiles = (
                current.get("fallbacks", [])
                if base_url == current.get("base_url")
                else []
            )
            candidate_runtime = {
                "api_key": api_key,
                "base_url": base_url,
                "provider_model": body.model.strip(),
                "provider_protocol": body.provider_protocol,
                "api_mode": body.api_mode,
                "reasoning_mode": body.reasoning_mode,
                "fallbacks": fallback_profiles,
            }
            candidate_runtime["fallbacks"] = fallback_model_profiles(
                candidate_runtime
            )
            candidate = build_runtime_adapter(candidate_runtime)
            if hasattr(candidate, "set_usage_recorder"):
                candidate.set_usage_recorder(request.app.state.ai_usage_recorder)
            try:
                primary_check = getattr(
                    candidate,
                    "check_primary_connection",
                    candidate.check_connection,
                )
                await primary_check()
            except Exception:
                await candidate.close()
                raise AppError("连接验证失败，请检查 API Key、Base URL 和模型名称", code="AI_RUNTIME_CONNECTION_FAILED", status=400)
            next_runtime = {
                "mode": "provider",
                "api_key": api_key,
                "base_url": base_url,
                "provider_model": body.model.strip(),
                "capabilities": capabilities,
                "fallbacks": candidate_runtime["fallbacks"],
            }
        if request.app.state.runtime_store:
            try:
                request.app.state.runtime_store.save(next_runtime)
            except Exception as error:
                await candidate.close()
                raise AppError(
                    "AI 配置验证成功，但无法保存到本机服务端",
                    code="AI_RUNTIME_PERSIST_FAILED",
                    status=500,
                ) from error
        previous = request.app.state.ai
        request.app.state.ai = candidate
        request.app.state.ai_runtime = next_runtime
        request.app.state.retired_ai.append(previous)
        if request.app.state.runtime_verifier_managed:
            request.app.state.source_verifier = managed_source_verifier(candidate)
        return runtime_status(request)

    @app.post("/api/runtime/remediations/{remediation_id}/regenerate")
    async def regenerate_runtime_remediation(
        remediation_id: str,
        request: Request,
        session: Session = Depends(db),
    ):
        require_local_runtime_access(request)
        remediation = session.get(Remediation, remediation_id)
        if not remediation:
            raise AppError(
                "补救内容不存在",
                code="REMEDIATION_NOT_FOUND",
                status=404,
            )
        attempt = session.get(QuizAttempt, remediation.attempt_id)
        if not attempt:
            raise AppError(
                "补救内容对应的答题记录不存在",
                code="REMEDIATION_ATTEMPT_NOT_FOUND",
                status=409,
            )
        user_scope = UserScope(
            Principal(
                actor_kind="user",
                actor_id=attempt.user_id,
                subject_user_id=attempt.user_id,
                session_id=None,
            )
        )
        maintenance_service = SlowService(
            session,
            request.app.state.ai,
            request.app.state.source_verifier,
            request.app.state.attachment_storage,
            scope=user_scope,
        )
        with request.app.state.ai_usage_recorder.attributed(
            user_scope.principal
        ):
            return await maintenance_service.generate_section(
                remediation.section_id,
                retry=True,
                retry_attempt_id=remediation.attempt_id,
                supersede_remediation_id=remediation.id,
            )

    @app.get("/api/bootstrap")
    def bootstrap(s: SlowService = Depends(service)): return s.bootstrap()

    @app.get("/api/daily-mode")
    def daily_mode(s: SlowService = Depends(service)):
        return s.daily_mode()

    @app.put("/api/daily-mode")
    def update_daily_mode(
        body: DailyModeUpdate,
        idempotency_key: str = Header(alias="Idempotency-Key"),
        s: SlowService = Depends(service),
    ):
        return s.update_daily_mode(body, idempotency_key)

    @app.post("/api/feedback", status_code=201)
    async def submit_feedback(
        request: Request,
        body: FeedbackCreate,
        idempotency_key: str = Header(alias="Idempotency-Key"),
        session: Session = Depends(db),
        scope: UserScope = Depends(current_scope),
    ):
        result = FeedbackService(
            session,
            scope,
            source_mode=request.app.state.app_mode,
        ).submit(body, idempotency_key)
        return result

    @app.post("/api/feedback/{feedback_id}/repair/stream")
    async def stream_feedback_repair(
        feedback_id: str,
        s: SlowService = Depends(service),
    ):
        async def events():
            event_id = 0
            try:
                async for event in s.feedback_repair_stream(feedback_id):
                    event_id += 1
                    event_type = str(event.get("type") or "message")
                    payload = json.dumps(event, ensure_ascii=False)
                    yield (
                        f"id: {event_id}\n"
                        f"event: {event_type}\n"
                        f"data: {payload}\n\n"
                    )
            except Exception as error:
                event_id += 1
                payload = json.dumps(
                    {
                        "type": "error",
                        "code": getattr(error, "code", "FEEDBACK_REPAIR_FAILED"),
                        "message": (
                            str(error)
                            if isinstance(error, AppError)
                            else "补救内容生成失败，请重试"
                        ),
                        "retryable": getattr(error, "retryable", True),
                    },
                    ensure_ascii=False,
                )
                yield (
                    f"id: {event_id}\n"
                    "event: error\n"
                    f"data: {payload}\n\n"
                )

        return StreamingResponse(
            events(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache, no-transform",
                "X-Accel-Buffering": "no",
            },
        )

    @app.put("/api/sections/{section_id}/resume")
    def update_resume(
        section_id: str,
        body: ResumeUpdate,
        s: SlowService = Depends(service),
    ):
        return s.record_resume_position(section_id, body.block_id)

    @app.post("/api/shelves", status_code=201)
    def create_shelf(body: ShelfCreate, s: SlowService = Depends(service)): return s.create_shelf(body)

    @app.post("/api/plans", status_code=201)
    async def create_plan(
        request: Request,
        body: PlanCreate,
        idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
        s: SlowService = Depends(service),
    ):
        result = await s.create_plan(body, idempotency_key)
        request.app.state.learning_task_wakeup.set()
        return result

    @app.get("/api/series/{series_id}")
    def series(series_id: str, s: SlowService = Depends(service)): return s.series(series_id)

    @app.get("/api/series/{series_id}/mission")
    def mission(series_id: str, s: SlowService = Depends(service)):
        return s.mission(series_id)

    @app.post("/api/series/{series_id}/mission-versions", status_code=201)
    def create_mission_version(
        series_id: str,
        body: MissionVersionCreate,
        s: SlowService = Depends(service),
    ):
        return s.create_mission_version(series_id, body)

    @app.post(
        "/api/series/{series_id}/mission-versions/{mission_version_id}/confirm"
    )
    def confirm_mission_version(
        series_id: str,
        mission_version_id: str,
        s: SlowService = Depends(service),
    ):
        return s.confirm_mission_version(series_id, mission_version_id)

    @app.post("/api/series/{series_id}/mission-adoptions")
    def adopt_mission_version(
        series_id: str,
        body: MissionAdoptionCreate,
        idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
        s: SlowService = Depends(service),
    ):
        return s.adopt_mission_version(series_id, body, idempotency_key)

    @app.post("/api/series/{series_id}/milestone-path/confirm")
    def confirm_milestone_path(
        series_id: str,
        s: SlowService = Depends(service),
    ):
        return s.confirm_milestone_path(series_id)

    @app.delete("/api/series/{series_id}", status_code=204)
    def delete_series(series_id: str, s: SlowService = Depends(service)): s.delete_series(series_id)

    @app.get("/api/books/{book_id}")
    def book(book_id: str, s: SlowService = Depends(service)): return s.book(book_id)

    @app.delete("/api/books/{book_id}", status_code=204)
    def delete_book(book_id: str, s: SlowService = Depends(service)): s.delete_book(book_id)

    @app.post("/api/books/{book_id}/chapters", status_code=201)
    def add_chapter(book_id: str, body: ChapterCreate, s: SlowService = Depends(service)): return s.add_chapter(book_id, body)

    @app.put("/api/books/{book_id}/chapters/order")
    def reorder_chapters(book_id: str, body: ChapterOrder, s: SlowService = Depends(service)): return s.reorder_chapters(book_id, body.chapter_ids)

    @app.post("/api/books/{book_id}/chapters/replan")
    async def replan_chapters(book_id: str, s: SlowService = Depends(service)): return await s.replan_chapters(book_id)

    @app.post("/api/books/{book_id}/chapters/replan/{proposal_id}/confirm")
    def confirm_replan(book_id: str, proposal_id: str, s: SlowService = Depends(service)): return s.confirm_replan(book_id, proposal_id)

    @app.patch("/api/chapters/{chapter_id}")
    def update_chapter(chapter_id: str, body: ChapterUpdate, s: SlowService = Depends(service)): return s.update_chapter(chapter_id, body)

    @app.delete("/api/chapters/{chapter_id}", status_code=204)
    def delete_chapter(chapter_id: str, s: SlowService = Depends(service)): s.delete_chapter(chapter_id)

    @app.post("/api/chapters/{chapter_id}/generate")
    async def generate_chapter(chapter_id: str, s: SlowService = Depends(service)): return await s.generate_chapter(chapter_id)

    @app.get("/api/sections/{section_id}")
    def section(section_id: str, s: SlowService = Depends(service)): return s.section(section_id)

    @app.post("/api/sections/{section_id}/open")
    def open_section(section_id: str, s: SlowService = Depends(service)):
        return s.open_section(section_id)

    @app.post("/api/sections/{section_id}/generate")
    async def generate_section(section_id: str, s: SlowService = Depends(service)): return await s.generate_section(section_id)

    @app.post("/api/sections/{section_id}/prepare")
    async def prepare_section(section_id: str, s: SlowService = Depends(service)):
        return await s.prepare_section(section_id)

    @app.post("/api/sections/{section_id}/regenerate")
    async def regenerate_section(section_id: str, s: SlowService = Depends(service)):
        return await s.generate_section(section_id, regenerate=True)

    @app.post("/api/sections/{section_id}/quiz")
    async def quiz(
        request: Request,
        section_id: str,
        body: QuizSubmit,
        idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
        s: SlowService = Depends(service),
    ):
        result = await s.submit_quiz(section_id, body, idempotency_key)
        request.app.state.learning_task_wakeup.set()
        return result

    @app.post("/api/sections/{section_id}/quiz-attempts/{attempt_id}/reassess")
    def reassess_quiz_attempt(
        request: Request,
        section_id: str,
        attempt_id: str,
        s: SlowService = Depends(service),
    ):
        result = s.reassess_quiz_attempt(section_id, attempt_id)
        request.app.state.learning_task_wakeup.set()
        return result

    @app.get("/api/reviews/due")
    def due_reviews(
        daily_budget: int = Query(default=10, ge=0, le=100),
        s: SlowService = Depends(service),
    ):
        return s.due_reviews(daily_budget)

    @app.get("/api/knowledge-map")
    def knowledge_map(
        series_id: str | None = Query(default=None),
        s: SlowService = Depends(service),
    ):
        return s.knowledge_map(series_id)

    @app.post("/api/reviews/{assignment_id}/start")
    async def start_review(
        assignment_id: str,
        s: SlowService = Depends(service),
    ):
        return await s.start_review(assignment_id)

    @app.post("/api/reviews/{assignment_id}/submit")
    def submit_review(
        assignment_id: str,
        body: ReviewSubmit,
        idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
        s: SlowService = Depends(service),
    ):
        return s.submit_review(assignment_id, body, idempotency_key)

    @app.post("/api/reviews/{assignment_id}/skip")
    def skip_review(
        assignment_id: str,
        s: SlowService = Depends(service),
    ):
        return s.skip_review(assignment_id)

    @app.post("/api/reviews/{assignment_id}/expire")
    def expire_review(
        assignment_id: str,
        s: SlowService = Depends(service),
    ):
        return s.expire_review(assignment_id)

    @app.post("/api/sections/{section_id}/ask")
    async def ask(section_id: str, body: AskRequest, s: SlowService = Depends(service)): return await s.ask(section_id, body)

    @app.get("/api/sections/{section_id}/qa/history")
    def qa_history(section_id: str, s: SlowService = Depends(service)):
        return s.qa_history(section_id)

    @app.post("/api/sections/{section_id}/ask/stream")
    async def ask_stream(section_id: str, body: AskRequest, s: SlowService = Depends(service)):
        context = s.prepare_ask(section_id, body)

        async def events():
            try:
                async for event in s.ask_stream(context, body):
                    yield json.dumps(event, ensure_ascii=False) + "\n"
            except Exception as error:
                yield json.dumps(
                    {
                        "type": "error",
                        "code": getattr(error, "code", "QA_STREAM_FAILED"),
                        "message": str(error) if isinstance(error, AppError) else "答疑生成失败，请稍后重试",
                        "error": str(error) if isinstance(error, AppError) else "答疑生成失败，请稍后重试",
                        "retryable": getattr(error, "retryable", True),
                        "operationId": getattr(error, "operation_id", None),
                    },
                    ensure_ascii=False,
                ) + "\n"

        return StreamingResponse(events(), media_type="application/x-ndjson")

    @app.patch("/api/sections/{section_id}/qa/threads/{thread_id}")
    def correct_qa(section_id: str, thread_id: str, body: QaClassificationUpdate, s: SlowService = Depends(service)): return s.correct_qa_classification(section_id, thread_id, body)

    @app.post("/api/sections/{section_id}/ask-me")
    async def ask_me(section_id: str, body: AskMeReply, s: SlowService = Depends(service)): return await s.ask_me(section_id, body.answer)

    @app.get("/api/sections/{section_id}/ask-me/discussion")
    def ask_me_discussion(section_id: str, s: SlowService = Depends(service)):
        return s.ask_me_discussion(section_id)

    @app.post("/api/sections/{section_id}/ask-me/discussion")
    def start_ask_me_discussion(
        section_id: str,
        s: SlowService = Depends(service),
    ):
        return s.start_ask_me_discussion(section_id)

    @app.post("/api/sections/{section_id}/ask-me/discussion/turns")
    async def submit_ask_me_discussion_turn(
        section_id: str,
        body: AskMeDiscussionTurnCreate,
        idempotency_key: str = Header(alias="Idempotency-Key"),
        s: SlowService = Depends(service),
    ):
        return await s.submit_ask_me_discussion_turn(
            section_id,
            body,
            idempotency_key,
        )

    @app.post("/api/sections/{section_id}/ask-me/discussion/actions")
    def apply_ask_me_discussion_action(
        section_id: str,
        body: AskMeDiscussionAction,
        idempotency_key: str = Header(alias="Idempotency-Key"),
        s: SlowService = Depends(service),
    ):
        return s.apply_ask_me_discussion_action(
            section_id,
            body,
            idempotency_key,
        )

    @app.get("/api/chapters/{chapter_id}/practice")
    def practice(chapter_id: str, s: SlowService = Depends(service)): return s.chapter_practice(chapter_id)

    @app.post("/api/chapters/{chapter_id}/practice")
    def submit_practice(chapter_id: str, body: AttachmentSubmit, s: SlowService = Depends(service)): return s.submit_chapter_practice(chapter_id, body.content, body.attachment_ids)

    @app.post("/api/chapters/{chapter_id}/practice/attachments", status_code=201)
    async def upload_practice_attachment(chapter_id: str, request: Request, s: SlowService = Depends(service)):
        return s.upload_chapter_practice_attachment(chapter_id, request.headers.get("x-filename", "attachment.bin"), request.headers.get("content-type", "application/octet-stream"), await attachment_body(request))

    @app.get("/api/books/{book_id}/capstone")
    def capstone(book_id: str, s: SlowService = Depends(service)): return s.book_capstone(book_id)

    @app.post("/api/books/{book_id}/capstone")
    def submit_capstone(book_id: str, body: AttachmentSubmit, s: SlowService = Depends(service)): return s.submit_book_capstone(book_id, body.content, body.attachment_ids)

    @app.post("/api/books/{book_id}/capstone/attachments", status_code=201)
    async def upload_capstone_attachment(book_id: str, request: Request, s: SlowService = Depends(service)):
        return s.upload_book_capstone_attachment(book_id, request.headers.get("x-filename", "attachment.bin"), request.headers.get("content-type", "application/octet-stream"), await attachment_body(request))

    @app.get("/api/attachments/{attachment_id}")
    def download_attachment(attachment_id: str, s: SlowService = Depends(service)):
        item, path = s.attachment(attachment_id)
        return FileResponse(path, media_type=item.media_type, filename=item.original_filename)

    @app.get("/api/learning-memory")
    def learning_memory(shelf_id: str | None = None, s: SlowService = Depends(service)): return s.learning_memory(shelf_id)

    @app.patch("/api/sections/{section_id}/note")
    def note(section_id: str, body: NoteUpdate, s: SlowService = Depends(service)): return s.update_note(section_id, body.content)

    @app.post("/api/sections/{section_id}/note/review-supplements", status_code=201)
    def add_note_review_supplement(
        section_id: str,
        body: NoteReviewSupplementCreate,
        s: SlowService = Depends(service),
    ):
        return s.add_note_review_supplement(
            section_id,
            body.review_episode_id,
            body.content,
        )

    @app.get("/api/learning-tasks/{task_id}")
    def learning_task(task_id: str, s: SlowService = Depends(service)):
        return s.learning_task(task_id)

    @app.post("/api/learning-tasks/{task_id}/retry")
    def retry_learning_task(
        task_id: str,
        request: Request,
        s: SlowService = Depends(service),
    ):
        result = s.retry_learning_task(task_id)
        request.app.state.learning_task_wakeup.set()
        return result

    @app.post("/api/note-tasks/{task_id}/retry")
    def retry_note_task(
        task_id: str,
        request: Request,
        s: SlowService = Depends(service),
    ):
        result = s.retry_note_task(task_id)
        request.app.state.learning_task_wakeup.set()
        return result

    return app


app = create_app()
