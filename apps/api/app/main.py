import asyncio
from contextlib import asynccontextmanager
from contextlib import suppress
import hmac
import json
from pathlib import Path
from urllib.parse import urlparse
from uuid import uuid4
from fastapi import Depends, FastAPI, Header, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, StreamingResponse
from sqlalchemy.orm import Session
from .auth.context import Principal, UserScope, demo_user_scope
from .auth.oidc import OidcClient
from .auth.service import IdentityService, OidcStateService, SessionService, token_hash
from .ai.openai_adapter import OpenAiAdapter
from .ai.anthropic_adapter import AnthropicAdapter
from .ai.local_adapter import LocalDemoAdapter
from .ai.port import ProviderCapabilities
from .ai.metering import AiUsageRecorder
from .api.schemas import AiRuntimeUpdate, AskMeReply, AskRequest, AttachmentSubmit, ChapterCreate, ChapterOrder, ChapterUpdate, NoteUpdate, PlanCreate, QaClassificationUpdate, QuizSubmit, ResumeUpdate, ShelfCreate
from .application.service import DEMO_USER_ID, SlowService
from .core.config import settings
from .core.errors import AppError
from .infrastructure.database import build_database
from .infrastructure.tables import Base, LearningTask, User
from .modules.learning.tasks import claim_task, heartbeat_task, recoverable_task_ids
from .services.source_verifier import AcceptingSourceVerifier, HttpSourceVerifier
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
    )


def create_app(
    database_url: str | None = None,
    ai=None,
    source_verifier=None,
    attachment_storage=None,
    runtime_settings_path=None,
    *,
    auth_mode: str | None = None,
    app_mode: str | None = None,
    oidc_client=None,
):
    effective_auth_mode = auth_mode or settings.auth_mode
    effective_app_mode = app_mode or settings.app_mode
    if effective_app_mode == "production" and effective_auth_mode == "demo":
        raise RuntimeError("Production mode cannot use demo authentication")
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
    }
    configured_runtime = saved_runtime or environment_runtime
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
        }
    else:
        adapter = (
            build_provider_adapter(
                configured_runtime["api_key"],
                configured_runtime["provider_model"],
                configured_runtime["base_url"],
                configured_capabilities,
            )
            if configured_runtime["mode"] == "provider"
            else LocalDemoAdapter()
        )
        initial_runtime = {
            "mode": configured_runtime["mode"],
            "api_key": configured_runtime["api_key"],
            "base_url": configured_runtime["base_url"],
            "provider_model": configured_runtime["provider_model"],
            "capabilities": configured_capabilities,
        }
    if hasattr(adapter, "set_usage_recorder"):
        adapter.set_usage_recorder(usage_recorder)
    verifier = source_verifier or (HttpSourceVerifier() if adapter.configured else AcceptingSourceVerifier())
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

    async def learning_task_worker(app: FastAPI):
        while not app.state.learning_task_stop.is_set():
            app.state.learning_task_wakeup.clear()
            while not app.state.learning_task_stop.is_set():
                with sessions() as db:
                    task_ids = recoverable_task_ids(db)
                if not task_ids:
                    break
                for task_id in task_ids:
                    if app.state.learning_task_stop.is_set():
                        break
                    with sessions() as db:
                        context = claim_task(
                            db,
                            task_id,
                            lease_owner=worker_id,
                        )
                        if not context:
                            continue
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
                                await worker_service.execute_learning_task(
                                    context
                                )
                        finally:
                            heartbeat_stop.set()
                            heartbeat.cancel()
                            with suppress(asyncio.CancelledError):
                                await heartbeat
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
                startup_service.ensure_seed()
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
        return JSONResponse(
            status_code=400,
            content={
                "code": "INVALID_REQUEST",
                "message": "请求参数无效",
                "error": "请求参数无效",
                "retryable": False,
                "operationId": None,
                "details": error.errors(),
            },
        )

    def db(request: Request):
        session = request.app.state.sessions()
        try: yield session
        finally: session.close()

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
        with request.app.state.ai_usage_recorder.attributed(
            scope.principal
        ):
            yield scope

    def service(
        request: Request,
        session: Session = Depends(db),
        scope: UserScope = Depends(current_scope),
    ):
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

    @app.get("/api/auth/login")
    async def auth_login(
        request: Request,
        return_to: str = "/",
        session: Session = Depends(db),
    ):
        destination = safe_return_to(return_to)
        if request.app.state.auth_mode == "demo":
            return RedirectResponse(f"{settings.web_origin}{destination}")
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
        ).ensure_seed()
        response = RedirectResponse(
            f"{settings.web_origin}{safe_return_to(login.return_to)}"
        )
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
        if request.app.state.auth_mode == "oidc":
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
        }

    @app.post("/api/auth/logout", status_code=204)
    def auth_logout(
        request: Request,
        scope: UserScope = Depends(current_scope),
        session: Session = Depends(db),
    ):
        del scope
        response = JSONResponse(status_code=204, content=None)
        if request.app.state.auth_mode == "oidc":
            SessionService(
                session,
                ttl_seconds=settings.session_ttl_seconds,
            ).revoke(request.cookies.get(settings.session_cookie_name))
            response.delete_cookie(
                settings.session_cookie_name,
                path="/",
            )
            response.delete_cookie("slow_csrf", path="/")
        return response

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
            candidate = build_provider_adapter(
                api_key,
                body.model.strip(),
                base_url,
                capabilities,
            )
            if hasattr(candidate, "set_usage_recorder"):
                candidate.set_usage_recorder(request.app.state.ai_usage_recorder)
            try:
                await candidate.check_connection()
            except Exception:
                await candidate.close()
                raise AppError("连接验证失败，请检查 API Key、Base URL 和模型名称", code="AI_RUNTIME_CONNECTION_FAILED", status=400)
            next_runtime = {
                "mode": "provider",
                "api_key": api_key,
                "base_url": base_url,
                "provider_model": body.model.strip(),
                "capabilities": capabilities,
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
            request.app.state.source_verifier = HttpSourceVerifier() if candidate.configured else AcceptingSourceVerifier()
        return runtime_status(request)

    @app.get("/api/bootstrap")
    def bootstrap(s: SlowService = Depends(service)): return s.bootstrap()

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

    @app.post("/api/sections/{section_id}/generate")
    async def generate_section(section_id: str, s: SlowService = Depends(service)): return await s.generate_section(section_id)

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

    @app.post("/api/sections/{section_id}/ask")
    async def ask(section_id: str, body: AskRequest, s: SlowService = Depends(service)): return await s.ask(section_id, body)

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
