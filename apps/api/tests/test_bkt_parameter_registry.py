import json

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.core.errors import AppError
from app.evaluation.bkt_calibration import calibrate_bkt_candidate
from app.infrastructure.tables import AssessmentTarget, Base, User
from app.modules.learning.bkt_parameters import (
    DEFAULT_BKT_PARAMETERS,
    activate_parameter_set,
    parameter_payload,
    register_parameter_set,
    resolve_bkt_parameters,
)


def _session() -> Session:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    return Session(engine)


def test_candidate_requires_shadow_and_explicit_gate_before_online_activation():
    with _session() as db:
        db.add(User(id="learner", name="Learner"))
        db.add(AssessmentTarget(
            id="target",
            objective_key="objective",
            objective_statement="使用导数解决标准最优化问题",
            dimension="application",
            target_depth="standard",
            status="active",
        ))
        artifact = register_parameter_set(
            db,
            scope_kind="assessment_target",
            scope_key="target",
            parameters={**parameter_payload(DEFAULT_BKT_PARAMETERS), "prior_known": 0.3},
            training_snapshot={"sampleCount": 400},
            evaluation={"gatePassed": True},
        )
        with pytest.raises(AppError) as missing_shadow:
            activate_parameter_set(
                db,
                version=artifact.version,
                deployment_mode="online",
                decision={"approved": True},
            )
        assert missing_shadow.value.code == "BKT_SHADOW_EVIDENCE_REQUIRED"

        activate_parameter_set(
            db,
            version=artifact.version,
            deployment_mode="shadow",
            decision={"approved": True, "basis": "shadow_started"},
        )
        with pytest.raises(AppError) as missing_approval:
            activate_parameter_set(
                db,
                version=artifact.version,
                deployment_mode="online",
                decision={"approved": False},
            )
        assert missing_approval.value.code == "BKT_ACTIVATION_GATE_FAILED"

        activation = activate_parameter_set(
            db,
            version=artifact.version,
            deployment_mode="online",
            decision={
                "approved": True,
                "shadowPassed": True,
                "shadowObservationCount": 160,
                "basis": "offline_and_shadow_review",
            },
        )
        resolved = resolve_bkt_parameters(db, user_id="learner", target_id="target")
        assert activation.parameter_set_version == artifact.version
        assert resolved.version == artifact.version
        assert resolved.prior_known == 0.3
        assert json.loads(activation.decision_json)["approved"] is True


def test_offline_calibration_fails_closed_when_real_sequences_are_insufficient():
    with _session() as db:
        result = calibrate_bkt_candidate(db)
        assert result["status"] == "insufficient_data"
        assert result["registered"] is False
        assert result["required"]["sequences"] > 0
