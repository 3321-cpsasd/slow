from contextlib import asynccontextmanager
import json
from urllib.parse import urlparse
from fastapi import Depends, FastAPI, Header, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from sqlalchemy.orm import Session
from .ai.openai_adapter import OpenAiAdapter
from .ai.local_adapter import LocalDemoAdapter
from .api.schemas import AiRuntimeUpdate, AskMeReply, AskRequest, AttachmentSubmit, ChapterCreate, ChapterOrder, ChapterUpdate, NoteUpdate, PlanCreate, QaClassificationUpdate, QuizSubmit, ShelfCreate
from .application.service import SlowService
from .core.config import settings
from .core.errors import AppError
from .infrastructure.database import build_database
from .infrastructure.tables import Base
from .services.source_verifier import AcceptingSourceVerifier, HttpSourceVerifier
from .services.attachment_storage import LocalAttachmentStorage


def create_app(database_url: str | None = None, ai=None, source_verifier=None, attachment_storage=None):
    engine, sessions = build_database(database_url or settings.database_url)
    adapter = ai or (OpenAiAdapter(settings.openai_api_key, settings.openai_model, settings.openai_base_url) if settings.openai_api_key else LocalDemoAdapter())
    initial_runtime = {
        "mode": "injected" if ai is not None else ("provider" if settings.openai_api_key else "demo"),
        "api_key": "" if ai is not None else settings.openai_api_key,
        "base_url": "" if ai is not None else settings.openai_base_url,
        "provider_model": adapter.model if adapter.configured else settings.openai_model,
    }
    verifier = source_verifier or (HttpSourceVerifier() if adapter.configured else AcceptingSourceVerifier())
    storage = attachment_storage or LocalAttachmentStorage(settings.attachment_storage_dir, settings.attachment_max_bytes)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        Base.metadata.create_all(engine)
        with sessions() as db: SlowService(db, adapter, verifier, storage).ensure_seed()
        app.state.sessions, app.state.ai, app.state.source_verifier, app.state.attachment_storage = sessions, adapter, verifier, storage
        app.state.ai_runtime = initial_runtime
        app.state.retired_ai = []
        app.state.runtime_verifier_managed = source_verifier is None
        yield
        seen = set()
        for candidate in [app.state.ai, *app.state.retired_ai]:
            if id(candidate) not in seen and hasattr(candidate, "close"):
                seen.add(id(candidate))
                await candidate.close()
        engine.dispose()

    app = FastAPI(title="Slow API", version="0.1.0", lifespan=lifespan)
    app.add_middleware(CORSMiddleware, allow_origins=[settings.web_origin], allow_methods=["*"], allow_headers=["*"])

    @app.exception_handler(AppError)
    async def app_error(_request, error): return JSONResponse(status_code=error.status, content={"code": error.code, "error": str(error)})

    @app.exception_handler(ValueError)
    async def value_error(_request, error): return JSONResponse(status_code=400, content={"code": "INVALID_INPUT", "error": str(error)})

    @app.exception_handler(RequestValidationError)
    async def validation_error(_request, error): return JSONResponse(status_code=400, content={"code": "INVALID_REQUEST", "error": "请求参数无效", "details": error.errors()})

    def db(request: Request):
        session = request.app.state.sessions()
        try: yield session
        finally: session.close()

    def service(request: Request, session: Session = Depends(db)): return SlowService(session, request.app.state.ai, request.app.state.source_verifier, request.app.state.attachment_storage)

    def require_local_runtime_access(request: Request):
        host = request.client.host if request.client else ""
        if host not in {"127.0.0.1", "::1", "localhost", "testclient"}:
            raise AppError("运行时 AI 设置仅允许从本机访问", code="AI_RUNTIME_LOCAL_ONLY", status=403)

    def runtime_status(request: Request):
        runtime = request.app.state.ai_runtime
        return {
            "mode": runtime["mode"],
            "configured": bool(request.app.state.ai.configured),
            "model": request.app.state.ai.model,
            "providerModel": runtime["provider_model"],
            "baseUrl": runtime["base_url"],
            "apiKeyStored": bool(runtime["api_key"]),
            "ephemeral": True,
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
            next_runtime = {**current, "mode": "demo"}
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
            candidate = OpenAiAdapter(api_key, body.model.strip(), base_url)
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
            }
        previous = request.app.state.ai
        request.app.state.ai = candidate
        request.app.state.ai_runtime = next_runtime
        request.app.state.retired_ai.append(previous)
        if request.app.state.runtime_verifier_managed:
            request.app.state.source_verifier = HttpSourceVerifier() if candidate.configured else AcceptingSourceVerifier()
        return runtime_status(request)

    @app.get("/api/bootstrap")
    def bootstrap(s: SlowService = Depends(service)): return s.bootstrap()

    @app.post("/api/shelves", status_code=201)
    def create_shelf(body: ShelfCreate, s: SlowService = Depends(service)): return s.create_shelf(body)

    @app.post("/api/plans", status_code=201)
    async def create_plan(
        body: PlanCreate,
        idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
        s: SlowService = Depends(service),
    ):
        return await s.create_plan(body, idempotency_key)

    @app.get("/api/series/{series_id}")
    def series(series_id: str, s: SlowService = Depends(service)): return s.series(series_id)

    @app.delete("/api/series/{series_id}", status_code=204)
    def delete_series(series_id: str, s: SlowService = Depends(service)): s.delete_series(series_id)

    @app.get("/api/books/{book_id}")
    def book(book_id: str, s: SlowService = Depends(service)): return s.book(book_id)

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
    async def quiz(section_id: str, body: QuizSubmit, s: SlowService = Depends(service)): return await s.submit_quiz(section_id, body)

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
                        "error": str(error) if isinstance(error, AppError) else "答疑生成失败，请稍后重试",
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

    return app


app = create_app()
