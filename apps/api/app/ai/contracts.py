from typing import Literal
from urllib.parse import urlparse
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class PlanChapter(StrictModel):
    title: str
    objective: str


class PlanBook(StrictModel):
    title: str
    topic: str
    description: str
    estimated_minutes: int = Field(ge=120, le=4000)
    chapters: list[PlanChapter] = Field(min_length=2, max_length=10)


class PlanMilestoneCriterion(StrictModel):
    statement: str
    book_position: int = Field(ge=1, le=6)
    chapter_position: int = Field(ge=1, le=10)


class PlanMilestone(StrictModel):
    title: str
    outcome: str
    criteria: list[PlanMilestoneCriterion] = Field(min_length=1, max_length=6)


class GeneratedPlan(StrictModel):
    series_title: str
    rationale: str
    assumptions: list[str] = Field(max_length=6)
    confidence: Literal["high", "medium", "low"]
    books: list[PlanBook] = Field(min_length=1, max_length=6)
    milestones: list[PlanMilestone] = Field(min_length=3, max_length=5)

    @model_validator(mode="after")
    def valid_milestone_references(self):
        for milestone in self.milestones:
            for criterion in milestone.criteria:
                if criterion.book_position > len(self.books):
                    raise ValueError("milestone references a missing book")
                book = self.books[criterion.book_position - 1]
                if criterion.chapter_position > len(book.chapters):
                    raise ValueError("milestone references a missing chapter")
        return self


class GeneratedSectionOutline(StrictModel):
    title: str
    question: str
    objectives: list[str] = Field(min_length=1, max_length=4)


class GeneratedChapter(StrictModel):
    sections: list[GeneratedSectionOutline] = Field(min_length=3, max_length=5)


class Source(StrictModel):
    title: str
    url: str
    kind: Literal["official", "source_code", "paper", "community"]
    version: str = Field(min_length=1, max_length=160)

    @model_validator(mode="after")
    def valid_reference(self):
        parsed = urlparse(self.url)
        if parsed.scheme != "https" or not parsed.netloc:
            raise ValueError("source URL must be an absolute HTTPS URL")
        if self.kind == "source_code":
            parts = parsed.path.strip("/").split("/")
            if parsed.netloc.lower() != "github.com" or len(parts) < 5 or parts[2] != "blob":
                raise ValueError("source_code URL must pin a GitHub blob reference")
            ref = parts[3]
            if ref.lower() in {"main", "master", "head", "latest"}:
                raise ValueError("source_code URL must use an immutable tag or commit")
            if self.version != ref:
                raise ValueError("source_code version must match URL tag or commit")
        return self


class ContentBlock(StrictModel):
    id: str = Field(default="", max_length=120)
    version: int = Field(default=1, ge=1)
    kind: Literal["text", "code", "formula", "table", "diagram"]
    role: Literal["conclusion", "mechanism", "example", "boundary", "practice", "transition"]
    heading: str
    content: str
    source_indexes: list[int] = Field(default_factory=list)


class ChoiceQuestion(StrictModel):
    prompt: str
    options: list[str] = Field(min_length=3, max_length=6)
    correct: list[int] = Field(min_length=1)
    core: bool
    objective: str
    explanation: str
    difficulty: Literal["standard"] = "standard"

    @model_validator(mode="after")
    def valid_indexes(self):
        if any(index < 0 or index >= len(self.options) for index in self.correct):
            raise ValueError("correct index out of range")
        return self


class GeneratedContent(StrictModel):
    confidence: Literal["high", "medium", "low"]
    sources: list[Source] = Field(min_length=1, max_length=12)
    blocks: list[ContentBlock] = Field(min_length=5, max_length=12)

    @model_validator(mode="after")
    def valid_source_coverage(self):
        # Sources are mandatory for the small set of blocks that state a core
        # conclusion or a boundary. Explanations, examples, practice prompts,
        # and transitions may be pedagogical synthesis and must not be forced
        # to pretend that a URL supports every sentence.
        strict_source_roles = {"conclusion", "boundary"}
        for block in self.blocks:
            if block.role in strict_source_roles and not block.source_indexes:
                raise ValueError(
                    "conclusion and boundary blocks need an explicit source"
                )
            if any(index < 0 or index >= len(self.sources) for index in block.source_indexes):
                raise ValueError("content block source index out of range")
        return self


class GeneratedQuiz(StrictModel):
    # Initial quizzes still request 4-5 items. A remediation quiz may contain a
    # single failed target and must not be padded with already-passed targets.
    questions: list[ChoiceQuestion] = Field(min_length=1, max_length=5)


class GeneratedLesson(GeneratedContent):
    questions: list[ChoiceQuestion] = Field(min_length=4, max_length=5)


class GeneratedRemediationContent(GeneratedContent):
    blocks: list[ContentBlock] = Field(min_length=1, max_length=5)

    @model_validator(mode="after")
    def valid_remediation_completeness(self):
        sentence_endings = tuple("。！？.!?；;：:）)]】」』”’\"'|")
        for block in self.blocks:
            heading = block.heading.strip()
            content = block.content.strip()
            if len(heading) < 4:
                raise ValueError("remediation block heading is too short")
            table_rows = [
                line.strip()
                for line in content.splitlines()
                if line.strip().startswith("|")
            ]
            if any(not line.endswith("|") for line in table_rows):
                raise ValueError("remediation markdown table is incomplete")
            if len(content) < 80:
                raise ValueError("remediation block content is too short")
            if content == heading:
                raise ValueError("remediation block repeats its heading")
            if block.kind not in {"code", "formula"} and not content.endswith(
                sentence_endings
            ):
                raise ValueError("remediation block ends mid-sentence")
        return self


class GeneratedRemediationLesson(GeneratedRemediationContent):
    questions: list[ChoiceQuestion] = Field(min_length=1, max_length=5)


class SourceRepairBlock(StrictModel):
    block_index: int = Field(ge=0, le=11)
    heading: str
    content: str


class SourceRepairReplacement(StrictModel):
    source_index: int = Field(ge=0, le=11)
    source: Source
    blocks: list[SourceRepairBlock] = Field(default_factory=list, max_length=12)


class GeneratedSourceRepair(StrictModel):
    replacements: list[SourceRepairReplacement] = Field(min_length=1, max_length=12)


class ClassifiedAnswer(StrictModel):
    relation: Literal["follow_up", "new_question"]
    thread_id: str
    answer: str
    thread_summary: str = ""


class GeneratedNote(StrictModel):
    solved_question: str
    core_mechanism: list[str]
    personal_gaps: list[str]
    boundaries: list[str]
    practice_checks: list[str]
    sources: list[str]
    unresolved: list[str]


class AskMeTurn(StrictModel):
    dimension: Literal["mechanism", "boundary", "transfer"]
    prompt: str
    evaluation: Literal["strong", "partial", "weak", "not_evaluated"]
    rationale: str = ""


class ReplannedChapter(StrictModel):
    title: str
    objective: str


class ReplannedBook(StrictModel):
    rationale: str
    chapters: list[ReplannedChapter] = Field(min_length=1, max_length=10)


class EvaluationQuizAnswers(StrictModel):
    answers: list[list[int]]


class ReviewerFinding(StrictModel):
    gate_id: str
    severity: Literal["critical", "high", "medium", "low"]
    finding: str
    evidence: str


class EvaluationReview(StrictModel):
    verdict: Literal["PASS", "FAIL"]
    findings: list[ReviewerFinding]
    content_quality: int = Field(ge=0, le=100)
    source_support: int = Field(ge=0, le=100)
    note_fidelity: int = Field(ge=0, le=100)
    summary: str
