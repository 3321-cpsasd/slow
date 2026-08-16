"""Pure rules for selecting and advancing delayed-review assignments.

Persistence owns assignment/event rows.  This module only decides which due
targets fit today's budget, whether a lifecycle transition is valid, and
whether a submitted review may qualify as retention evidence.
"""

from dataclasses import dataclass, replace
from datetime import datetime, timezone
from typing import Literal


REVIEW_ASSIGNMENT_RULE_VERSION = "review_assignment_v2_capability_priority"
RETENTION_QUALIFICATION_RULE_VERSION = "retention_assignment_v1"
MAX_OVERDUE_PRIORITY_BOOST_DAYS = 30

AssignmentStatus = Literal[
    "scheduled",
    "presented",
    "started",
    "submitted",
    "skipped",
    "expired",
]
AssignmentEventType = Literal[
    "presented",
    "started",
    "submitted",
    "skipped",
    "expired",
]


class ReviewAssignmentRuleError(ValueError):
    """A deterministic lifecycle or selection invariant was violated."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def _utc(value: datetime) -> datetime:
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


@dataclass(frozen=True)
class ReviewCandidate:
    review_state_id: str
    assessment_target_id: str
    due_at: datetime
    priority: int
    status: str = "scheduled"
    need_kind: Literal["activation_due", "remediation"] = "activation_due"
    capability_stage: str = "unranked"
    capability_activation_state: str = ""
    capability_revision_id: str = ""


@dataclass(frozen=True)
class SelectedReview:
    review_state_id: str
    assessment_target_id: str
    due_at: datetime
    base_priority: int
    effective_priority: int
    rank: int
    need_kind: Literal["activation_due", "remediation"] = "activation_due"
    capability_stage: str = "unranked"
    capability_revision_id: str = ""
    rule_version: str = REVIEW_ASSIGNMENT_RULE_VERSION


@dataclass(frozen=True)
class DailyReviewSelection:
    as_of: datetime
    daily_budget: int
    due_count: int
    items: tuple[SelectedReview, ...]
    rule_version: str = REVIEW_ASSIGNMENT_RULE_VERSION


def select_daily_reviews(
    candidates: list[ReviewCandidate] | tuple[ReviewCandidate, ...],
    *,
    daily_budget: int,
    as_of: datetime,
) -> DailyReviewSelection:
    """Return a stable, budget-limited selection of currently due targets."""

    if daily_budget < 0:
        raise ReviewAssignmentRuleError(
            "REVIEW_BUDGET_INVALID",
            "daily review budget cannot be negative",
        )
    as_of = _utc(as_of)
    by_target: dict[str, ReviewCandidate] = {}
    for candidate in candidates:
        if not candidate.assessment_target_id or not candidate.review_state_id:
            raise ReviewAssignmentRuleError(
                "REVIEW_CANDIDATE_ID_MISSING",
                "review candidates require stable state and target ids",
            )
        if candidate.need_kind not in {"activation_due", "remediation"}:
            raise ReviewAssignmentRuleError(
                "REVIEW_CANDIDATE_NEED_KIND_INVALID",
                "review candidates require a supported need kind",
            )
        if candidate.assessment_target_id in by_target:
            raise ReviewAssignmentRuleError(
                "REVIEW_CANDIDATE_DUPLICATE",
                "only one review state may exist per assessment target",
            )
        by_target[candidate.assessment_target_id] = candidate

    due = [
        item
        for item in by_target.values()
        if item.status in {"scheduled", "remediation_due"}
        and _utc(item.due_at) <= as_of
    ]

    def effective_priority(item: ReviewCandidate) -> int:
        overdue_days = max(0, (as_of - _utc(item.due_at)).days)
        activation_boost = (
            20 if item.capability_activation_state == "due_for_reactivation" else 0
        )
        return item.priority + activation_boost + min(
            overdue_days,
            MAX_OVERDUE_PRIORITY_BOOST_DAYS,
        )

    ranked_targets = sorted(
        due,
        key=lambda item: (
            0 if item.need_kind == "activation_due" else 1,
            -effective_priority(item),
            _utc(item.due_at),
            item.assessment_target_id,
            item.review_state_id,
        ),
    )
    ranked = []
    seen_needs: set[str] = set()
    for item in ranked_targets:
        need_key = (
            f"capability:{item.capability_revision_id}"
            if item.capability_revision_id
            else f"target:{item.assessment_target_id}"
        )
        if need_key in seen_needs:
            continue
        seen_needs.add(need_key)
        ranked.append(item)
    selected = ranked[:daily_budget]
    return DailyReviewSelection(
        as_of=as_of,
        daily_budget=daily_budget,
        due_count=len(ranked),
        items=tuple(
            SelectedReview(
                review_state_id=item.review_state_id,
                assessment_target_id=item.assessment_target_id,
                due_at=_utc(item.due_at),
                base_priority=item.priority,
                effective_priority=effective_priority(item),
                rank=rank,
                need_kind=item.need_kind,
                capability_stage=item.capability_stage,
                capability_revision_id=item.capability_revision_id,
            )
            for rank, item in enumerate(selected, 1)
        ),
    )


@dataclass(frozen=True)
class ReviewAssignmentState:
    assignment_id: str
    user_id: str
    assessment_target_id: str
    due_at: datetime
    expires_at: datetime
    status: AssignmentStatus
    last_event_at: datetime
    rule_version: str = REVIEW_ASSIGNMENT_RULE_VERSION


@dataclass(frozen=True)
class ReviewAssignmentEvent:
    assignment_id: str
    event_type: AssignmentEventType
    occurred_at: datetime
    rule_version: str = REVIEW_ASSIGNMENT_RULE_VERSION


@dataclass(frozen=True)
class ReviewTransition:
    previous_status: AssignmentStatus
    state: ReviewAssignmentState
    event: ReviewAssignmentEvent
    may_create_observation: bool


_ALLOWED_TRANSITIONS: dict[AssignmentStatus, frozenset[AssignmentEventType]] = {
    "scheduled": frozenset({"presented", "expired"}),
    "presented": frozenset({"started", "skipped", "expired"}),
    "started": frozenset({"submitted", "skipped", "expired"}),
    "submitted": frozenset(),
    "skipped": frozenset(),
    "expired": frozenset(),
}


def scheduled_assignment(
    *,
    assignment_id: str,
    user_id: str,
    assessment_target_id: str,
    due_at: datetime,
    expires_at: datetime,
    scheduled_at: datetime,
) -> ReviewAssignmentState:
    due_at = _utc(due_at)
    expires_at = _utc(expires_at)
    scheduled_at = _utc(scheduled_at)
    if not assignment_id or not user_id or not assessment_target_id:
        raise ReviewAssignmentRuleError(
            "REVIEW_ASSIGNMENT_ID_MISSING",
            "assignment, user, and assessment target ids are required",
        )
    if expires_at < due_at:
        raise ReviewAssignmentRuleError(
            "REVIEW_ASSIGNMENT_WINDOW_INVALID",
            "review assignment expiry cannot precede its due time",
        )
    return ReviewAssignmentState(
        assignment_id=assignment_id,
        user_id=user_id,
        assessment_target_id=assessment_target_id,
        due_at=due_at,
        expires_at=expires_at,
        status="scheduled",
        last_event_at=scheduled_at,
    )


def transition_assignment(
    state: ReviewAssignmentState,
    *,
    event_type: AssignmentEventType,
    occurred_at: datetime,
) -> ReviewTransition:
    occurred_at = _utc(occurred_at)
    if event_type not in _ALLOWED_TRANSITIONS[state.status]:
        raise ReviewAssignmentRuleError(
            "REVIEW_ASSIGNMENT_TRANSITION_INVALID",
            f"cannot apply {event_type} while assignment is {state.status}",
        )
    if occurred_at < _utc(state.last_event_at):
        raise ReviewAssignmentRuleError(
            "REVIEW_ASSIGNMENT_EVENT_OUT_OF_ORDER",
            "review assignment events must be applied in event-time order",
        )
    if event_type == "expired" and occurred_at < _utc(state.expires_at):
        raise ReviewAssignmentRuleError(
            "REVIEW_ASSIGNMENT_NOT_EXPIRED",
            "an assignment cannot expire before its expiry time",
        )
    if event_type in {"presented", "started"} and occurred_at > _utc(
        state.expires_at
    ):
        raise ReviewAssignmentRuleError(
            "REVIEW_ASSIGNMENT_EXPIRED",
            "an expired assignment cannot be presented or started",
        )
    event = ReviewAssignmentEvent(
        assignment_id=state.assignment_id,
        event_type=event_type,
        occurred_at=occurred_at,
    )
    next_state = replace(
        state,
        status=event_type,
        last_event_at=occurred_at,
    )
    return ReviewTransition(
        previous_status=state.status,
        state=next_state,
        event=event,
        may_create_observation=event_type == "submitted",
    )


@dataclass(frozen=True)
class ReviewSubmission:
    assignment_id: str
    assessment_target_id: str
    submitted_at: datetime
    assistance_mode: str
    item_signatures: frozenset[str]
    prior_item_signatures: frozenset[str]


@dataclass(frozen=True)
class RetentionQualification:
    eligible: bool
    status: Literal["eligible", "ineligible"]
    reasons: tuple[str, ...]
    rule_version: str = RETENTION_QUALIFICATION_RULE_VERSION


def qualify_retention_submission(
    state: ReviewAssignmentState,
    submission: ReviewSubmission,
) -> RetentionQualification:
    """Qualify a server-bound submission; never infer eligibility from a quiz alone."""

    submitted_at = _utc(submission.submitted_at)
    signatures = frozenset(value for value in submission.item_signatures if value)
    prior_signatures = frozenset(
        value for value in submission.prior_item_signatures if value
    )
    reasons = []
    if submission.assignment_id != state.assignment_id:
        reasons.append("assignment_mismatch")
    if submission.assessment_target_id != state.assessment_target_id:
        reasons.append("assessment_target_mismatch")
    if state.status != "started":
        reasons.append("assignment_not_started")
    if submitted_at < _utc(state.due_at):
        reasons.append("assignment_not_due")
    if submitted_at > _utc(state.expires_at):
        reasons.append("assignment_expired")
    if submission.assistance_mode != "unassisted_review":
        reasons.append("assistance_not_unassisted")
    if not signatures:
        reasons.append("item_signature_missing")
    elif signatures.intersection(prior_signatures):
        reasons.append("item_not_novel")
    result = tuple(reasons)
    return RetentionQualification(
        eligible=not result,
        status="eligible" if not result else "ineligible",
        reasons=result,
    )
