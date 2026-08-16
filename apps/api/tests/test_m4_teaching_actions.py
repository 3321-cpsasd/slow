from app.application.section_generation import teaching_action_snapshot


TARGETS = [{
    "assessmentTargetId": "target-new-book",
    "conceptRevisionId": "concept-target",
    "objective": "应用目标概念",
}]


def test_cross_book_state_matches_stable_concept_not_only_target_id():
    actions = teaching_action_snapshot(
        [{
            "assessmentTargetId": "target-old-book",
            "teachingAction": "compress",
            "sourceObservationWatermark": 17,
            "knowledgeNode": {"conceptRevisionId": "concept-target"},
        }],
        TARGETS,
        {"edges": []},
    )

    assert actions == [{
        "assessmentTargetId": "target-old-book",
        "teachingAction": "compress",
        "sourceObservationWatermark": 17,
        "knowledgeNode": {"conceptRevisionId": "concept-target"},
        "conceptRevisionId": "concept-target",
        "reasonCode": "stable_prior_capability",
        "evidenceWatermark": 17,
        "decisionRuleVersion": "teaching_action_v1",
    }]


def test_one_missing_direct_prerequisite_becomes_scaffold():
    actions = teaching_action_snapshot(
        [],
        TARGETS,
        {"edges": [{
            "relationType": "prerequisite_of",
            "fromConceptRevisionId": "concept-prerequisite",
            "toConceptRevisionId": "concept-target",
        }]},
    )

    assert actions[0]["teachingAction"] == "scaffold"
    assert actions[0]["requiredByConceptRevisionId"] == "concept-target"


def test_cross_book_state_matches_stable_capability_binding():
    actions = teaching_action_snapshot(
        [{
            "assessmentTargetId": "target-old-book",
            "teachingAction": "connect",
            "sourceObservationWatermark": 23,
            "capabilityState": {
                "capabilityRevisionId": "capability-shared",
                "stage": "bronze",
            },
        }],
        [{
            "assessmentTargetId": "target-new-book",
            "conceptRevisionId": "concept-expanded-scope",
            "capabilityRevisionId": "capability-shared",
            "objective": "在新一册继续推进稳定能力",
        }],
        {"edges": []},
    )

    assert len(actions) == 1
    assert actions[0]["capabilityRevisionId"] == "capability-shared"
    assert actions[0]["teachingAction"] == "connect"
    assert actions[0]["reasonCode"] == "qualified_prior_understanding"


def test_multiple_missing_direct_prerequisites_require_replan():
    actions = teaching_action_snapshot(
        [],
        TARGETS,
        {"edges": [
            {
                "relationType": "prerequisite_of",
                "fromConceptRevisionId": prerequisite,
                "toConceptRevisionId": "concept-target",
            }
            for prerequisite in ("concept-p1", "concept-p2")
        ]},
    )

    assert actions[0]["teachingAction"] == "replan"
    assert actions[0]["blockingPrerequisiteConceptRevisionIds"] == [
        "concept-p1",
        "concept-p2",
    ]
