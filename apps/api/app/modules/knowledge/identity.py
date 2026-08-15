"""Conservative, append-only resolution for on-demand knowledge identities."""

from __future__ import annotations

import hashlib
import json

from sqlalchemy import select
from sqlalchemy.orm import Session

from ...core.errors import AppError
from ...infrastructure.tables import (
    AssessmentTarget,
    Concept,
    ConceptRevision,
    KnowledgeIdentityCandidate,
    KnowledgeIdentityDecision,
    LearningObjective,
)


CANDIDATE_NAMESPACE = "on_demand_knowledge"
CANDIDATE_IDENTITY_STATUS = "route_scoped_knowledge"
CANDIDATE_REVISION_STATUS = "route_scoped"
IDENTITY_RULE_VERSION = "knowledge_identity_exact_v1"
CAPABILITY_DIMENSIONS = {
    "recognition",
    "mechanism",
    "application",
    "boundary",
    "transfer",
}


def _dump(value) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def _load(value: str, default):
    try:
        return json.loads(value) if value else default
    except (TypeError, ValueError):
        return default


def _normalized(value: object) -> str:
    return " ".join(str(value or "").strip().lower().split())


def _stable_id(prefix: str, *parts: object) -> str:
    material = "\x1f".join(str(part) for part in parts)
    return f"{prefix}_{hashlib.sha256(material.encode()).hexdigest()[:32]}"


def _candidate_value(candidate: dict, snake: str, camel: str, default=""):
    if snake in candidate:
        return candidate[snake]
    return candidate.get(camel, default)


def normalize_candidate(candidate: dict) -> dict:
    if not isinstance(candidate, dict):
        raise AppError(
            "知识候选结构无效，请重新规划本章",
            code="KNOWLEDGE_IDENTITY_CANDIDATE_INVALID",
            status=409,
        )
    boundaries = _candidate_value(candidate, "boundaries", "boundaries", [])
    if not isinstance(boundaries, list):
        boundaries = []
    normalized = {
        "candidateKey": _normalized(
            _candidate_value(candidate, "candidate_key", "candidateKey")
        ),
        "label": " ".join(str(candidate.get("label") or "").strip().split()),
        "definition": " ".join(
            str(candidate.get("definition") or "").strip().split()
        ),
        "scope": " ".join(str(candidate.get("scope") or "").strip().split()),
        "boundaries": sorted(
            {
                " ".join(str(item).strip().split())
                for item in boundaries
                if str(item).strip()
            }
        ),
        "reuseConceptRevisionId": str(
            _candidate_value(
                candidate,
                "reuse_concept_revision_id",
                "reuseConceptRevisionId",
            )
            or ""
        ).strip(),
    }
    if not all(
        normalized[key] for key in ("candidateKey", "label", "definition", "scope")
    ):
        raise AppError(
            "知识候选缺少名称、定义或范围，请重新规划本章",
            code="KNOWLEDGE_IDENTITY_CANDIDATE_INCOMPLETE",
            status=409,
        )
    return normalized


def candidate_semantic_hash(candidate: dict) -> str:
    normalized = normalize_candidate(candidate)
    semantic = {
        key: normalized[key]
        for key in ("candidateKey", "label", "definition", "scope", "boundaries")
    }
    return hashlib.sha256(_dump(semantic).encode()).hexdigest()


def _rank_policy(label: str, scope: str) -> dict:
    return {
        "version": "knowledge_rank_policy_v1",
        "capabilityScope": f"{label}：{scope}",
        "rankCeiling": "master",
        "dimensionRanks": {
            "recognition": "bronze",
            "mechanism": "silver",
            "application": "gold",
            "boundary": "platinum",
            "transfer": "diamond",
        },
    }


