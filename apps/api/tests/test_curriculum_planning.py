import pytest
from pydantic import ValidationError

from app.ai.contracts import GeneratedChapter, GeneratedSectionOutline
from app.modules.curriculum.policy import CHAPTER_SECTION_POLICY


def _sections(count: int) -> list[GeneratedSectionOutline]:
    return [
        GeneratedSectionOutline(
            title=f"知识点 {position}",
            question=f"核心问题 {position} 是什么？",
            objectives=[f"验证目标 {position}"],
        )
        for position in range(1, count + 1)
    ]


@pytest.mark.parametrize("count", [2, 3, 5, 6, 12])
def test_chapter_section_count_accepts_soft_range(count):
    chapter = GeneratedChapter(sections=_sections(count))
    assert len(chapter.sections) == count


@pytest.mark.parametrize("count", [1, 13])
def test_chapter_section_count_rejects_only_technical_anomalies(count):
    with pytest.raises(ValidationError):
        GeneratedChapter(sections=_sections(count))


@pytest.mark.parametrize(
    ("count", "level"),
    [(2, "light"), (3, "typical"), (5, "typical"), (6, "extended")],
)
def test_workload_hint_does_not_turn_typical_range_into_gate(count, level):
    hint = CHAPTER_SECTION_POLICY.workload(count)
    assert hint["level"] == level
    assert hint["typicalRange"] == [3, 5]
    assert hint["technicalRange"] == [2, 12]


def test_historical_partial_chapter_is_visible_but_explicitly_anomalous():
    hint = CHAPTER_SECTION_POLICY.workload(1)
    assert hint["level"] == "anomalous"
    assert "显式检查" in hint["message"]
