import json
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.ai.context import KnowledgeContextBudget
from app.core.errors import AppError
from app.infrastructure.tables import (
    Base,
    Concept,
    ConceptRelationVersion,
    ConceptRevision,
    KnowledgeClaimBinding,
    KnowledgeGraphRelease,
    KnowledgeSourceVersion,
    LearningContractConcept,
    LearningContractVersion,
    SeriesCurriculumBaselineBinding,
    SourceClaimVersion,
)
from app.modules.knowledge.context import KnowledgeContextBuilder


def _db() -> Session:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    return Session(engine)


def _contract(db: Session, concept_ids: list[str]) -> LearningContractVersion:
    contract = LearningContractVersion(
        id="contract_1",
        section_id="section_1",
        mission_version_id="mission_1",
        version=1,
        section_question_snapshot="如何从递归过渡到搜索？",
        target_depth="deep",
        contract_hash="contract_hash_1",
    )
    db.add(contract)
    for position, concept_id in enumerate(concept_ids):
        db.add(
            LearningContractConcept(
                id=f"contract_concept_{position}",
                contract_version_id=contract.id,
                concept_revision_id=concept_id,
                position=position,
                role="primary",
                required=True,
            )
        )
    return contract


def _published_graph(db: Session, *, seed_status: str = "reviewed") -> None:
    db.add(
        SeriesCurriculumBaselineBinding(
            id="series_baseline_1",
            series_id="series_1",
            baseline_version_id="baseline_1",
            selection_reason="M2 thin slice",
        )
    )
    concept_revision_ids = []
    for key in ("recursion", "search", "dynamic_programming", "memoization"):
        concept = Concept(
            id=f"concept_{key}",
            namespace="test",
            concept_key=key,
            canonical_name=key,
            status="active",
        )
        revision = ConceptRevision(
            id=f"revision_{key}",
            concept_id=concept.id,
            revision=1,
            label=key,
            definition=f"definition of {key}",
            verification_status=(seed_status if key == "recursion" else "reviewed"),
        )
        db.add_all([concept, revision])
        concept_revision_ids.append(revision.id)

    release = KnowledgeGraphRelease(
        id="release_1",
        baseline_version_id="baseline_1",
        version=1,
        status="published",
        manifest_json="{}",
        content_hash="release_hash_1",
    )
    db.add(release)
    relation_ids = []
    for position, (left, right, relation_type) in enumerate(
        (
            ("recursion", "search", "prerequisite_of"),
            ("search", "dynamic_programming", "contrasts_with"),
            ("dynamic_programming", "memoization", "implemented_by"),
        ),
        1,
    ):
        relation = ConceptRelationVersion(
            id=f"relation_{position}",
            release_id=release.id,
            from_concept_revision_id=f"revision_{left}",
            to_concept_revision_id=f"revision_{right}",
            relation_type=relation_type,
            relation_revision=1,
            status="published",
        )
        db.add(relation)
        relation_ids.append(relation.id)

    claim = SourceClaimVersion(
        id="claim_1",
        source_claim_id="claim_identity_1",
        version=1,
        statement="搜索会系统枚举候选状态。",
        claim_kind="mechanism",
        scope_json=json.dumps(
            {"conceptRevisionIds": ["revision_search"]}, ensure_ascii=False
        ),
        strict=True,
        trust_state="verified",
        generation_method="human_reviewed_source",
        status="published",
    )
    source = KnowledgeSourceVersion(
        id="knowledge_source_1",
        source_key="source_1",
        source_kind="official_documentation",
        title="公开算法资料",
        authority="test authority",
        url="https://example.edu/algorithms",
        version_label="2026-08-09 snapshot",
        retrieval_date="2026-08-09",
        content_digest="a" * 64,
        rights_status="public_web",
        verification_status="verified",
    )
    claim_binding = KnowledgeClaimBinding(
        id="knowledge_claim_binding_1",
        release_id=release.id,
        source_claim_version_id=claim.id,
        knowledge_source_version_id=source.id,
        locator_type="section",
        locator_json=json.dumps({"heading": "Search"}),
        locator_hash="b" * 64,
        excerpt_hash="c" * 64,
        support_type="supports",
        verification_status="verified",
    )
    db.add_all([claim, source, claim_binding])
    release.manifest_json = json.dumps(
        {
            "conceptRevisionIds": concept_revision_ids,
            "objectiveIds": [],
            "relationVersionIds": relation_ids,
            "claimVersionIds": [claim.id],
            "sourceVersionIds": [source.id],
            "claimBindingIds": [claim_binding.id],
        }
    )
    db.commit()


