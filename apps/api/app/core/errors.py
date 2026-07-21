class AppError(RuntimeError):
    def __init__(self, message: str, *, code: str, status: int = 400):
        super().__init__(message)
        self.code = code
        self.status = status


class AiError(AppError):
    def __init__(self, message: str = "AI 生成失败"):
        super().__init__(message, code="AI_ERROR", status=502)
