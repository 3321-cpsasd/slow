"""Deterministic safety gate for generated series and book outlines.

This is intentionally an intent gate, not a general keyword blacklist.  It
blocks requests that ask for operational help with serious wrongdoing or
harm, while leaving defensive, historical, legal, and risk-awareness study
available.  Provider-side safety remains a second, independent boundary.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass

from ...core.errors import AppError


CONTENT_SAFETY_POLICY_VERSION = "learning_intent_safety_v1"


@dataclass(frozen=True)
class SafetyDecision:
    allowed: bool
    category: str = ""
    rule_id: str = ""
    policy_version: str = CONTENT_SAFETY_POLICY_VERSION


@dataclass(frozen=True)
class _Rule:
    rule_id: str
    category: str
    patterns: tuple[str, ...]


_RULES = (
    _Rule(
        "S1_SEXUAL_EXPLOITATION",
        "sexual_exploitation",
        (
            r"儿童色情|未成年人色情|幼童色情|性侵儿童|诱骗未成年人",
            r"childporn|childsexualabuse|csam",
        ),
    ),
    _Rule(
        "S2_SELF_HARM_INSTRUCTIONS",
        "self_harm",
        (
            r"(?:自杀|自残)(?:方法|教程|技巧|指南|方案|最有效|无痛)",
            r"(?:方法|教程|技巧|指南|无痛|最有效)(?:自杀|自残)",
            r"howto(?:kill|harm)myself|painlesssuicide|suicidemethod",
        ),
    ),
    _Rule(
        "S3_WEAPON_OR_VIOLENT_HARM",
        "violent_wrongdoing",
        (
            r"(?:制造|制作|配制|组装|自制)(?:炸弹|爆炸物|枪支|枪械|毒药|生化武器|燃烧弹)",
            r"(?:炸弹|爆炸物|枪支|枪械|毒药|生化武器|燃烧弹)(?:制作|制造|配方|教程|设计图|组装)",
            r"(?:暗杀|杀害|绑架|投毒)(?:方法|教程|计划|技巧|指南|方案)",
            r"(?:方法|教程|计划|技巧|指南|方案)(?:暗杀|杀害|绑架|投毒)",
            r"howto(?:make|build)(?:abomb|explosives|agun)|assassinationplan",
        ),
    ),
    _Rule(
        "S4_ILLICIT_DRUGS",
        "illicit_drugs",
        (
            r"(?:制毒|炼毒|制造毒品|贩毒|毒品销售)",
            r"(?:冰毒|甲基苯丙胺|芬太尼|海洛因)(?:合成|制造|配方|教程)",
            r"(?:合成|制造|配方|教程)(?:冰毒|甲基苯丙胺|芬太尼|海洛因)",
            r"manufacture(?:meth|fentanyl|heroin)|howtomake(?:meth|fentanyl|heroin)",
        ),
    ),
    _Rule(
        "S5_CYBER_ABUSE",
        "cyber_abuse",
        (
            r"(?:入侵|破解|盗取|窃取)(?:银行|支付|账户|账号|密码|凭证|系统|服务器|数据库)",
            r"(?:银行|支付|账户|账号|密码|凭证|系统|服务器|数据库)(?:入侵|破解|盗取|窃取)",
            r"勒索软件(?:制作|开发|传播|部署)|(?:制作|开发|传播|部署)勒索软件",
            r"steal(?:passwords|credentials|accounts)|deployransomware|buildransomware",
        ),
    ),
    _Rule(
        "S6_FRAUD_OR_EVASION",
        "fraud_or_evasion",
        (
            r"(?:诈骗|洗钱|逃税|行贿|伪造证件|伪造发票)(?:方法|教程|技巧|话术|流程|指南|方案)",
            r"(?:方法|教程|技巧|话术|流程|指南|方案)(?:诈骗|洗钱|逃税|行贿|伪造证件|伪造发票)",
            r"(?:绕过|规避|躲避)(?:监管|执法|风控|实名认证|身份验证|安全检测)",
            r"laundermoney|evadetaxes|evadelawenforcement|bypass(?:kyc|identityverification|frauddetection)",
        ),
    ),
    _Rule(
        "S7_TARGETED_ABUSE",
        "targeted_abuse",
        (
            r"(?:跟踪|骚扰|人肉搜索)(?:方法|教程|技巧|指南|方案)",
            r"(?:方法|教程|技巧|指南|方案)(?:跟踪|骚扰|人肉搜索)",
            r"doxxingsomeone|howtostalk|howtoharass",
        ),
    ),
)


def _compact(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", value).casefold()
    compact = re.sub(r"[^\w\u3400-\u9fff]+", "", normalized)
    # Established defensive concepts should not be reinterpreted as their
    # contained offence plus an instruction word (for example, 反洗钱方法).
    return compact.replace("反洗钱", "合规治理")


def evaluate_learning_intent(*values: str) -> SafetyDecision:
    """Return the first deterministic safety rule matched by supplied text."""

    text = _compact("\n".join(value for value in values if value))
    if not text:
        return SafetyDecision(allowed=True)
    for rule in _RULES:
        if any(re.search(pattern, text) for pattern in rule.patterns):
            return SafetyDecision(
                allowed=False,
                category=rule.category,
                rule_id=rule.rule_id,
            )
    return SafetyDecision(allowed=True)


def require_safe_plan_request(body) -> None:
    decision = evaluate_learning_intent(
        body.topic,
        body.role,
        body.experience,
        body.purpose,
        body.details,
    )
    if not decision.allowed:
        raise AppError(
            "这个学习目标涉及可能造成伤害或违法违规的操作性内容，暂时无法生成。"
            "你可以改为风险识别、合规治理、历史原理或安全防护方向后重试。",
            code="LEARNING_GOAL_SAFETY_BLOCKED",
            status=422,
            retryable=False,
        )


def require_safe_generated_plan(generated) -> None:
    values = [generated.series_title, generated.rationale]
    for book in generated.books:
        values.extend((book.title, book.topic, book.description))
        for chapter in book.chapters:
            values.extend((chapter.title, chapter.objective))
    decision = evaluate_learning_intent(*values)
    if not decision.allowed:
        raise AppError(
            "AI 生成的学习目录未通过内容安全检查，本次没有创建系列。"
            "请调整学习目标后重试。",
            code="GENERATED_PLAN_SAFETY_BLOCKED",
            status=502,
            retryable=False,
        )


def require_safe_book_replan(*, feedback: str, generated=None) -> None:
    values = [feedback]
    if generated is not None:
        values.append(generated.rationale)
        for chapter in generated.chapters:
            values.extend((chapter.title, chapter.objective))
    decision = evaluate_learning_intent(*values)
    if not decision.allowed:
        raise AppError(
            "这次目录调整涉及可能造成伤害或违法违规的操作性内容，暂时无法生成。"
            "请改为风险识别、合规治理或安全防护方向后重试。",
            code=(
                "GENERATED_BOOK_OUTLINE_SAFETY_BLOCKED"
                if generated is not None
                else "BOOK_REPLAN_SAFETY_BLOCKED"
            ),
            status=502 if generated is not None else 422,
            retryable=False,
        )
