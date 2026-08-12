import json

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.infrastructure.tables import (
    AssessmentDistractorDiagnostic,
    AssessmentItemEvidenceBlock,
    AssessmentItemVersion,
    Base,
    ContentBlockVersion,
    QuizAttempt,
    QuizSet,
)
from app.modules.learning.remediation_diagnosis import (
    choose_remediation_strategy,
    diagnose_failed_attempt,
)


def _question(item_id: str, target_id: str) -> dict:
    return {
        "id": item_id,
        "itemKey": item_id,
        "assessmentTargetId": target_id,
        "objective": "解释机制",
        "core": True,
        "prompt": "哪个判断成立？",
        "options": ["错误", "正确", "另一个错误"],
        "correct": [1],
        "explanation": "依据正文机制。",
        "evidenceBlockIds": ["block_1"],
    }


def _attempt_with_signals(causes: list[str | None]) -> tuple[Session, QuizAttempt]:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    db = Session(engine)
    quiz = QuizSet(
        id="quiz_1", section_id="section_1", content_version_id="content_1",
        learning_contract_version_id="contract_1", generation=1,
        questions_json="[]", schema_version="lesson_candidate_v3",
    )
    db.add(quiz)
    db.add(ContentBlockVersion(
        id="block_1", content_version_id="content_1", position=0,
        format_kind="text", semantic_role="core_instruction", content="正文",
    ))
    questions = []
    for index, cause in enumerate(causes):
        item_id = f"item_{index}"
        payload = _question(item_id, "target_1")
        questions.append(payload)
        db.add(AssessmentItemVersion(
            id=item_id, quiz_set_id=quiz.id, assessment_target_id="target_1",
            position=index, item_key=item_id, payload_json=json.dumps(payload),
        ))
        db.add(AssessmentItemEvidenceBlock(
            id=f"binding_{index}", assessment_item_version_id=item_id,
            content_block_version_id="block_1",
        ))
        if cause:
            db.add(AssessmentDistractorDiagnostic(
                id=f"diagnostic_{index}", assessment_item_version_id=item_id,
                option_index=0, option_hash="hash", cause_code=cause,
                rationale="该选项跳过了机制。",
            ))
    quiz.questions_json = json.dumps(questions)
    attempt = QuizAttempt(
        id="attempt_1", quiz_set_id=quiz.id,
        learning_contract_version_id="contract_1", content_version_id="content_1",
        learning_run_id="run_1", user_id="user_1", answers_json=json.dumps([[0]] * len(causes)),
        results_json=json.dumps([
            {
                "correct": False, "selectedOptions": [0], "correctOptions": [1],
                "incorrectOptions": [0], "missedOptions": [1],
            }
            for _ in causes
        ]),
        passed=False,
    )
    db.add(attempt)
    db.commit()
    return db, attempt


def test_two_concordant_signals_support_a_mechanism_diagnosis():
    db, attempt = _attempt_with_signals([
        "mechanism_reasoning_break", "mechanism_reasoning_break"
    ])
    diagnoses = diagnose_failed_attempt(db, attempt)
    assert diagnoses == [{
        "assessmentTargetId": "target_1",
        "causeCode": "mechanism_reasoning_break",
        "status": "supported",
        "confidence": 0.9,
        "evidenceCount": 2,
        "recommendedStrategy": "mechanism_walkthrough",
    }]
    assert choose_remediation_strategy(diagnoses) == "mechanism_walkthrough"


def test_conflicting_or_missing_signals_abstain_instead_of_guessing():
    db, attempt = _attempt_with_signals([
        "concept_confusion", "boundary_comparison_error", None
    ])
    diagnoses = diagnose_failed_attempt(db, attempt)
    assert diagnoses[0]["causeCode"] == "insufficient_evidence"
    assert diagnoses[0]["status"] == "abstained"
    assert choose_remediation_strategy(diagnoses) == "diagnostic_probe"
