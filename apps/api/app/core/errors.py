class AppError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        code: str,
        status: int = 400,
        retryable: bool = False,
        operation_id: str | None = None,
    ):
        super().__init__(message)
        self.code = code
        self.status = status
        self.retryable = retryable
        self.operation_id = operation_id


class AiError(AppError):
    def __init__(
        self,
        message: str = "AI 生成失败，请稍后重试",
        *,
        code: str = "AI_ERROR",
        retryable: bool = True,
        operation_id: str | None = None,
    ):
        super().__init__(
            message,
            code=code,
            status=502,
            retryable=retryable,
            operation_id=operation_id,
        )