def test_bounded_published_graph_materializes_deterministic_context_and_audit():
    db = _db()
    _published_graph(db)
    contract = _contract(db, ["revision_recursion"])
    db.commit()

    pack = KnowledgeContextBuilder(db).build(
        operation="lesson_content",
        series=SimpleNamespace(id="series_1"),
        contract=contract,
        budget=KnowledgeContextBudget(maxNodes=3, maxEdges=2, maxHops=3),
    )

    assert pack.status == "ready"
    assert [item["conceptRevisionId"] for item in pack.nodes] == [
        "revision_recursion",
        "revision_search",
        "revision_dynamic_programming",
    ]
    assert [item["relationVersionId"] for item in pack.edges] == [
        "relation_1",
        "relation_2",
    ]
    assert pack.claims[0]["claimVersionId"] == "claim_1"
    assert pack.truncation["truncated"] is True
    assert {item["code"] for item in pack.truncation["reasons"]} == {
        "maxNodes"
    }
    audit = pack.audit_manifest()
    assert audit["budget"] == {"maxNodes": 3, "maxEdges": 2, "maxHops": 3}
    assert audit["actual"] == {"nodeCount": 3, "edgeCount": 2, "claimCount": 1}
    assert audit["actualSubgraph"]["edges"][0] == {
        "relationVersionId": "relation_1",
        "fromConceptRevisionId": "revision_recursion",
        "toConceptRevisionId": "revision_search",
        "relationType": "prerequisite_of",
    }
    assert audit["contextHash"] == pack.context_hash


def test_formal_baseline_without_published_release_fails_before_generation():
    db = _db()
    db.add(
        SeriesCurriculumBaselineBinding(
            id="series_baseline_1",
            series_id="series_1",
            baseline_version_id="baseline_1",
            selection_reason="formal course",
        )
    )
    db.commit()

    with pytest.raises(AppError) as raised:
        KnowledgeContextBuilder(db).build(
            operation="lesson_content",
            series=SimpleNamespace(id="series_1"),
            contract=SimpleNamespace(id="contract_1"),
        )

    assert raised.value.code == "KNOWLEDGE_GRAPH_RELEASE_MISSING"


def test_pre_contract_chapter_planning_uses_curriculum_without_fake_graph_pack():
    db = _db()
    db.add(
        SeriesCurriculumBaselineBinding(
            id="series_baseline_1",
            series_id="series_1",
            baseline_version_id="baseline_1",
            selection_reason="formal course",
        )
    )
    db.commit()

    pack = KnowledgeContextBuilder(db).build(
        operation="chapter",
        series=SimpleNamespace(id="series_1"),
        contract=None,
    )

    assert pack.status == "not_applicable"
    assert pack.reason == "pre_contract_operation_uses_curriculum_authority_only"
    assert pack.baseline_version_id == "baseline_1"
    assert pack.release_id == ""
    assert pack.nodes == []


def test_formal_lesson_without_contract_still_fails_closed():
    db = _db()
    db.add(
        SeriesCurriculumBaselineBinding(
            id="series_baseline_1",
            series_id="series_1",
            baseline_version_id="baseline_1",
            selection_reason="formal course",
        )
    )
    db.commit()

    with pytest.raises(AppError) as raised:
        KnowledgeContextBuilder(db).build(
            operation="lesson_content",
            series=SimpleNamespace(id="series_1"),
            contract=None,
        )

    assert raised.value.code == "KNOWLEDGE_CONTEXT_CONTRACT_MISSING"


@pytest.mark.parametrize(
    ("budget", "node_count", "edge_count", "reason"),
    [
        (
            KnowledgeContextBudget(maxNodes=4, maxEdges=1, maxHops=3),
            2,
            1,
            "maxEdges",
        ),
        (
            KnowledgeContextBudget(maxNodes=4, maxEdges=3, maxHops=1),
            2,
            1,
            "maxHops",
        ),
    ],
)
def test_edge_and_hop_budgets_are_independently_audited(
    budget,
    node_count,
    edge_count,
    reason,
):
    db = _db()
    _published_graph(db)
    contract = _contract(db, ["revision_recursion"])
    db.commit()

    pack = KnowledgeContextBuilder(db).build(
        operation="lesson_content",
        series=SimpleNamespace(id="series_1"),
        contract=contract,
        budget=budget,
    )

    assert len(pack.nodes) == node_count
    assert len(pack.edges) == edge_count
    assert {item["code"] for item in pack.truncation["reasons"]} == {reason}


def test_unreviewed_contract_seed_is_never_silently_used():
    db = _db()
    _published_graph(db, seed_status="unverified")
    contract = _contract(db, ["revision_recursion"])
    db.commit()

    with pytest.raises(AppError) as raised:
        KnowledgeContextBuilder(db).build(
            operation="lesson_content",
            series=SimpleNamespace(id="series_1"),
            contract=contract,
        )

    assert raised.value.code == "KNOWLEDGE_GRAPH_CONCEPT_UNPUBLISHED"


def test_series_without_curriculum_authority_is_explicitly_not_applicable():
    db = _db()

    pack = KnowledgeContextBuilder(db).build(
        operation="lesson_content",
        series=SimpleNamespace(id="ordinary_series"),
        contract=None,
    )

    assert pack.status == "not_applicable"
    assert pack.reason == "series_has_no_published_curriculum_baseline_binding"
    assert pack.nodes == []
    assert pack.context_hash
