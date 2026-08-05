from datetime import datetime, timedelta, timezone

import pytest

from app.modules.learning.review_assignments import (
    RETENTION_QUALIFICATION_RULE_VERSION,
    REVIEW_ASSIGNMENT_RULE_VERSION,
    ReviewAssignmentRuleError,
    ReviewCandidate,
    ReviewSubmission,
    qualify_retention_submission,
    scheduled_assignment,
    select_daily_reviews,
    transition_assignment,
)


NOW = datetime(2026, 8, 4, 9, 0, tzinfo=timezone.utc)


def candidate(target: str, *, due_days: int, priority: int) -> ReviewCandidate:
    return ReviewCandidate(
        review_state_id=f"state-{target}",
        assessment_target_id=target,
        due_at=NOW + timedelta(days=due_days),
        priority=priority,
    )


def started_assignment():
    state = scheduled_assignment(
        assignment_id="assignment-1",
        user_id="user-1",
        assessment_target_id="target-1",
        due_at=NOW,
        expires_at=NOW + timedelta(days=2),
        scheduled_at=NOW - timedelta(days=1),
    )
    state = transition_assignment(
        state,
        event_type="presented",
        occurred_at=NOW,
    ).state
    return transition_assignment(
        state,
        event_type="started",
        occurred_at=NOW + timedelta(minutes=1),
    ).state


def eligible_submission(**changes) -> ReviewSubmission:
    values = {
        "assignment_id": "assignment-1",
        "assessment_target_id": "target-1",
        "submitted_at": NOW + timedelta(minutes=5),
        "assistance_mode": "unassisted_review",
        "item_signatures": frozenset({"question:new"}),
        "prior_item_signatures": frozenset({"question:old"}),
    }
    values.update(changes)
    return ReviewSubmission(**values)


def test_daily_selection_is_due_budgeted_and_stable_across_input_order():
    values = [
        candidate("target-c", due_days=-2, priority=60),
        candidate("target-a", due_days=-1, priority=70),
        candidate("target-b", due_days=-5, priority=67),
        candidate("target-future", due_days=1, priority=999),
    ]

    first = select_daily_reviews(values, daily_budget=2, as_of=NOW)
    second = select_daily_reviews(list(reversed(values)), daily_budget=2, as_of=NOW)

    assert [item.assessment_target_id for item in first.items] == [
        "target-b",
        "target-a",
    ]
    assert first == second
    assert first.due_count == 3
    assert first.rule_version == REVIEW_ASSIGNMENT_RULE_VERSION
    assert [item.rank for item in first.items] == [1, 2]


def test_daily_selection_uses_ids_as_final_deterministic_tie_breaker():
    values = [
        candidate("target-b", due_days=-1, priority=50),
        candidate("target-a", due_days=-1, priority=50),
    ]

    selected = select_daily_reviews(values, daily_budget=10, as_of=NOW)

    assert [item.assessment_target_id for item in selected.items] == [
        "target-a",
        "target-b",
    ]


def test_daily_selection_rejects_duplicate_target_authorities():
    with pytest.raises(ReviewAssignmentRuleError) as error:
        select_daily_reviews(
            [
                candidate("target-a", due_days=-1, priority=10),
                ReviewCandidate(
                    review_state_id="other-state",
                    assessment_target_id="target-a",
                    due_at=NOW,
                    priority=20,
                ),
            ],
            daily_budget=1,
            as_of=NOW,
        )

    assert error.value.code == "REVIEW_CANDIDATE_DUPLICATE"


def test_assignment_follows_the_happy_path_and_only_submission_may_observe():
    state = scheduled_assignment(
        assignment_id="assignment-1",
        user_id="user-1",
        assessment_target_id="target-1",
        due_at=NOW,
        expires_at=NOW + timedelta(days=2),
        scheduled_at=NOW - timedelta(days=1),
    )

    presented = transition_assignment(state, event_type="presented", occurred_at=NOW)
    started = transition_assignment(
        presented.state,
        event_type="started",
        occurred_at=NOW + timedelta(minutes=1),
    )
    submitted = transition_assignment(
        started.state,
        event_type="submitted",
        occurred_at=NOW + timedelta(minutes=5),
    )

    assert presented.may_create_observation is False
    assert started.may_create_observation is False
    assert submitted.may_create_observation is True
    assert submitted.state.status == "submitted"
    assert submitted.event.rule_version == REVIEW_ASSIGNMENT_RULE_VERSION


