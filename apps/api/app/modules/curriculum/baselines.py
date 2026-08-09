import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, HttpUrl, model_validator
from sqlalchemy import select
from sqlalchemy.orm import Session

from ...core.errors import AppError
from ...infrastructure.tables import (
    ChapterCurriculumObjectiveBinding,
    Concept,
    ConceptObjectiveBinding,
    ConceptRevision,
    Competency,
    CourseVersion,
    CurriculumBaselineVersion,
    CurriculumSourceVersion,
    Discipline,
    KnowledgeGraphRelease,
    LearningObjective,
    ProgramVersion,
    SeriesCurriculumBaselineBinding,
    now,
)


class BaselineModel(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)


class SourceLocator(BaselineModel):
    source_key: str = Field(alias="sourceKey", min_length=1, max_length=160)
    locator: str = Field(min_length=1, max_length=500)
    support: Literal["direct", "derived"]


class CurriculumSourceInput(BaselineModel):
    source_key: str = Field(alias="sourceKey", min_length=1, max_length=160)
    source_type: Literal[
        "training_program",
        "course_syllabus",
        "course_description",
        "textbook",
        "professional_standard",
    ] = Field(alias="sourceType")
    title: str = Field(min_length=1, max_length=500)
    authority: str = Field(min_length=1, max_length=240)
    url: HttpUrl
    version_label: str = Field(alias="versionLabel", min_length=1, max_length=160)
    publication_date: str = Field(default="", alias="publicationDate", max_length=32)
    applicability: dict = Field(default_factory=dict)
    retrieval_date: str = Field(alias="retrievalDate", min_length=10, max_length=32)
    content_digest: str = Field(alias="contentDigest", pattern=r"^[a-f0-9]{64}$")
    verification_status: Literal["candidate", "reachable", "reviewed"] = Field(
        default="candidate",
        alias="verificationStatus",
    )
    provenance: dict = Field(default_factory=dict)


class DisciplineInput(BaselineModel):
    code: str = Field(min_length=1, max_length=80)
    name: str = Field(min_length=1, max_length=240)
    jurisdiction: str = Field(default="", max_length=80)


class ProgramInput(BaselineModel):
    institution: str = Field(min_length=1, max_length=240)
    program_code: str = Field(alias="programCode", min_length=1, max_length=80)
    name: str = Field(min_length=1, max_length=300)
    version_label: str = Field(alias="versionLabel", min_length=1, max_length=160)
    source_key: str = Field(alias="sourceKey", min_length=1, max_length=160)
    applicability: dict = Field(default_factory=dict)


class CourseInput(BaselineModel):
    course_code: str = Field(alias="courseCode", min_length=1, max_length=80)
    title: str = Field(min_length=1, max_length=300)
    version_label: str = Field(alias="versionLabel", min_length=1, max_length=160)
    course_type: str = Field(default="", alias="courseType", max_length=80)
    credits: dict = Field(default_factory=dict)
    assessment: dict = Field(default_factory=dict)
    aliases: list[str] = Field(default_factory=list, max_length=20)


class CompetencyInput(BaselineModel):
    key: str = Field(min_length=1, max_length=160)
    statement: str = Field(min_length=4, max_length=1000)
    competency_type: Literal["knowledge", "skill", "practice", "transfer"] = Field(
        alias="competencyType"
    )
    verification_modes: list[
        Literal["choice_quiz", "code_task", "oral", "project", "written_response"]
    ] = Field(alias="verificationModes", min_length=1, max_length=5)
    sources: list[SourceLocator] = Field(min_length=1, max_length=8)


class ObjectiveInput(BaselineModel):
    key: str = Field(min_length=1, max_length=160)
    statement: str = Field(min_length=4, max_length=1000)
    competency_keys: list[str] = Field(alias="competencyKeys", min_length=1, max_length=8)
    required: bool = True
    verification_policy: Literal[
        "choice_quiz_v1",
        "choice_plus_code_v1",
        "code_task_v1",
        "oral_v1",
        "project_v1",
        "written_response_v1",
    ] = Field(alias="verificationPolicy")
    sources: list[SourceLocator] = Field(min_length=1, max_length=8)


class ConceptInput(BaselineModel):
    key: str = Field(min_length=1, max_length=160)
    label: str = Field(min_length=1, max_length=300)
    scope: str = Field(min_length=1, max_length=1200)
    objective_keys: list[str] = Field(alias="objectiveKeys", min_length=1, max_length=12)
    sources: list[SourceLocator] = Field(min_length=1, max_length=8)


