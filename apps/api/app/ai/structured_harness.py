import hashlib
import json

from pydantic import ValidationError


MAX_REPAIR_OUTPUT_CHARS = 64_000
MAX_VALIDATION_ISSUES = 12


def clean_json_output(content: str) -> str:
    candidate = content.strip()
    if candidate.startswith("```"):
        candidate = candidate.split("\n", 1)[-1].rsplit("```", 1)[0]
    return candidate.strip()


def output_digest(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()[:16]


def validation_issues(error: ValidationError) -> list[dict]:
    issues = []
    for item in error.errors(
        include_url=False,
        include_context=False,
        include_input=False,
    )[:MAX_VALIDATION_ISSUES]:
        issues.append(
            {
                "path": ".".join(str(part) for part in item.get("loc", ()))
                or "$",
                "type": item.get("type", "validation_error"),
                "message": item.get("msg", "invalid value"),
            }
        )
    return issues


def repair_request(
    *,
    schema,
    developer: str,
    invalid_output: str,
    error: ValidationError,
) -> tuple[str, str]:
    schema_text = json.dumps(schema.model_json_schema(), ensure_ascii=False)
    candidate = clean_json_output(invalid_output)
    if len(candidate) > MAX_REPAIR_OUTPUT_CHARS:
        candidate = candidate[:MAX_REPAIR_OUTPUT_CHARS]
    system = (
        "你是严格的 JSON 结构修复器，不是内容作者。"
        "只修复候选 JSON 的语法、字段、类型、数量和索引，使其通过给定 Schema。"
        "必须保留候选中的原意；不得新增候选中不存在的事实、来源 URL、"
        "技术主张或答案依据。无法在不编造事实的前提下修复时，"
        "仍应尽量保持原值，让服务端校验拒绝，而不是猜测。"
        "只输出一个 JSON 对象，不使用 Markdown。\n"
        f"原始任务约束：\n{developer}\n"
        f"目标 JSON Schema：\n{schema_text}"
    )
    user = json.dumps(
        {
            "invalid_output": candidate,
            "validation_errors": validation_issues(error),
        },
        ensure_ascii=False,
    )
    return system, user


def trace_entry(
    *,
    schema,
    attempts: int,
    invalid_outputs: list[str],
    last_error: ValidationError | None,
    outcome: str,
    token_budgets: list[int] | None = None,
    repair_attempts: int | None = None,
) -> dict:
    entry = {
        "schema": schema.__name__,
        "attempts": attempts,
        "repairAttempts": (
            max(0, attempts - 1)
            if repair_attempts is None
            else repair_attempts
        ),
        "outcome": outcome,
        "invalidOutputDigests": [
            output_digest(content) for content in invalid_outputs
        ],
        "lastValidationIssues": (
            validation_issues(last_error) if last_error else []
        ),
    }
    if token_budgets is not None:
        entry["tokenBudgets"] = token_budgets
    return entry
