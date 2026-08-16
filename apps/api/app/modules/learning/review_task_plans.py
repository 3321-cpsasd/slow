"""Deterministic task planning for forgetting review and conscious strengthening."""

from dataclasses import dataclass


REVIEW_TASK_PLAN_RULE_VERSION = "review_task_plan_v1"
STAGE_ORDER = {"unranked": 0, "bronze": 1, "silver": 2, "gold": 3, "diamond": 4}


@dataclass(frozen=True)
class ReviewCriterion:
    criterion_id: str
    stage: str
    position: int
    task_type: str
    verification_protocol: str


def _task_kind(criteria: tuple[ReviewCriterion, ...], *, purpose: str) -> str:
    task_types = {item.task_type for item in criteria}
    if task_types == {"choice_quiz"}:
        base = "choice"
    elif task_types.issubset({"oral_explanation", "oral_boundary"}):
        base = "oral"
    elif task_types == {"standard_application"}:
        base = "application"
    elif task_types == {"transfer_task"}:
        base = "transfer"
    else:
        raise ValueError("review criteria do not form one executable task family")
    return f"{base}_{purpose}"


def plan_review_tasks(
    *,
    current_stage: str,
    criteria: tuple[ReviewCriterion, ...],
    missing_criterion_ids: tuple[str, ...],
    available_criterion_ids: frozenset[str],
    remediation_due: bool,
) -> dict:
    """Freeze one reactivation task and an optional next-stage strengthening task."""

    if current_stage not in STAGE_ORDER:
        raise ValueError("unsupported capability stage")
    ordered = tuple(
        sorted(criteria, key=lambda item: (STAGE_ORDER[item.stage], item.position))
    )
    by_id = {item.criterion_id: item for item in ordered}
    if len(by_id) != len(ordered):
        raise ValueError("review criteria ids must be unique")
    if not set(missing_criterion_ids).issubset(by_id):
        raise ValueError("missing criteria must belong to the capability rubric")

    achieved_stage = "bronze" if current_stage == "unranked" else current_stage
    reactivation = tuple(
        item
        for item in ordered
        if item.stage == achieved_stage
        and item.criterion_id in available_criterion_ids
    )
    if remediation_due:
        bronze = tuple(
            item
            for item in ordered
            if item.stage == "bronze"
            and item.criterion_id in available_criterion_ids
        )
        if bronze:
            reactivation = bronze
    if not reactivation:
        raise ValueError("current stage has no formally available review task")

    missing = [by_id[item] for item in missing_criterion_ids]
    strengthening = None
    if missing:
        next_stage_order = min(STAGE_ORDER[item.stage] for item in missing)
        next_stage = tuple(
            item
            for item in missing
            if STAGE_ORDER[item.stage] == next_stage_order
            and item.criterion_id in available_criterion_ids
        )
        if next_stage:
            strengthening = {
                "purpose": "stage_strengthening",
                "taskKind": _task_kind(next_stage, purpose="strengthening"),
                "stage": next_stage[0].stage,
                "criterionIds": [item.criterion_id for item in next_stage],
                "verificationProtocols": [
                    item.verification_protocol for item in next_stage
                ],
                "evidenceEffect": "may_advance_stage_after_qualified_evidence",
            }

    return {
        "ruleVersion": REVIEW_TASK_PLAN_RULE_VERSION,
        "reactivation": {
            "purpose": "retention_reactivation",
            "taskKind": _task_kind(reactivation, purpose="reactivation"),
            "stage": reactivation[0].stage,
            "criterionIds": [item.criterion_id for item in reactivation],
            "verificationProtocols": [
                item.verification_protocol for item in reactivation
            ],
            "evidenceEffect": "activation_only",
        },
        "strengthening": strengthening,
    }