class RelationInput(BaselineModel):
    from_concept_key: str = Field(alias="fromConceptKey")
    to_concept_key: str = Field(alias="toConceptKey")
    relation_type: Literal[
        "prerequisite_for", "part_of", "contrasts_with", "applies_to"
    ] = Field(alias="relationType")
    review_status: Literal["candidate", "reviewed"] = Field(
        default="candidate", alias="reviewStatus"
    )
    sources: list[SourceLocator] = Field(min_length=1, max_length=8)


class BaselineGapInput(BaselineModel):
    code: str = Field(min_length=1, max_length=100)
    severity: Literal["warning", "blocking"]
    message: str = Field(min_length=4, max_length=1000)
    source_keys: list[str] = Field(default_factory=list, alias="sourceKeys")


class BaselineSourceReviewDecision(BaselineModel):
    source_key: str = Field(alias="sourceKey", min_length=1, max_length=160)
    decision: Literal["reviewed", "rejected"]
    note: str = Field(min_length=4, max_length=1000)


class BaselineRelationReviewDecision(BaselineModel):
    from_concept_key: str = Field(alias="fromConceptKey", min_length=1, max_length=160)
    to_concept_key: str = Field(alias="toConceptKey", min_length=1, max_length=160)
    relation_type: Literal[
        "prerequisite_for", "part_of", "contrasts_with", "applies_to"
    ] = Field(alias="relationType")
    decision: Literal["reviewed", "rejected"]
    note: str = Field(min_length=4, max_length=1000)


class BaselineGapReviewDecision(BaselineModel):
    code: str = Field(min_length=1, max_length=100)
    disposition: Literal["resolved", "accepted_risk", "deferred"]
    remaining_blocking_stages: list[
        Literal["baseline_publication", "knowledge_publication", "assessment_evidence"]
    ] = Field(default_factory=list, alias="remainingBlockingStages", max_length=3)
    note: str = Field(min_length=4, max_length=1000)


class CodeRubricDimension(BaselineModel):
    key: str = Field(min_length=1, max_length=80)
    label: str = Field(min_length=1, max_length=160)
    weight: int = Field(ge=1, le=100)


class PlatformCodeAssessmentPolicy(BaselineModel):
    policy_key: str = Field(alias="policyKey", min_length=1, max_length=120)
    authority: Literal["slow_platform"] = "slow_platform"
    official_course_policy: Literal[False] = Field(
        default=False,
        alias="officialCoursePolicy",
    )
    dimensions: list[CodeRubricDimension] = Field(min_length=1, max_length=10)
    required_dimension_minimums: dict[str, int] = Field(
        alias="requiredDimensionMinimums"
    )
    pass_score: int = Field(alias="passScore", ge=1, le=100)

    @model_validator(mode="after")
    def weights_and_required_dimensions_are_valid(self):
        keys = [item.key for item in self.dimensions]
        if len(keys) != len(set(keys)):
            raise ValueError("duplicate code rubric dimension")
        if sum(item.weight for item in self.dimensions) != 100:
            raise ValueError("code rubric weights must total 100")
        if any(key not in keys for key in self.required_dimension_minimums):
            raise ValueError("code rubric minimum references an unknown dimension")
        if any(value < 0 or value > 100 for value in self.required_dimension_minimums.values()):
            raise ValueError("code rubric minimum must be between 0 and 100")
        return self


class CurriculumBaselineReview(BaselineModel):
    schema_version: Literal["curriculum_baseline_review_v1"] = Field(
        alias="schemaVersion"
    )
    baseline_key: str = Field(alias="baselineKey", min_length=1, max_length=160)
    baseline_version: int = Field(alias="baselineVersion", ge=1)
    baseline_content_hash: str = Field(
        alias="baselineContentHash",
        pattern=r"^[a-f0-9]{64}$",
    )
    reviewer_id: str = Field(alias="reviewerId", min_length=1, max_length=160)
    confirmation_reference: str = Field(
        alias="confirmationReference",
        min_length=4,
        max_length=500,
    )
    final_decision: Literal["approved", "rejected"] = Field(alias="finalDecision")
    sources: list[BaselineSourceReviewDecision] = Field(min_length=1, max_length=20)
    relations: list[BaselineRelationReviewDecision] = Field(default_factory=list, max_length=1200)
    gaps: list[BaselineGapReviewDecision] = Field(default_factory=list, max_length=100)
    platform_code_assessment: PlatformCodeAssessmentPolicy | None = Field(
        default=None,
        alias="platformCodeAssessment",
    )