def _same_series_revision_allowed(
    db: Session, *, series_id: str, revision_id: str
) -> bool:
    revision = db.get(ConceptRevision, revision_id)
    if revision is None:
        return False
    if revision.verification_status == "reviewed":
        return True
    return (
        db.scalar(
            select(KnowledgeIdentityDecision.id)
            .join(
                KnowledgeIdentityCandidate,
                KnowledgeIdentityCandidate.id
                == KnowledgeIdentityDecision.candidate_id,
            )
            .where(
                KnowledgeIdentityCandidate.series_id == series_id,
                KnowledgeIdentityDecision.resolved_concept_revision_id == revision_id,
            )
            .limit(1)
        )
        is not None
    )


def _family_revision_ids(
    db: Session,
    *,
    series_id: str,
    candidate_key: str,
    label: str,
    exclude_hash: str,
) -> list[str]:
    rows = db.execute(
        select(KnowledgeIdentityCandidate, KnowledgeIdentityDecision)
        .join(
            KnowledgeIdentityDecision,
            KnowledgeIdentityDecision.candidate_id == KnowledgeIdentityCandidate.id,
        )
        .where(
            KnowledgeIdentityCandidate.series_id == series_id,
            KnowledgeIdentityCandidate.candidate_hash != exclude_hash,
        )
        .order_by(KnowledgeIdentityDecision.created_at.desc())
    ).all()
    result: list[str] = []
    for other, decision in rows:
        same_family = (
            _normalized(other.candidate_key) == candidate_key
            or _normalized(other.label) == _normalized(label)
        )
        revision_id = decision.resolved_concept_revision_id
        if same_family and revision_id and revision_id not in result:
            result.append(revision_id)
    return result[:20]


def _ensure_candidate_occurrence(
    db: Session,
    *,
    series_id: str,
    section_id: str,
    candidate: dict,
    semantic_hash: str,
) -> KnowledgeIdentityCandidate:
    existing = db.scalar(
        select(KnowledgeIdentityCandidate).where(
            KnowledgeIdentityCandidate.section_id == section_id,
            KnowledgeIdentityCandidate.candidate_hash == semantic_hash,
        )
    )
    if existing:
        return existing
    row = KnowledgeIdentityCandidate(
        id=_stable_id("knowledge_candidate", section_id, semantic_hash),
        series_id=series_id,
        section_id=section_id,
        candidate_key=candidate["candidateKey"],
        label=candidate["label"],
        definition=candidate["definition"],
        scope_json=_dump({"description": candidate["scope"]}),
        boundaries_json=_dump(candidate["boundaries"]),
        candidate_hash=semantic_hash,
        status="proposed",
        provenance_mode="chapter_outline",
    )
    db.add(row)
    db.flush()
    return row


def _record_decision(
    db: Session,
    *,
    candidate: KnowledgeIdentityCandidate,
    decision: str,
    revision_id: str,
    compared_revision_ids: list[str],
    basis: dict,
) -> KnowledgeIdentityDecision:
    decision_hash = hashlib.sha256(
        _dump(
            {
                "candidateId": candidate.id,
                "decision": decision,
                "resolvedConceptRevisionId": revision_id,
                "comparedRevisionIds": sorted(compared_revision_ids),
                "ruleVersion": IDENTITY_RULE_VERSION,
            }
        ).encode()
    ).hexdigest()
    existing = db.scalar(
        select(KnowledgeIdentityDecision).where(
            KnowledgeIdentityDecision.decision_hash == decision_hash
        )
    )
    if existing:
        return existing
    row = KnowledgeIdentityDecision(
        id=_stable_id("knowledge_decision", decision_hash),
        candidate_id=candidate.id,
        decision=decision,
        resolved_concept_revision_id=revision_id,
        compared_revision_ids_json=_dump(sorted(compared_revision_ids)),
        basis_json=_dump(basis),
        actor_kind="deterministic_rule",
        actor_id="",
        rule_version=IDENTITY_RULE_VERSION,
        model_version="",
        supersedes_id=None,
        decision_hash=decision_hash,
    )
    db.add(row)
    candidate.status = "unresolved" if decision == "unresolved" else "resolved"
    db.flush()
    return row


