from contextvars import ContextVar
from typing import Callable

from ..core.errors import AiError, safe_error_code


class FallbackAiAdapter:
    """Retry atomic structured operations across an ordered model chain.

    Every physical provider request remains recorded by the concrete adapter.
    The router only moves to the next model after a retryable AI failure, and
    never combines partial candidates from different models.
    """

    staged_lesson_generation = True

    def __init__(self, adapters: list):
        if not adapters:
            raise ValueError("fallback adapter requires at least one model")
        self.adapters = adapters
        self.capabilities = adapters[0].capabilities
        self._last_model = ContextVar(
            f"fallback_last_model_{id(self)}",
            default=adapters[0].model,
        )
        self._fallback_trace = ContextVar(
            f"fallback_trace_{id(self)}",
            default=(),
        )
        self._structured_trace = ContextVar(
            f"fallback_structured_trace_{id(self)}",
            default=(),
        )

    @property
    def model(self) -> str:
        return self.adapters[0].model

    @property
    def models(self) -> list[str]:
        return [adapter.model for adapter in self.adapters]

    @property
    def last_model(self) -> str:
        return self._last_model.get()

    @property
    def configured(self) -> bool:
        return bool(self.adapters[0].configured)

    @property
    def input_tokens(self) -> int:
        return sum(getattr(adapter, "input_tokens", 0) for adapter in self.adapters)

    @property
    def output_tokens(self) -> int:
        return sum(getattr(adapter, "output_tokens", 0) for adapter in self.adapters)

    def fallback_trace(self) -> list[dict]:
        return list(self._fallback_trace.get())

    def structured_trace(self) -> list[dict]:
        return list(self._structured_trace.get())

    def set_usage_recorder(self, recorder) -> None:
        for adapter in self.adapters:
            adapter.set_usage_recorder(recorder)

    async def close(self) -> None:
        for adapter in self.adapters:
            await adapter.close()

    async def check_connection(self) -> None:
        for adapter in self.adapters:
            await adapter.check_connection()

    async def check_primary_connection(self) -> None:
        """Validate a runtime update without making optional fallbacks a gate."""

        await self.adapters[0].check_connection()

    def __getattr__(self, name):
        # Streaming and interactive tutoring stay on the primary model. Moving
        # a partially streamed answer between models would splice two outputs.
        return getattr(self.adapters[0], name)

    async def _call(
        self,
        method: str,
        *args,
        candidate_validator: Callable | None = None,
        **kwargs,
    ):
        self._fallback_trace.set(())
        self._structured_trace.set(())
        self._last_model.set(self.adapters[0].model)
        attempts: list[dict] = []
        structured: list[dict] = []
        last_error: AiError | None = None
        last_candidate = None
        for index, adapter in enumerate(self.adapters):
            self._last_model.set(adapter.model)
            try:
                candidate = await getattr(adapter, method)(*args, **kwargs)
                adapter_trace = [
                    {**item, "model": adapter.model, "fallbackIndex": index}
                    for item in adapter.structured_trace()
                ]
                structured.extend(adapter_trace)
                if candidate_validator is not None:
                    try:
                        candidate_validator(candidate)
                    except Exception as validation_error:
                        last_candidate = candidate
                        attempts.append({
                            "model": adapter.model,
                            "outcome": "candidate_rejected",
                            "errorCode": safe_error_code(validation_error),
                        })
                        if index + 1 < len(self.adapters):
                            continue
                        self._fallback_trace.set(tuple(attempts))
                        self._structured_trace.set(tuple(structured))
                        return last_candidate
                attempts.append({"model": adapter.model, "outcome": "succeeded"})
                self._fallback_trace.set(tuple(attempts))
                self._structured_trace.set(tuple(structured))
                return candidate
            except AiError as error:
                last_error = error
                structured.extend([
                    {**item, "model": adapter.model, "fallbackIndex": index}
                    for item in adapter.structured_trace()
                ])
                attempts.append({
                    "model": adapter.model,
                    "outcome": "failed",
                    "errorCode": error.code,
                    "retryable": error.retryable,
                })
                if not error.retryable or index + 1 >= len(self.adapters):
                    break
            except BaseException as error:
                structured.extend([
                    {**item, "model": adapter.model, "fallbackIndex": index}
                    for item in adapter.structured_trace()
                ])
                attempts.append({
                    "model": adapter.model,
                    "outcome": "failed",
                    "errorCode": safe_error_code(error),
                    "retryable": False,
                })
                self._fallback_trace.set(tuple(attempts))
                self._structured_trace.set(tuple(structured))
                raise
        self._fallback_trace.set(tuple(attempts))
        self._structured_trace.set(tuple(structured))
        if last_error:
            if len(attempts) > 1:
                # The durable task runner must not immediately replay an entire
                # exhausted model chain. A user can still request an audited
                # retry after the provider or configuration has recovered.
                raise AiError(
                    str(last_error),
                    code=last_error.code,
                    retryable=False,
                    operation_id=last_error.operation_id,
                ) from last_error
            raise last_error
        raise AiError("备用模型链未返回有效结果", code="AI_FALLBACK_EXHAUSTED")

    async def plan(self, request, memory):
        return await self._call("plan", request, memory)

    async def learning_goal_interview(self, request):
        return await self._call("learning_goal_interview", request)

    async def chapter(self, request, memory):
        return await self._call("chapter", request, memory)

    async def review_chapter_outline(self, payload):
        return await self._call("review_chapter_outline", payload)

    async def teaching_blueprint(self, request, memory):
        return await self._call("teaching_blueprint", request, memory)

    async def author_lesson_content(self, spec):
        return await self._call("author_lesson_content", spec)

    async def author_lesson_questions(self, payload):
        return await self._call("author_lesson_questions", payload)

    async def review_lesson_questions(self, payload):
        return await self._call("review_lesson_questions", payload)

    async def adjudicate_lesson_questions(self, payload):
        return await self._call("adjudicate_lesson_questions", payload)

    async def generate_lesson(self, spec):
        return await self._call("generate_lesson", spec)

    async def generate_lesson_validated(self, spec, validator):
        return await self._call(
            "generate_lesson",
            spec,
            candidate_validator=validator,
        )

    async def lesson(self, request, memory, prior_questions=None):
        return await self._call("lesson", request, memory, prior_questions)

    async def lesson_content(self, request, memory, prior_questions=None):
        return await self._call(
            "lesson_content", request, memory, prior_questions
        )

    async def lesson_quiz(self, request, content, prior_questions=None):
        return await self._call(
            "lesson_quiz", request, content, prior_questions
        )

    async def note(self, request):
        return await self._call("note", request)

    async def answer(self, request):
        return await self._call("answer", request)

    async def ask_me(self, request):
        return await self._call("ask_me", request)

    async def ask_me_probe(self, request):
        return await self._call("ask_me_probe", request)

    async def evaluate_ask_me(self, request):
        return await self._call("evaluate_ask_me", request)

    async def ask_me_discussion(self, request):
        return await self._call("ask_me_discussion", request)

    async def evaluate_ask_me_discussion(self, request):
        return await self._call("evaluate_ask_me_discussion", request)

    async def ask_me_discussion_probe(self, request):
        return await self._call("ask_me_discussion_probe", request)

    async def replan_book(self, request, memory):
        return await self._call("replan_book", request, memory)