class CurriculumBaselinePackage(BaselineModel):
    schema_version: Literal["curriculum_baseline_package_v1"] = Field(
        alias="schemaVersion"
    )
    baseline_key: str = Field(alias="baselineKey", min_length=1, max_length=160)
    version: int = Field(ge=1)
    title: str = Field(min_length=1, max_length=300)
    status: Literal["candidate"] = "candidate"
    match_terms: list[str] = Field(alias="matchTerms", min_length=1, max_length=30)
    discipline: DisciplineInput
    program: ProgramInput
    course: CourseInput
    sources: list[CurriculumSourceInput] = Field(min_length=1, max_length=20)
    competencies: list[CompetencyInput] = Field(min_length=1, max_length=40)
    objectives: list[ObjectiveInput] = Field(min_length=1, max_length=120)
    concepts: list[ConceptInput] = Field(min_length=1, max_length=300)
    relations: list[RelationInput] = Field(default_factory=list, max_length=1200)
    gaps: list[BaselineGapInput] = Field(default_factory=list, max_length=100)

    @model_validator(mode="after")
    def references_are_closed(self):
        source_keys = [item.source_key for item in self.sources]
        competency_keys = [item.key for item in self.competencies]
        objective_keys = [item.key for item in self.objectives]
        concept_keys = [item.key for item in self.concepts]
        for label, values in (
            ("source", source_keys),
            ("competency", competency_keys),
            ("objective", objective_keys),
            ("concept", concept_keys),
        ):
            if len(values) != len(set(values)):
                raise ValueError(f"duplicate {label} key")
        if self.program.source_key not in source_keys:
            raise ValueError("program references an unknown source")
        for item in [*self.competencies, *self.objectives, *self.concepts, *self.relations]:
            if any(source.source_key not in source_keys for source in item.sources):
                raise ValueError("baseline item references an unknown source")
        for item in self.objectives:
            if any(key not in competency_keys for key in item.competency_keys):
                raise ValueError("objective references an unknown competency")
        for item in self.concepts:
            if any(key not in objective_keys for key in item.objective_keys):
                raise ValueError("concept references an unknown objective")
        for item in self.relations:
            if item.from_concept_key not in concept_keys or item.to_concept_key not in concept_keys:
                raise ValueError("relation references an unknown concept")
            if item.from_concept_key == item.to_concept_key:
                raise ValueError("relation cannot be self-referential")
        for gap in self.gaps:
            if any(key not in source_keys for key in gap.source_keys):
                raise ValueError("gap references an unknown source")
        return self

    def canonical_payload(self) -> dict:
        return self.model_dump(by_alias=True, mode="json")

    def content_hash(self) -> str:
        encoded = json.dumps(
            self.canonical_payload(),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
        return hashlib.sha256(encoded).hexdigest()


def _json(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _stable_id(prefix: str, *parts: object) -> str:
    material = "\x1f".join(str(part) for part in parts)
    return f"{prefix}_{hashlib.sha256(material.encode()).hexdigest()[:32]}"


class CurriculumBaselineService:
    """Imports candidates and exposes only explicitly reviewed baselines to planning."""

    def __init__(self, db: Session):
        self.db = db

    @staticmethod
    def read_package(path: Path) -> CurriculumBaselinePackage:
        return CurriculumBaselinePackage.model_validate_json(path.read_text())

    @staticmethod
    def read_review(path: Path) -> CurriculumBaselineReview:
        return CurriculumBaselineReview.model_validate_json(path.read_text())

    @staticmethod
    def _relation_key(item: dict | RelationInput | BaselineRelationReviewDecision) -> tuple[str, str, str]:
        if isinstance(item, dict):
            return (
                item.get("fromConceptKey", ""),
                item.get("relationType", ""),
                item.get("toConceptKey", ""),
            )
        return (item.from_concept_key, item.relation_type, item.to_concept_key)

    def import_candidate(
        self,
        package: CurriculumBaselinePackage,
    ) -> CurriculumBaselineVersion:
        existing = self.db.scalar(
            select(CurriculumBaselineVersion).where(
                CurriculumBaselineVersion.baseline_key == package.baseline_key,
                CurriculumBaselineVersion.version == package.version,
            )
        )
        digest = package.content_hash()
        if existing:
            if existing.content_hash != digest:
                raise AppError(
                    "课程基准版本已经存在且内容不同，必须创建新版本",
                    code="CURRICULUM_BASELINE_VERSION_CONFLICT",
                    status=409,
                )
            return existing

        source_rows: dict[str, CurriculumSourceVersion] = {}
        for source in package.sources:
            row = self.db.scalar(
                select(CurriculumSourceVersion).where(
                    CurriculumSourceVersion.source_key == source.source_key,
                    CurriculumSourceVersion.version_label == source.version_label,
                )
            )
            if row and row.content_digest != source.content_digest:
                raise AppError(
                    "课程来源版本内容摘要冲突，必须声明新的来源版本",
                    code="CURRICULUM_SOURCE_VERSION_CONFLICT",
                    status=409,
                )
            if not row:
                row = CurriculumSourceVersion(
                    id=_stable_id("curriculum_source", source.source_key, source.version_label),
                    source_key=source.source_key,
                    source_type=source.source_type,
                    title=source.title,
                    authority=source.authority,
                    url=str(source.url),
                    version_label=source.version_label,
                    publication_date=source.publication_date,
                    applicability_json=_json(source.applicability),
                    retrieval_date=source.retrieval_date,
                    content_digest=source.content_digest,
                    verification_status=source.verification_status,
                    provenance_json=_json(source.provenance),
                )
                self.db.add(row)
                self.db.flush()
            source_rows[source.source_key] = row

        discipline = self.db.scalar(
            select(Discipline).where(Discipline.code == package.discipline.code)
        )
        if not discipline:
            discipline = Discipline(
                id=_stable_id("discipline", package.discipline.code),
                code=package.discipline.code,
                name=package.discipline.name,
                jurisdiction=package.discipline.jurisdiction,
            )
            self.db.add(discipline)
            self.db.flush()

        program_id = _stable_id(
            "program_version",
            package.program.institution,
            package.program.program_code,
            package.program.version_label,
        )
        program = self.db.get(ProgramVersion, program_id)
        if not program:
            program = ProgramVersion(
                id=program_id,
                discipline_id=discipline.id,
                source_version_id=source_rows[package.program.source_key].id,
                institution=package.program.institution,
                program_code=package.program.program_code,
                name=package.program.name,
                version_label=package.program.version_label,
                applicability_json=_json(package.program.applicability),
                review_status="candidate",
            )
            self.db.add(program)
            self.db.flush()

        course_id = _stable_id(
            "course_version",
            program.id,
            package.course.course_code,
            package.course.version_label,
        )
        course = self.db.get(CourseVersion, course_id)
        if not course:
            course = CourseVersion(
                id=course_id,
                program_version_id=program.id,
                course_code=package.course.course_code,
                title=package.course.title,
                version_label=package.course.version_label,
                course_type=package.course.course_type,
                credits_json=_json(package.course.credits),
                assessment_json=_json(package.course.assessment),
                aliases_json=_json(package.course.aliases),
                review_status="candidate",
            )
            self.db.add(course)
            self.db.flush()

        competency_ids: dict[str, str] = {}
        for item in package.competencies:
            competency_id = _stable_id("competency", package.baseline_key, item.key)
            competency_ids[item.key] = competency_id
            if not self.db.get(Competency, competency_id):
                self.db.add(
                    Competency(
                        id=competency_id,
                        namespace=package.baseline_key,
                        competency_key=item.key,
                        statement=item.statement,
                        competency_type=item.competency_type,
                        verification_modes_json=_json(item.verification_modes),
                        review_status="candidate",
                    )
                )

        graph = {
            "matchTerms": package.match_terms,
            "competencies": [
                {**item.model_dump(by_alias=True, mode="json"), "id": competency_ids[item.key]}
                for item in package.competencies
            ],
            "objectives": [item.model_dump(by_alias=True, mode="json") for item in package.objectives],
            "concepts": [item.model_dump(by_alias=True, mode="json") for item in package.concepts],
            "relations": [item.model_dump(by_alias=True, mode="json") for item in package.relations],
        }
        baseline = CurriculumBaselineVersion(
            id=_stable_id("curriculum_baseline", package.baseline_key, package.version),
            baseline_key=package.baseline_key,
            version=package.version,
            title=package.title,
            discipline_id=discipline.id,
            program_version_id=program.id,
            course_version_id=course.id,
            status="candidate",
            scope_json=_json(
                {
                    "institution": package.program.institution,
                    "program": package.program.name,
                    "programVersion": package.program.version_label,
                    "course": package.course.title,
                    "courseVersion": package.course.version_label,
                }
            ),
            graph_json=_json(graph),
            gaps_json=_json([item.model_dump(by_alias=True, mode="json") for item in package.gaps]),
            source_version_ids_json=_json([source_rows[item.source_key].id for item in package.sources]),
            content_hash=digest,
            review_json="{}",
        )
        self.db.add(baseline)
        self.db.commit()
        return baseline

    def apply_review(
        self,
        baseline_id: str,
        review: CurriculumBaselineReview,
    ) -> CurriculumBaselineVersion:
        baseline = self.db.get(CurriculumBaselineVersion, baseline_id)
        if not baseline:
            raise AppError("课程基准不存在", code="CURRICULUM_BASELINE_NOT_FOUND", status=404)
        if baseline.status != "candidate":
            raise AppError(
                "只有候选课程基准可以提交审核决定",
                code="CURRICULUM_BASELINE_REVIEW_STATE_INVALID",
                status=409,
            )
        if (
            review.baseline_key != baseline.baseline_key
            or review.baseline_version != baseline.version
            or review.baseline_content_hash != baseline.content_hash
        ):
            raise AppError(
                "审核清单与冻结课程基准版本不一致",
                code="CURRICULUM_BASELINE_REVIEW_VERSION_MISMATCH",
                status=409,
            )

        graph = json.loads(baseline.graph_json)
        gaps = json.loads(baseline.gaps_json or "[]")
        source_ids = json.loads(baseline.source_version_ids_json or "[]")
        source_rows = [self.db.get(CurriculumSourceVersion, item) for item in source_ids]
        source_keys = {item.source_key for item in source_rows if item}
        reviewed_source_keys = [item.source_key for item in review.sources]
        if len(reviewed_source_keys) != len(set(reviewed_source_keys)) or set(
            reviewed_source_keys
        ) != source_keys:
            raise AppError(
                "审核清单必须逐一覆盖冻结基准的全部来源",
                code="CURRICULUM_BASELINE_SOURCE_REVIEW_INCOMPLETE",
                status=409,
            )

        relation_keys = {self._relation_key(item) for item in graph.get("relations", [])}
        reviewed_relation_keys = [self._relation_key(item) for item in review.relations]
        if len(reviewed_relation_keys) != len(set(reviewed_relation_keys)) or set(
            reviewed_relation_keys
        ) != relation_keys:
            raise AppError(
                "审核清单必须逐一覆盖冻结基准的全部候选关系",
                code="CURRICULUM_BASELINE_RELATION_REVIEW_INCOMPLETE",
                status=409,
            )

        gap_codes = {item.get("code", "") for item in gaps}
        reviewed_gap_codes = [item.code for item in review.gaps]
        if len(reviewed_gap_codes) != len(set(reviewed_gap_codes)) or set(
            reviewed_gap_codes
        ) != gap_codes:
            raise AppError(
                "审核清单必须逐一处置冻结基准的全部显式缺口",
                code="CURRICULUM_BASELINE_GAP_REVIEW_INCOMPLETE",
                status=409,
            )

        course = self.db.get(CourseVersion, baseline.course_version_id)
        assessment = json.loads(course.assessment_json or "{}") if course else {}
        evidence_modes = set(assessment.get("requiredEvidenceModes", []))
        if "code_task" in evidence_modes and not review.platform_code_assessment:
            raise AppError(
                "课程要求代码能力证据，但审核清单没有平台代码评分量规",
                code="CURRICULUM_BASELINE_CODE_RUBRIC_REQUIRED",
                status=409,
            )

        baseline.review_json = _json(
            {
                **review.model_dump(by_alias=True, mode="json"),
                "reviewedAt": datetime.now(timezone.utc).isoformat(),
            }
        )
        for decision in review.sources:
            if decision.decision == "reviewed":
                row = next(
                    (item for item in source_rows if item and item.source_key == decision.source_key),
                    None,
                )
                if row:
                    row.verification_status = "reviewed"
        self.db.commit()
        return baseline

    def publish(
        self,
        baseline_id: str,
        *,
        reviewer_id: str,
        review_note: str,
    ) -> CurriculumBaselineVersion:
        baseline = self.db.get(CurriculumBaselineVersion, baseline_id)
        if not baseline:
            raise AppError("课程基准不存在", code="CURRICULUM_BASELINE_NOT_FOUND", status=404)
        if baseline.status == "published":
            return baseline
        review = json.loads(baseline.review_json or "{}")
        has_review_manifest = review.get("schemaVersion") == "curriculum_baseline_review_v1"
        if has_review_manifest:
            if review.get("finalDecision") != "approved" or review.get("reviewerId") != reviewer_id:
                raise AppError(
                    "课程基准没有匹配发布者的批准审核决定",
                    code="CURRICULUM_BASELINE_REVIEW_APPROVAL_REQUIRED",
                    status=409,
                )
            if any(item.get("decision") != "reviewed" for item in review.get("sources", [])):
                raise AppError(
                    "课程基准来源审核存在拒绝项",
                    code="CURRICULUM_BASELINE_SOURCE_REVIEW_REQUIRED",
                    status=409,
                )
            baseline_blockers = [
                item.get("code", "")
                for item in review.get("gaps", [])
                if "baseline_publication" in item.get("remainingBlockingStages", [])
            ]
        else:
            gaps = json.loads(baseline.gaps_json or "[]")
            baseline_blockers = [
                item.get("code", "")
                for item in gaps
                if item.get("severity") == "blocking"
            ]
        if baseline_blockers:
            raise AppError(
                f"课程基准仍有阻断发布的缺口：{', '.join(sorted(baseline_blockers))}",
                code="CURRICULUM_BASELINE_BLOCKING_GAP",
                status=409,
            )
        graph = json.loads(baseline.graph_json)
        if not has_review_manifest and any(
            item.get("reviewStatus") != "reviewed" for item in graph.get("relations", [])
        ):
            raise AppError(
                "课程基准中的知识关系尚未全部完成人工复核",
                code="CURRICULUM_BASELINE_RELATION_REVIEW_REQUIRED",
                status=409,
            )
        source_ids = json.loads(baseline.source_version_ids_json or "[]")
        sources = [self.db.get(CurriculumSourceVersion, source_id) for source_id in source_ids]
        if any(not source or source.verification_status != "reviewed" for source in sources):
            raise AppError(
                "课程基准来源尚未全部完成人工复核",
                code="CURRICULUM_BASELINE_SOURCE_REVIEW_REQUIRED",
                status=409,
            )
        reviewed_at = datetime.now(timezone.utc)
        baseline.status = "published"
        if has_review_manifest:
            review["publicationNote"] = review_note
            review["publishedAt"] = reviewed_at.isoformat()
            baseline.review_json = _json(review)
        else:
            baseline.review_json = _json(
                {
                    "reviewerId": reviewer_id,
                    "reviewNote": review_note,
                    "reviewedAt": reviewed_at.isoformat(),
                }
            )
        baseline.published_at = reviewed_at
        program = self.db.get(ProgramVersion, baseline.program_version_id)
        course = self.db.get(CourseVersion, baseline.course_version_id)
        if program:
            program.review_status = "reviewed"
        if course:
            course.review_status = "reviewed"
        competency_ids = [item["id"] for item in graph.get("competencies", [])]
        for competency_id in competency_ids:
            competency = self.db.get(Competency, competency_id)
            if competency:
                competency.review_status = "reviewed"
        self.db.commit()
        return baseline

    def select_for_plan(self, *, shelf, plan_input: dict) -> CurriculumBaselineVersion | None:
        haystack = " ".join(
            str(value)
            for value in (
                shelf.domain,
                shelf.specialty,
                plan_input.get("topic", ""),
                plan_input.get("purpose", ""),
                plan_input.get("details", ""),
            )
        ).lower()
        candidates = self.db.scalars(
            select(CurriculumBaselineVersion).where(
                CurriculumBaselineVersion.status == "published"
            )
        ).all()
        matches = []
        for baseline in candidates:
            graph = json.loads(baseline.graph_json)
            terms = [str(term).lower() for term in graph.get("matchTerms", [])]
            score = sum(1 for term in terms if term and term in haystack)
            if score:
                matches.append((score, baseline.version, baseline))
        if not matches:
            return None
        matches.sort(key=lambda item: (item[0], item[1]), reverse=True)
        return matches[0][2]

    def planning_context(self, baseline: CurriculumBaselineVersion) -> dict:
        if baseline.status != "published":
            raise AppError(
                "未发布的课程基准不能进入正式规划",
                code="CURRICULUM_BASELINE_NOT_PUBLISHED",
                status=409,
            )
        graph = json.loads(baseline.graph_json)
        course = self.db.get(CourseVersion, baseline.course_version_id)
        program = self.db.get(ProgramVersion, baseline.program_version_id)
        review = json.loads(baseline.review_json or "{}")
        relations = graph.get("relations", [])
        if review.get("schemaVersion") == "curriculum_baseline_review_v1":
            accepted_relation_keys = {
                self._relation_key(item)
                for item in review.get("relations", [])
                if item.get("decision") == "reviewed"
            }
            relations = [
                item
                for item in relations
                if self._relation_key(item) in accepted_relation_keys
            ]
        release = self.db.scalar(
            select(KnowledgeGraphRelease)
            .where(
                KnowledgeGraphRelease.baseline_version_id == baseline.id,
                KnowledgeGraphRelease.status == "published",
            )
            .order_by(KnowledgeGraphRelease.version.desc())
        )
        published_identities = []
        if release:
            identity_rows = self.db.execute(
                select(ConceptRevision, Concept, LearningObjective)
                .select_from(ConceptObjectiveBinding)
                .join(
                    ConceptRevision,
                    ConceptRevision.id
                    == ConceptObjectiveBinding.concept_revision_id,
                )
                .join(Concept, Concept.id == ConceptRevision.concept_id)
                .join(
                    LearningObjective,
                    LearningObjective.id
                    == ConceptObjectiveBinding.learning_objective_id,
                )
                .where(ConceptObjectiveBinding.release_id == release.id)
                .order_by(Concept.concept_key, LearningObjective.objective_key)
            ).all()
            published_identities = [
                {
                    "releaseId": release.id,
                    "conceptRevisionId": revision.id,
                    "conceptKey": concept.concept_key,
                    "conceptLabel": revision.label,
                    "conceptDefinition": revision.definition,
                    "objectiveId": objective.id,
                    "objectiveKey": objective.objective_key,
                }
                for revision, concept, objective in identity_rows
            ]
        return {
            "baselineVersionId": baseline.id,
            "baselineKey": baseline.baseline_key,
            "version": baseline.version,
            "title": baseline.title,
            "institution": program.institution if program else "",
            "programVersion": program.version_label if program else "",
            "course": {
                "code": course.course_code if course else "",
                "title": course.title if course else "",
                "version": course.version_label if course else "",
                "assessment": json.loads(course.assessment_json) if course else {},
                "platformCodeAssessment": review.get("platformCodeAssessment"),
            },
            "competencies": graph.get("competencies", []),
            "objectives": graph.get("objectives", []),
            "concepts": graph.get("concepts", []),
            "relations": relations,
            "publishedKnowledgeIdentities": published_identities,
            "coveragePolicy": {
                "requiredObjectiveKeys": [
                    item["key"] for item in graph.get("objectives", []) if item.get("required")
                ],
                "rule": "every required objective must be bound to at least one chapter; no minimum concept or section count",
            },
        }

    def validate_plan_coverage(self, baseline: CurriculumBaselineVersion, generated) -> None:
        graph = json.loads(baseline.graph_json)
        known = {item["key"] for item in graph.get("objectives", [])}
        required = {
            item["key"] for item in graph.get("objectives", []) if item.get("required")
        }
        bound = {
            objective_key
            for book in generated.books
            for chapter in book.chapters
            for objective_key in chapter.baseline_objective_ids
        }
        unknown = bound - known
        missing = required - bound
        if unknown:
            raise AppError(
                f"课程规划引用了未知基准目标：{', '.join(sorted(unknown))}",
                code="CURRICULUM_BASELINE_OBJECTIVE_UNKNOWN",
                status=502,
            )
        if missing:
            raise AppError(
                f"课程规划未覆盖必需基准目标：{', '.join(sorted(missing))}",
                code="CURRICULUM_BASELINE_COVERAGE_INCOMPLETE",
                status=502,
            )
        release = self.db.scalar(
            select(KnowledgeGraphRelease)
            .where(
                KnowledgeGraphRelease.baseline_version_id == baseline.id,
                KnowledgeGraphRelease.status == "published",
            )
            .order_by(KnowledgeGraphRelease.version.desc())
        )
        if not release:
            if any(
                chapter.baseline_concept_ids
                for book in generated.books
                for chapter in book.chapters
            ):
                raise AppError(
                    "课程规划引用了尚未发布的知识概念",
                    code="CURRICULUM_KNOWLEDGE_IDENTITY_NOT_PUBLISHED",
                    status=502,
                )
            return
        rows = self.db.execute(
            select(Concept.concept_key, LearningObjective.objective_key)
            .select_from(ConceptObjectiveBinding)
            .join(
                ConceptRevision,
                ConceptRevision.id == ConceptObjectiveBinding.concept_revision_id,
            )
            .join(Concept, Concept.id == ConceptRevision.concept_id)
            .join(
                LearningObjective,
                LearningObjective.id == ConceptObjectiveBinding.learning_objective_id,
            )
            .where(ConceptObjectiveBinding.release_id == release.id)
        ).all()
        allowed_pairs = set(rows)
        known_concepts = {concept_key for concept_key, _ in allowed_pairs}
        planned_concepts = set()
        invalid_pairs = []
        for book in generated.books:
            for chapter in book.chapters:
                chapter_objectives = set(chapter.baseline_objective_ids)
                for concept_key in chapter.baseline_concept_ids:
                    planned_concepts.add(concept_key)
                    if concept_key not in known_concepts or not any(
                        (concept_key, objective_key) in allowed_pairs
                        for objective_key in chapter_objectives
                    ):
                        invalid_pairs.append(
                            {
                                "conceptKey": concept_key,
                                "objectiveKeys": sorted(chapter_objectives),
                            }
                        )
        if invalid_pairs:
            raise AppError(
                "课程规划引用了目标范围外或未发布的知识概念",
                code="CURRICULUM_KNOWLEDGE_IDENTITY_OUT_OF_SCOPE",
                status=502,
                details={"identities": invalid_pairs},
            )
        missing_concepts = sorted(known_concepts - planned_concepts)
        if missing_concepts:
            raise AppError(
                "课程规划未覆盖已发布知识图中的必需概念",
                code="CURRICULUM_KNOWLEDGE_CONCEPT_COVERAGE_INCOMPLETE",
                status=502,
                details={"conceptKeys": missing_concepts},
            )
        first_book_concepts = {
            concept_key
            for chapter in generated.books[0].chapters
            for concept_key in chapter.baseline_concept_ids
        }
        missing_from_leading_book = sorted(known_concepts - first_book_concepts)
        if missing_from_leading_book:
            raise AppError(
                "已发布知识概念必须形成第一本书的可生成前导路径",
                code="CURRICULUM_KNOWLEDGE_CONCEPT_ROUTE_NOT_LEADING",
                status=502,
                details={"conceptKeys": missing_from_leading_book},
            )
        saw_unpublished_scope = False
        for chapter in generated.books[0].chapters:
            if chapter.baseline_concept_ids:
                if saw_unpublished_scope:
                    raise AppError(
                        "第一本书的已发布知识章节不能排在未发布知识范围之后",
                        code="CURRICULUM_KNOWLEDGE_CONCEPT_ROUTE_NOT_LEADING",
                        status=502,
                    )
            else:
                saw_unpublished_scope = True

    def bind_series(
        self,
        *,
        series_id: str,
        baseline: CurriculumBaselineVersion,
        plan_input: dict,
    ) -> SeriesCurriculumBaselineBinding:
        row = SeriesCurriculumBaselineBinding(
            id=_stable_id("series_curriculum_baseline", series_id, baseline.id),
            series_id=series_id,
            baseline_version_id=baseline.id,
            selection_reason="published baseline matched confirmed shelf and plan terms",
            selection_snapshot_json=_json(
                {
                    "shelfId": plan_input.get("shelf_id") or plan_input.get("shelfId"),
                    "topic": plan_input.get("topic", ""),
                    "purpose": plan_input.get("purpose", ""),
                    "baselineContentHash": baseline.content_hash,
                }
            ),
        )
        self.db.add(row)
        self.db.flush()
        return row

    def bind_chapter_objectives(
        self,
        *,
        chapter_id: str,
        baseline: CurriculumBaselineVersion,
        objective_keys: list[str],
    ) -> None:
        for objective_key in objective_keys:
            self.db.add(
                ChapterCurriculumObjectiveBinding(
                    id=_stable_id(
                        "chapter_curriculum_objective",
                        chapter_id,
                        baseline.id,
                        objective_key,
                    ),
                    chapter_id=chapter_id,
                    baseline_version_id=baseline.id,
                    objective_key=objective_key,
                    coverage_role="teaches",
                )
            )