def materialize_candidate_target(
    db: Session,
    *,
    series_id: str,
    section_id: str,
    statement: str,
    dimension: str,
    candidate: dict,
) -> AssessmentTarget:
    """Resolve one candidate conservatively and materialize a separate target."""

    if dimension not in CAPABILITY_DIMENSIONS:
        raise AppError(
            "能力目标维度无效，请重新规划本章",
            code="KNOWLEDGE_CAPABILITY_DIMENSION_INVALID",
            status=409,
        )
    normalized = normalize_candidate(candidate)
    semantic_hash = candidate_semantic_hash(candidate)
    occurrence = _ensure_candidate_occurrence(
        db,
        series_id=series_id,
        section_id=section_id,
        candidate=normalized,
        semantic_hash=semantic_hash,
    )

    explicit_revision_id = normalized["reuseConceptRevisionId"]
    deterministic_revision_id = _stable_id(
        "concept_revision_candidate", series_id, semantic_hash, 1
    )
    occurrence_decision = db.scalar(
        select(KnowledgeIdentityDecision)
        .where(KnowledgeIdentityDecision.candidate_id == occurrence.id)
        .order_by(
            KnowledgeIdentityDecision.created_at.desc(),
            KnowledgeIdentityDecision.id.desc(),
        )
    )
    if occurrence_decision and occurrence_decision.resolved_concept_revision_id:
        revision_id = occurrence_decision.resolved_concept_revision_id
        if explicit_revision_id and explicit_revision_id != revision_id:
            raise AppError(
                "同一知识候选引用了冲突的既有身份",
                code="KNOWLEDGE_IDENTITY_CANDIDATE_DECISION_CONFLICT",
                status=409,
            )
        family_revision_ids = _load(
            occurrence_decision.compared_revision_ids_json, []
        )
        decision = occurrence_decision.decision
        basis = _load(occurrence_decision.basis_json, {})
    elif explicit_revision_id:
        family_revision_ids = _family_revision_ids(
            db,
            series_id=series_id,
            candidate_key=normalized["candidateKey"],
            label=normalized["label"],
            exclude_hash=semantic_hash,
        )
        if not _same_series_revision_allowed(
            db,
            series_id=series_id,
            revision_id=explicit_revision_id,
        ):
            raise AppError(
                "知识候选引用了未授权的既有身份，请重新规划本章",
                code="KNOWLEDGE_IDENTITY_REUSE_NOT_ALLOWED",
                status=409,
            )
        revision_id = explicit_revision_id
        decision = "reuse_revision"
        basis = {"mode": "allowlisted_revision_reference"}
    else:
        existing_revision = db.get(ConceptRevision, deterministic_revision_id)
        family_revision_ids = _family_revision_ids(
            db,
            series_id=series_id,
            candidate_key=normalized["candidateKey"],
            label=normalized["label"],
            exclude_hash=semantic_hash,
        )
        revision_id = deterministic_revision_id
        if existing_revision is not None:
            decision = "reuse_revision"
            basis = {"mode": "exact_semantic_hash"}
        else:
            concept_id = _stable_id("concept_candidate", series_id, semantic_hash)
            db.add(
                Concept(
                    id=concept_id,
                    namespace=f"{CANDIDATE_NAMESPACE}:{series_id}",
                    concept_key=semantic_hash,
                    canonical_name=normalized["label"],
                    status="active",
                    origin="on_demand_candidate",
                )
            )
            db.add(
                ConceptRevision(
                    id=revision_id,
                    concept_id=concept_id,
                    revision=1,
                    label=normalized["label"],
                    definition=normalized["definition"],
                    scope_json=_dump(
                        {
                            "candidateKey": normalized["candidateKey"],
                            "description": normalized["scope"],
                            "semanticHash": semantic_hash,
                            "rankPolicy": _rank_policy(
                                normalized["label"], normalized["scope"]
                            ),
                        }
                    ),
                    boundaries_json=_dump(normalized["boundaries"]),
                    provenance_mode="on_demand_candidate",
                    verification_status=CANDIDATE_REVISION_STATUS,
                )
            )
            decision = "unresolved" if family_revision_ids else "create_concept"
            basis = {
                "mode": (
                    "same_name_or_key_semantic_conflict"
                    if family_revision_ids
                    else "new_semantic_identity"
                )
            }
            db.flush()

    revision = db.get(ConceptRevision, revision_id)
    if revision is None:
        raise AppError(
            "知识候选未能解析为有效版本",
            code="KNOWLEDGE_IDENTITY_RESOLUTION_FAILED",
            status=500,
        )
    if occurrence_decision is None:
        _record_decision(
            db,
            candidate=occurrence,
            decision=decision,
            revision_id=revision_id,
            compared_revision_ids=family_revision_ids,
            basis=basis,
        )

    statement_hash = hashlib.sha256(_normalized(statement).encode()).hexdigest()
    objective_id = _stable_id(
        "learning_objective_candidate", revision_id, statement_hash
    )
    if db.get(LearningObjective, objective_id) is None:
        db.add(
            LearningObjective(
                id=objective_id,
                namespace=f"{CANDIDATE_NAMESPACE}:{series_id}",
                objective_key=f"{revision_id}:{statement_hash}",
                statement=statement,
                cognitive_verb={
                    "recognition": "recognize",
                    "mechanism": "explain",
                    "application": "apply",
                    "boundary": "distinguish",
                    "transfer": "transfer",
                }[dimension],
                outcome_type="knowledge",
                provenance_mode="on_demand_candidate",
                verification_status="provisional",
                status="active",
            )
        )
    objective_key = f"candidate:{revision_id}:{statement_hash}"
    target_id = _stable_id(
        "target_candidate", revision_id, statement_hash, dimension, "standard"
    )
    target = db.get(AssessmentTarget, target_id)
    from ..learning.capabilities import ensure_route_capability

    capability_revision, bronze_criterion = ensure_route_capability(
        db,
        series_id=series_id,
        concept_revision_id=revision_id,
    )
    if target is None:
        target = AssessmentTarget(
            id=target_id,
            concept_revision_id=revision_id,
            learning_objective_id=objective_id,
            capability_revision_id=capability_revision.id,
            capability_stage_criterion_id=bronze_criterion.id,
            objective_key=objective_key,
            objective_statement=statement,
            dimension=dimension,
            target_depth="standard",
            identity_status=CANDIDATE_IDENTITY_STATUS,
            status="active",
        )
        db.add(target)
    elif (
        target.capability_revision_id is None
        and target.capability_stage_criterion_id is None
    ):
        target.capability_revision_id = capability_revision.id
        target.capability_stage_criterion_id = bronze_criterion.id
    db.flush()
    return target


