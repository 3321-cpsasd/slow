from datetime import datetime, timezone

from sqlalchemy.orm import Session

from app.auth.privacy import PRIVACY_NOTICE_VERSION, TRIAL_TERMS_VERSION
from app.infrastructure.database import build_database
from app.infrastructure.tables import (
    AiInvocation,
    AiUsageMeasurement,
    Base,
    LocalCredential,
    PrivacyConsent,
    User,
)
from app.operations.report import build_operations_snapshot


def test_operations_snapshot_is_pseudonymous_and_formula_ready(tmp_path):
    engine, sessions = build_database(
        f"sqlite+pysqlite:///{tmp_path / 'operations.db'}"
    )
    Base.metadata.create_all(engine)
    created = datetime(2026, 8, 8, tzinfo=timezone.utc)
    with Session(engine) as db:
        db.add(User(id="user_ops", name="运营测试用户", status="active", created_at=created))
        db.commit()
        db.add(
            LocalCredential(
                id="credential_ops",
                user_id="user_ops",
                username="operator-ledger-user",
                password_hash="not-used-in-report",
            )
        )
        db.add(
            PrivacyConsent(
                id="consent_ops",
                user_id="user_ops",
                notice_version=PRIVACY_NOTICE_VERSION,
                trial_terms_version=TRIAL_TERMS_VERSION,
                status="accepted",
                source="in_app",
                accepted_at=created,
            )
        )
        db.add(
            AiInvocation(
                id="invocation_ops",
                provider="openai",
                api_mode="responses",
                model="test-model",
                operation="lesson",
                status="succeeded",
                usage_status="reported",
                attribution_status="verified",
                subject_user_id="user_ops",
                started_at=created,
            )
        )
        db.commit()
        db.add(
            AiUsageMeasurement(
                id="usage_ops",
                invocation_id="invocation_ops",
                source="provider_response",
                quality="reported",
                input_tokens=120,
                output_tokens=30,
                total_tokens=150,
                observed_at=created,
            )
        )
        db.commit()

        protected = build_operations_snapshot(
            db,
            include_identifiers=True,
            generated_at=created,
        )
        pseudonymous = build_operations_snapshot(
            db,
            include_identifiers=False,
            generated_at=created,
        )

    row = protected["users"][0]
    assert row["username"] == "operator-ledger-user"
    assert row["privacyConsentCurrent"] is True
    assert row["inputTokens"] == 120
    assert row["outputTokens"] == 30
    assert row["totalTokens"] == 150
    assert row["firstSectionStarted"] is False
    assert "username" not in pseudonymous["users"][0]
    assert "userId" not in pseudonymous["users"][0]
    assert pseudonymous["users"][0]["accountRef"].startswith("U-")
