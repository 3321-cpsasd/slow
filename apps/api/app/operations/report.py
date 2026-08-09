import hashlib
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..auth.privacy import PRIVACY_NOTICE_VERSION, TRIAL_TERMS_VERSION
from ..infrastructure.tables import (
    AccountExitRequest,
    AiInvocation,
    AiUsageMeasurement,
    BookProgress,
    ChapterProgress,
    KnowledgeStateProjection,
    LearningResumePosition,
    LearningTask,
    LocalCredential,
    PrivacyConsent,
    QuizAttempt,
    ReviewAssignment,
    User,
    UserFeedback,
    UserProfile,
)


REPORT_SCHEMA_VERSION = "operations_snapshot_v1"


def _iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.isoformat()


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def _account_ref(user_id: str) -> str:
    return f"U-{hashlib.sha256(user_id.encode()).hexdigest()[:10].upper()}"


def _count(db: Session, model, *criteria) -> int:
    return int(db.scalar(select(func.count()).select_from(model).where(*criteria)) or 0)


def _seven_day_retained_targets(db: Session, user_id: str) -> int:
    retained: set[str] = set()
    assignments = db.scalars(
        select(ReviewAssignment).where(
            ReviewAssignment.user_id == user_id,
            ReviewAssignment.status == "submitted",
            ReviewAssignment.submitted_attempt_id.is_not(None),
        )
    ).all()
    for assignment in assignments:
        submitted = db.get(QuizAttempt, assignment.submitted_attempt_id)
        if not submitted or not submitted.passed:
            continue
        source_at = db.scalar(
            select(func.min(QuizAttempt.created_at)).where(
                QuizAttempt.user_id == user_id,
                QuizAttempt.quiz_set_id == assignment.prior_quiz_set_id,
                QuizAttempt.passed.is_(True),
            )
        )
        if source_at and _aware(submitted.created_at) - _aware(source_at) >= timedelta(days=7):
            retained.add(assignment.assessment_target_id)
    return len(retained)


def build_operations_snapshot(
    db: Session,
    *,
    include_identifiers: bool = False,
    generated_at: datetime | None = None,
) -> dict:
    generated = generated_at or datetime.now(timezone.utc)
    rows = []
    for user in db.scalars(select(User).order_by(User.created_at, User.id)).all():
        credential = db.scalar(
            select(LocalCredential).where(LocalCredential.user_id == user.id)
        )
        profile = db.get(UserProfile, user.id)
        consent = db.scalar(
            select(PrivacyConsent).where(
                PrivacyConsent.user_id == user.id,
                PrivacyConsent.notice_version == PRIVACY_NOTICE_VERSION,
                PrivacyConsent.trial_terms_version == TRIAL_TERMS_VERSION,
                PrivacyConsent.status == "accepted",
            )
        )
        exit_request = db.scalar(
            select(AccountExitRequest)
            .where(AccountExitRequest.user_id == user.id)
            .order_by(AccountExitRequest.requested_at.desc())
        )
        usage = db.execute(
            select(
                func.coalesce(func.sum(AiUsageMeasurement.input_tokens), 0),
                func.coalesce(func.sum(AiUsageMeasurement.output_tokens), 0),
                func.coalesce(func.sum(AiUsageMeasurement.total_tokens), 0),
            )
            .select_from(AiUsageMeasurement)
            .join(
                AiInvocation,
                AiInvocation.id == AiUsageMeasurement.invocation_id,
            )
            .where(AiInvocation.subject_user_id == user.id)
        ).one()
        first_section_started = bool(
            db.scalar(
                select(LearningResumePosition.id).where(
                    LearningResumePosition.user_id == user.id
                ).limit(1)
            )
            or db.scalar(
                select(QuizAttempt.id).where(QuizAttempt.user_id == user.id).limit(1)
            )
        )
        row = {
            "accountRef": _account_ref(user.id),
            "accountStatus": user.status,
            "createdAt": _iso(user.created_at),
            "lastLoginAt": _iso(credential.last_login_at) if credential else None,
            "privacyConsentCurrent": bool(consent),
            "privacyAcceptedAt": _iso(consent.accepted_at) if consent else None,
            "profileCompleted": bool(profile and profile.completed_at),
            "firstSectionStarted": first_section_started,
            "firstChapterCompleted": bool(
                _count(
                    db,
                    ChapterProgress,
                    ChapterProgress.user_id == user.id,
                    ChapterProgress.status == "completed",
                )
            ),
            "firstBookCompleted": bool(
                _count(
                    db,
                    BookProgress,
                    BookProgress.user_id == user.id,
                    BookProgress.status == "completed",
                )
            ),
            "retainedConcepts7d": _seven_day_retained_targets(db, user.id),
            "retainedClaims": _count(
                db,
                KnowledgeStateProjection,
                KnowledgeStateProjection.user_id == user.id,
                KnowledgeStateProjection.claim_status == "retained",
            ),
            "failedTasks": _count(
                db,
                LearningTask,
                LearningTask.user_id == user.id,
                LearningTask.status == "failed",
            ),
            "feedbackCount": _count(
                db,
                UserFeedback,
                UserFeedback.user_id == user.id,
            ),
            "aiInvocations": _count(
                db,
                AiInvocation,
                AiInvocation.subject_user_id == user.id,
            ),
            "failedAiInvocations": _count(
                db,
                AiInvocation,
                AiInvocation.subject_user_id == user.id,
                AiInvocation.status == "failed",
            ),
            "inputTokens": int(usage[0] or 0),
            "outputTokens": int(usage[1] or 0),
            "totalTokens": int(usage[2] or 0),
            "exitStatus": exit_request.status if exit_request else "",
            "exitRequestedAt": _iso(exit_request.requested_at) if exit_request else None,
            "deletionDueAt": _iso(exit_request.deletion_due_at) if exit_request else None,
        }
        if include_identifiers:
            row = {
                **row,
                "userId": user.id,
                "username": credential.username if credential else "",
            }
        rows.append(row)
    return {
        "schemaVersion": REPORT_SCHEMA_VERSION,
        "generatedAt": _iso(generated),
        "privacyNoticeVersion": PRIVACY_NOTICE_VERSION,
        "trialTermsVersion": TRIAL_TERMS_VERSION,
        "identifiersIncluded": include_identifiers,
        "definitions": {
            "firstSectionStarted": "存在服务端阅读位置或测验作答事实",
            "firstChapterCompleted": "至少一个 ChapterProgress.status=completed",
            "firstBookCompleted": "至少一个 BookProgress.status=completed",
            "retainedConcepts7d": "合格复习作答通过，且距同题集首次通过不少于 7×24 小时的目标数",
            "retainedClaims": "当前可重建掌握投影中 claim_status=retained 的目标数",
            "failedTasks": "当前状态为 failed 的持久化学习任务数",
            "modelCost": "由工作簿中的每百万输入/输出 Token 单价公式计算；快照不猜测供应商价格",
        },
        "users": rows,
    }
