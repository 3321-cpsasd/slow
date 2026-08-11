from datetime import timedelta
from uuid import uuid4

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from ..core.errors import AppError
from ..infrastructure.tables import (
    AccountExitRequest,
    AccountRecoveryCode,
    AuthSession,
    LocalCredential,
    PrivacyConsent,
    User,
    now,
)


PRIVACY_NOTICE_VERSION = "2026-08-08-r2"
TRIAL_TERMS_VERSION = "2026-08-08"
ACCOUNT_EXIT_POLICY_VERSION = "2026-08-08"
ACTIVE_EXIT_STATUSES = {"requested", "processing"}


def privacy_notice() -> dict:
    return {
        "noticeVersion": PRIVACY_NOTICE_VERSION,
        "trialTermsVersion": TRIAL_TERMS_VERSION,
        "title": "Slow 内测试点隐私告知与参与说明",
        "summary": "Slow 只为提供个人学习、验证效果和改进内测服务处理必要数据。",
        "items": [
            {
                "title": "会处理什么",
                "body": "账号与安全会话、学习画像与目标、书架和教材、阅读与测验记录、答疑、笔记、反馈、页面与关键功能访问、每满 60 秒的有效阅读事件、前端异常类型，以及 AI 调用的用量和错误元数据。",
            },
            {
                "title": "为什么处理",
                "body": "用于生成和交付个性化教材、服务端评分与解锁、跨设备恢复、学习效果验证、故障排查和内测试点分析。",
            },
            {
                "title": "AI 如何参与",
                "body": "生成教材或答疑时，完成当前任务所需的学习目标、相关画像和上下文会发送给已配置的模型服务商；不会把服务端 API Key 发送给浏览器。",
            },
            {
                "title": "不会做什么",
                "body": "不会公开你的笔记、完整问答、错题或掌握画像，不会出售个人数据，不做鼠标轨迹、会话回放或设备指纹，不把答案、笔记、答疑和反馈正文写入产品埋点，也不会把埋点或 Demo 数据伪装成学习完成与掌握证据。",
            },
            {
                "title": "保存与退出",
                "body": "数据在账号有效和试点分析所需期间保存。提交退出与删除申请后立即撤销会话并停止新写入；运营者应在 7 日内完成活动数据库中的删除或去标识化。备份副本进入受限轮转，当前试点以 14 日为清除目标；退出账号不会被恢复为可用状态。",
            },
            {
                "title": "自愿参与",
                "body": "这是邀请制产品验证，不参加不会产生任何不利影响。你可以随时退出；有疑问请通过页面反馈或联系邀请你参加内测的运营者。",
            },
        ],
    }


class PrivacyService:
    def __init__(self, db: Session, user_id: str):
        self.db = db
        self.user_id = user_id

    def current_consent(self) -> PrivacyConsent | None:
        return self.db.scalar(
            select(PrivacyConsent).where(
                PrivacyConsent.user_id == self.user_id,
                PrivacyConsent.notice_version == PRIVACY_NOTICE_VERSION,
                PrivacyConsent.trial_terms_version == TRIAL_TERMS_VERSION,
                PrivacyConsent.status == "accepted",
            )
        )

    def state(self, *, required: bool) -> dict:
        consent = self.current_consent()
        return {
            **privacy_notice(),
            "required": bool(required and not consent),
            "status": "accepted" if consent else "required" if required else "not_required",
            "acceptedAt": consent.accepted_at.isoformat() if consent else None,
        }

    def require_current(self, *, required: bool) -> None:
        if required and not self.current_consent():
            raise AppError(
                "请先阅读并同意当前内测隐私告知",
                code="PRIVACY_CONSENT_REQUIRED",
                status=428,
            )

    def accept(self, *, privacy_accepted: bool, trial_accepted: bool) -> dict:
        if not privacy_accepted or not trial_accepted:
            raise AppError(
                "需要分别确认隐私告知和自愿参加内测试点",
                code="PRIVACY_CONSENT_INCOMPLETE",
                status=400,
            )
        consent = self.current_consent()
        if not consent:
            consent = PrivacyConsent(
                id=f"privacy_consent_{uuid4().hex}",
                user_id=self.user_id,
                notice_version=PRIVACY_NOTICE_VERSION,
                trial_terms_version=TRIAL_TERMS_VERSION,
                status="accepted",
                source="in_app",
            )
            self.db.add(consent)
            self.db.commit()
        return self.state(required=True)

    def request_exit(self, *, confirmation: str, reason: str = "") -> dict:
        if confirmation.strip() != "退出并删除":
            raise AppError(
                "请输入“退出并删除”确认申请",
                code="ACCOUNT_EXIT_CONFIRMATION_INVALID",
                status=400,
            )
        existing = self.db.scalar(
            select(AccountExitRequest).where(
                AccountExitRequest.user_id == self.user_id,
                AccountExitRequest.status.in_(ACTIVE_EXIT_STATUSES),
            )
        )
        if existing:
            return self._exit_view(existing)

        current = now()
        request = AccountExitRequest(
            id=f"account_exit_{uuid4().hex}",
            user_id=self.user_id,
            status="requested",
            policy_version=ACCOUNT_EXIT_POLICY_VERSION,
            reason=reason.strip()[:500],
            requested_at=current,
            deletion_due_at=current + timedelta(days=7),
        )
        user = self.db.get(User, self.user_id)
        if not user:
            raise AppError("当前用户不存在", code="AUTH_USER_MISSING", status=401)
        user.status = "exit_requested"
        user.updated_at = current
        self.db.execute(
            update(LocalCredential)
            .where(LocalCredential.user_id == self.user_id)
            .values(status="disabled", updated_at=current)
        )
        self.db.execute(
            update(AuthSession)
            .where(
                AuthSession.user_id == self.user_id,
                AuthSession.status == "active",
            )
            .values(status="revoked", revoked_at=current)
        )
        self.db.execute(
            update(AccountRecoveryCode)
            .where(
                AccountRecoveryCode.user_id == self.user_id,
                AccountRecoveryCode.status == "active",
            )
            .values(status="revoked", revoked_at=current)
        )
        self.db.add(request)
        self.db.commit()
        return self._exit_view(request)

    @staticmethod
    def _exit_view(request: AccountExitRequest) -> dict:
        return {
            "requestId": request.id,
            "status": request.status,
            "requestedAt": request.requested_at.isoformat(),
            "deletionDueAt": request.deletion_due_at.isoformat(),
            "policyVersion": request.policy_version,
        }