@pytest.mark.parametrize("terminal_event", ["skipped", "expired"])
def test_skipped_or_expired_assignment_is_terminal_and_never_observes(terminal_event):
    state = scheduled_assignment(
        assignment_id="assignment-1",
        user_id="user-1",
        assessment_target_id="target-1",
        due_at=NOW,
        expires_at=NOW + timedelta(days=1),
        scheduled_at=NOW - timedelta(days=1),
    )
    state = transition_assignment(state, event_type="presented", occurred_at=NOW).state
    occurred_at = NOW + timedelta(days=1) if terminal_event == "expired" else NOW

    terminal = transition_assignment(
        state,
        event_type=terminal_event,
        occurred_at=occurred_at,
    )

    assert terminal.may_create_observation is False
    with pytest.raises(ReviewAssignmentRuleError) as error:
        transition_assignment(
            terminal.state,
            event_type="started",
            occurred_at=occurred_at + timedelta(minutes=1),
        )
    assert error.value.code == "REVIEW_ASSIGNMENT_TRANSITION_INVALID"


def test_assignment_rejects_out_of_order_and_premature_expiry_events():
    state = scheduled_assignment(
        assignment_id="assignment-1",
        user_id="user-1",
        assessment_target_id="target-1",
        due_at=NOW,
        expires_at=NOW + timedelta(days=1),
        scheduled_at=NOW,
    )

    with pytest.raises(ReviewAssignmentRuleError) as out_of_order:
        transition_assignment(
            state,
            event_type="presented",
            occurred_at=NOW - timedelta(seconds=1),
        )
    assert out_of_order.value.code == "REVIEW_ASSIGNMENT_EVENT_OUT_OF_ORDER"

    with pytest.raises(ReviewAssignmentRuleError) as premature:
        transition_assignment(state, event_type="expired", occurred_at=NOW)
    assert premature.value.code == "REVIEW_ASSIGNMENT_NOT_EXPIRED"


def test_due_started_unassisted_novel_bound_submission_is_retention_eligible():
    result = qualify_retention_submission(
        started_assignment(),
        eligible_submission(),
    )

    assert result.eligible is True
    assert result.status == "eligible"
    assert result.reasons == ()
    assert result.rule_version == RETENTION_QUALIFICATION_RULE_VERSION


@pytest.mark.parametrize(
    ("state_change", "submission_change", "reason"),
    [
        ({"status": "presented"}, {}, "assignment_not_started"),
        ({}, {"assignment_id": "other"}, "assignment_mismatch"),
        ({}, {"assessment_target_id": "other"}, "assessment_target_mismatch"),
        ({}, {"submitted_at": NOW - timedelta(seconds=1)}, "assignment_not_due"),
        ({}, {"submitted_at": NOW + timedelta(days=3)}, "assignment_expired"),
        ({}, {"assistance_mode": "assisted_immediate"}, "assistance_not_unassisted"),
        ({}, {"item_signatures": frozenset()}, "item_signature_missing"),
        (
            {},
            {
                "item_signatures": frozenset({"question:same"}),
                "prior_item_signatures": frozenset({"question:same"}),
            },
            "item_not_novel",
        ),
    ],
)
def test_retention_qualification_names_every_failed_boundary(
    state_change,
    submission_change,
    reason,
):
    state = started_assignment()
    if state_change:
        state = type(state)(**{**state.__dict__, **state_change})

    result = qualify_retention_submission(
        state,
        eligible_submission(**submission_change),
    )

    assert result.eligible is False
    assert result.status == "ineligible"
    assert reason in result.reasons


def test_naive_datetimes_are_normalized_to_utc_for_deterministic_replay():
    naive_now = NOW.replace(tzinfo=None)
    selection = select_daily_reviews(
        [
            ReviewCandidate(
                review_state_id="state-1",
                assessment_target_id="target-1",
                due_at=naive_now,
                priority=1,
            )
        ],
        daily_budget=1,
        as_of=naive_now,
    )

    assert selection.as_of.tzinfo == timezone.utc
    assert selection.items[0].due_at.tzinfo == timezone.utc
