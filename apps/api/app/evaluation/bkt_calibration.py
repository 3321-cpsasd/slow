"""Offline, time-split calibration for versioned BKT candidates.

The job learns only from already-qualified observations. It registers an
immutable candidate and never activates it; shadow and online deployment remain
separate audited decisions.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timezone
from itertools import product
from math import log

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..infrastructure.tables import (
    AssessmentObservation,
    EvidenceQualificationEvent,
)
from ..modules.learning.bkt_parameters import (
    DEFAULT_BKT_PARAMETERS,
    parameter_payload,
    register_parameter_set,
)


CALIBRATION_RULE_VERSION = "bkt_offline_time_split_v1"
MIN_TRAINING_OBSERVATIONS = 200
MIN_VALIDATION_OBSERVATIONS = 50
MIN_SEQUENCES = 30


def _utc(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def _posterior(prior: float, correct: bool, guess: float, slip: float) -> float:
    if correct:
        denominator = prior * (1 - slip) + (1 - prior) * guess
        return prior * (1 - slip) / denominator if denominator else prior
    denominator = prior * slip + (1 - prior) * (1 - guess)
    return prior * slip / denominator if denominator else prior


def _metrics(sequences: list[list[tuple[bool, bool]]], parameters: dict) -> dict:
    losses: list[float] = []
    briers: list[float] = []
    for sequence in sequences:
        known = float(parameters["prior_known"])
        for correct, assisted in sequence:
            guess = (
                parameters["assisted_guess"]
                if assisted
                else parameters["standard_guess"]
            )
            slip = parameters["standard_slip"]
            probability = known * (1 - slip) + (1 - known) * guess
            probability = max(1e-6, min(1 - 1e-6, probability))
            outcome = 1.0 if correct else 0.0
            losses.append(-(outcome * log(probability) + (1 - outcome) * log(1 - probability)))
            briers.append((probability - outcome) ** 2)
            known = _posterior(known, correct, guess, slip)
    return {
        "observationCount": len(losses),
        "logLoss": round(sum(losses) / len(losses), 8) if losses else None,
        "brierScore": round(sum(briers) / len(briers), 8) if briers else None,
    }


def calibrate_bkt_candidate(
    db: Session,
    *,
    scope_kind: str = "global",
    scope_key: str = "*",
) -> dict:
    qualified_ids = set(db.scalars(
        select(EvidenceQualificationEvent.observation_id).where(
            EvidenceQualificationEvent.projection_family == "mastery",
            EvidenceQualificationEvent.rule_version == "evidence_v3",
            EvidenceQualificationEvent.status.in_({"eligible", "eligible_grouped"}),
        )
    ).all())
    observations = db.scalars(
        select(AssessmentObservation)
        .where(AssessmentObservation.id.in_(qualified_ids))
        .order_by(AssessmentObservation.created_at, AssessmentObservation.sequence)
    ).all() if qualified_ids else []
    grouped: dict[tuple[str, str], list[AssessmentObservation]] = defaultdict(list)
    for item in observations:
        grouped[(item.user_id, item.assessment_target_id)].append(item)
    sequences = [items for items in grouped.values() if len(items) >= 2]
    if len(observations) < MIN_TRAINING_OBSERVATIONS + MIN_VALIDATION_OBSERVATIONS or len(sequences) < MIN_SEQUENCES:
        return {
            "status": "insufficient_data",
            "registered": False,
            "observationCount": len(observations),
            "sequenceCount": len(sequences),
            "required": {
                "observations": MIN_TRAINING_OBSERVATIONS + MIN_VALIDATION_OBSERVATIONS,
                "sequences": MIN_SEQUENCES,
            },
            "ruleVersion": CALIBRATION_RULE_VERSION,
        }

    flattened = sorted(
        observations,
        key=lambda item: (_utc(item.created_at), item.sequence, item.id),
    )
    split_at = _utc(flattened[round(len(flattened) * 0.8)].created_at)
    train: list[list[tuple[bool, bool]]] = []
    validation: list[list[tuple[bool, bool]]] = []
    for items in sequences:
        train_items = [
            (item.correct, item.assistance_mode == "assisted_immediate")
            for item in items if _utc(item.created_at) < split_at
        ]
        validation_items = [
            (item.correct, item.assistance_mode == "assisted_immediate")
            for item in items if _utc(item.created_at) >= split_at
        ]
        if train_items:
            train.append(train_items)
        if validation_items:
            validation.append(validation_items)
    train_count = sum(len(items) for items in train)
    validation_count = sum(len(items) for items in validation)
    if train_count < MIN_TRAINING_OBSERVATIONS or validation_count < MIN_VALIDATION_OBSERVATIONS:
        return {
            "status": "insufficient_time_split",
            "registered": False,
            "trainingObservationCount": train_count,
            "validationObservationCount": validation_count,
            "ruleVersion": CALIBRATION_RULE_VERSION,
        }

    baseline = parameter_payload(DEFAULT_BKT_PARAMETERS)
    best = baseline
    best_metrics = _metrics(train, baseline)
    for prior, guess, slip in product(
        (0.1, 0.2, 0.3, 0.4),
        (0.1, 0.2, 0.3, 0.4),
        (0.05, 0.1, 0.15, 0.2),
    ):
        candidate = {
            **baseline,
            "prior_known": prior,
            "standard_guess": guess,
            "standard_slip": slip,
        }
        metrics = _metrics(train, candidate)
        if metrics["logLoss"] < best_metrics["logLoss"]:
            best = candidate
            best_metrics = metrics

    baseline_validation = _metrics(validation, baseline)
    candidate_validation = _metrics(validation, best)
    improvement = baseline_validation["logLoss"] - candidate_validation["logLoss"]
    gate_passed = improvement >= 0.005 and candidate_validation["brierScore"] <= baseline_validation["brierScore"]
    evaluation = {
        "ruleVersion": CALIBRATION_RULE_VERSION,
        "gatePassed": gate_passed,
        "baselineValidation": baseline_validation,
        "candidateValidation": candidate_validation,
        "logLossImprovement": round(improvement, 8),
    }
    artifact = register_parameter_set(
        db,
        scope_kind=scope_kind,
        scope_key=scope_key,
        parameters=best,
        training_snapshot={
            "ruleVersion": CALIBRATION_RULE_VERSION,
            "splitAt": split_at.isoformat(),
            "trainingObservationCount": train_count,
            "validationObservationCount": validation_count,
            "sequenceCount": len(sequences),
            "qualificationRuleVersion": "evidence_v3",
        },
        evaluation=evaluation,
    )
    return {
        "status": "candidate_registered",
        "registered": True,
        "parameterSetVersion": artifact.version,
        "gatePassed": gate_passed,
        "evaluation": evaluation,
        "ruleVersion": CALIBRATION_RULE_VERSION,
    }