def candidate_allowlist_for_series(db: Session, series_id: str) -> list[dict]:
    """Expose only server-resolved revisions from this series to later chapters."""

    rows = db.execute(
        select(
            KnowledgeIdentityCandidate,
            KnowledgeIdentityDecision,
            ConceptRevision,
        )
        .join(
            KnowledgeIdentityDecision,
            KnowledgeIdentityDecision.candidate_id == KnowledgeIdentityCandidate.id,
        )
        .join(
            ConceptRevision,
            ConceptRevision.id
            == KnowledgeIdentityDecision.resolved_concept_revision_id,
        )
        .where(KnowledgeIdentityCandidate.series_id == series_id)
        .order_by(KnowledgeIdentityDecision.created_at.desc())
    ).all()
    result: list[dict] = []
    seen: set[str] = set()
    for candidate, decision, revision in rows:
        if revision.id in seen:
            continue
        seen.add(revision.id)
        result.append(
            {
                "conceptRevisionId": revision.id,
                "candidateKey": candidate.candidate_key,
                "label": revision.label,
                "definition": revision.definition,
                "scope": _load(revision.scope_json, {}).get("description", ""),
                "boundaries": _load(revision.boundaries_json, []),
                "identityStatus": candidate.status,
                "decision": decision.decision,
            }
        )
    return result[:80]
