"""Versioned BKT parameters with fail-closed activation boundaries."""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from uuid import uuid4

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ...core.errors import AppError
from ...infrastructure.tables import (
    AssessmentTarget,
    BktParameterActivationEvent,
    BktParameterSetVersion,
)


BKT_PARAMETER_VERSION = "bkt_multimodal_v2"
BKT_REGISTRY_RULE_VERSION = "bkt_parameter_registry_v1"
MIN_SHADOW_OBSERVATIONS = 100


@dataclass(frozen=True)
class BktParameterSet:
    version: str
    prior_known: float
    standard_guess: float
    standard_slip: float
    assisted_guess: float
    oral_partial_guess: float
    oral_partial_slip: float
    oral_weak_guess: float
    oral_weak_slip: float


DEFAULT_BKT_PARAMETERS = BktParameterSet(
    version=BKT_PARAMETER_VERSION,
    prior_known=0.2,
    standard_guess=0.25,
    standard_slip=0.12,
    assisted_guess=0.5,
    oral_partial_guess=0.48,
    oral_partial_slip=0.25,
    oral_weak_guess=0.4,
    oral_weak_slip=0.4,
)


_FIELDS = tuple(key for key in asdict(DEFAULT_BKT_PARAMETERS) if key != "version")


def _dump(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _load(value: str, default):
    try:
        return json.loads(value) if value else default
    except (TypeError, ValueError):
        return default


def _validate(version: str, payload: object) -> BktParameterSet:
    if not isinstance(payload, dict) or set(payload) != set(_FIELDS):
        raise AppError(
            "BKT 参数集合字段不完整",
            code="BKT_PARAMETER_SCHEMA_INVALID",
            status=409,
        )
    values = {key: float(payload[key]) for key in _FIELDS}
    if any(value <= 0 or value >= 1 for value in values.values()):
        raise AppError(
            "BKT 参数必须位于 0 与 1 之间",
            code="BKT_PARAMETER_RANGE_INVALID",
            status=409,
        )
    for guess_key, slip_key in (
        ("standard_guess", "standard_slip"),
        ("oral_partial_guess", "oral_partial_slip"),
        ("oral_weak_guess", "oral_weak_slip"),
    ):
        if values[guess_key] + values[slip_key] >= 1:
            raise AppError(
                "BKT 猜测率与失误率不能形成反向测量",
                code="BKT_PARAMETER_IDENTIFIABILITY_INVALID",
                status=409,
            )
    return BktParameterSet(version=version, **values)


def parameter_payload(parameters: BktParameterSet) -> dict[str, float]:
    values = asdict(parameters)
    values.pop("version")
    return values


def register_parameter_set(
    db: Session,
    *,
    scope_kind: str,
    scope_key: str,
    parameters: dict,
    training_snapshot: dict,
    evaluation: dict,
    provenance_mode: str = "offline_calibration",
) -> BktParameterSetVersion:
    material = _dump({
        "scopeKind": scope_kind,
        "scopeKey": scope_key,
        "parameters": parameters,
        "trainingSnapshot": training_snapshot,
        "evaluation": evaluation,
        "registryRuleVersion": BKT_REGISTRY_RULE_VERSION,
    })
    version = f"bkt_{hashlib.sha256(material.encode()).hexdigest()[:20]}"
    normalized = _validate(version, parameters)
    existing = db.get(BktParameterSetVersion, version)
    if existing:
        return existing
    row = BktParameterSetVersion(
        version=version,
        scope_kind=scope_kind,
        scope_key=scope_key,
        parameters_json=_dump(parameter_payload(normalized)),
        training_snapshot_json=_dump(training_snapshot),
        evaluation_json=_dump(evaluation),
        provenance_mode=provenance_mode,
    )
    db.add(row)
    db.flush()
    return row


def _latest_activation(
    db: Session,
    *,
    scope_kind: str,
    scope_key: str,
    deployment_mode: str,
) -> BktParameterActivationEvent | None:
    return db.scalar(
        select(BktParameterActivationEvent)
        .where(
            BktParameterActivationEvent.scope_kind == scope_kind,
            BktParameterActivationEvent.scope_key == scope_key,
            BktParameterActivationEvent.deployment_mode == deployment_mode,
        )
        .order_by(
            BktParameterActivationEvent.sequence.desc(),
            BktParameterActivationEvent.created_at.desc(),
            BktParameterActivationEvent.id.desc(),
        )
        .limit(1)
    )


def activate_parameter_set(
    db: Session,
    *,
    version: str,
    deployment_mode: str,
    decision: dict,
) -> BktParameterActivationEvent:
    if deployment_mode not in {"shadow", "online"}:
        raise AppError(
            "BKT 参数部署模式无效",
            code="BKT_DEPLOYMENT_MODE_INVALID",
            status=409,
        )
    artifact = db.get(BktParameterSetVersion, version)
    if not artifact:
        raise AppError(
            "BKT 参数版本不存在",
            code="BKT_PARAMETER_SET_NOT_FOUND",
            status=404,
        )
    evaluation = _load(artifact.evaluation_json, {})
    if deployment_mode == "online" and artifact.provenance_mode != "system_default":
        shadow = _latest_activation(
            db,
            scope_kind=artifact.scope_kind,
            scope_key=artifact.scope_key,
            deployment_mode="shadow",
        )
        if not shadow or shadow.parameter_set_version != artifact.version:
            raise AppError(
                "候选参数尚未完成当前版本的影子运行",
                code="BKT_SHADOW_EVIDENCE_REQUIRED",
                status=409,
            )
        if (
            not evaluation.get("gatePassed")
            or not decision.get("approved")
            or not decision.get("shadowPassed")
            or int(decision.get("shadowObservationCount") or 0)
            < MIN_SHADOW_OBSERVATIONS
        ):
            raise AppError(
                "候选参数没有通过离线与上线审批门槛",
                code="BKT_ACTIVATION_GATE_FAILED",
                status=409,
            )
    previous = _latest_activation(
        db,
        scope_kind=artifact.scope_kind,
        scope_key=artifact.scope_key,
        deployment_mode=deployment_mode,
    )
    sequence = int(db.scalar(
        select(func.max(BktParameterActivationEvent.sequence)).where(
            BktParameterActivationEvent.scope_kind == artifact.scope_kind,
            BktParameterActivationEvent.scope_key == artifact.scope_key,
            BktParameterActivationEvent.deployment_mode == deployment_mode,
        )
    ) or 0) + 1
    row = BktParameterActivationEvent(
        id=f"bkt_activation_{uuid4().hex}",
        scope_kind=artifact.scope_kind,
        scope_key=artifact.scope_key,
        deployment_mode=deployment_mode,
        sequence=sequence,
        parameter_set_version=artifact.version,
        previous_parameter_set_version=(
            previous.parameter_set_version if previous else ""
        ),
        decision_json=_dump({
            **decision,
            "registryRuleVersion": BKT_REGISTRY_RULE_VERSION,
        }),
    )
    db.add(row)
    db.flush()
    return row


def resolve_bkt_parameters(
    db: Session,
    *,
    user_id: str,
    target_id: str,
) -> BktParameterSet:
    """Resolve the most specific online set, falling back to the frozen default."""

    target = db.get(AssessmentTarget, target_id)
    scopes = [
        ("assessment_target", target_id),
        *(([("knowledge_node", target.concept_revision_id)] if target and target.concept_revision_id else [])),
        ("learner", user_id),
        ("global", "*"),
    ]
    for scope_kind, scope_key in scopes:
        activation = _latest_activation(
            db,
            scope_kind=scope_kind,
            scope_key=scope_key,
            deployment_mode="online",
        )
        if not activation:
            continue
        artifact = db.get(BktParameterSetVersion, activation.parameter_set_version)
        if artifact:
            return _validate(artifact.version, _load(artifact.parameters_json, {}))
    return DEFAULT_BKT_PARAMETERS
