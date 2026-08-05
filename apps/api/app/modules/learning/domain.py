from dataclasses import dataclass


PROGRESSION_RULE_VERSION = "progression_v1"


@dataclass(frozen=True)
class ProgressionSnapshot:
    section_id: str
    chapter_id: str
    book_id: str
    next_section_id: str | None = None
    next_chapter_id: str | None = None
    next_chapter_first_section_id: str | None = None
    next_book_id: str | None = None
    next_book_first_chapter_id: str | None = None
    next_book_first_section_id: str | None = None
    practice_id: str | None = None
    capstone_id: str | None = None


@dataclass(frozen=True)
class ProgressionDecision:
    completed_section_id: str
    unlocked_section_id: str | None = None
    completed_chapter_id: str | None = None
    unlocked_chapter_id: str | None = None
    available_practice_id: str | None = None
    completed_book_id: str | None = None
    available_capstone_id: str | None = None
    unlocked_book_id: str | None = None


class ProgressionPolicy:
    """Pure learning-path rule. Persistence only applies the returned decision."""

    def after_quiz_passed(self, snapshot: ProgressionSnapshot) -> ProgressionDecision:
        if snapshot.next_section_id:
            return ProgressionDecision(
                completed_section_id=snapshot.section_id,
                unlocked_section_id=snapshot.next_section_id,
            )
        if snapshot.next_chapter_id:
            return ProgressionDecision(
                completed_section_id=snapshot.section_id,
                completed_chapter_id=snapshot.chapter_id,
                unlocked_chapter_id=snapshot.next_chapter_id,
                unlocked_section_id=snapshot.next_chapter_first_section_id,
                available_practice_id=snapshot.practice_id,
            )
        return ProgressionDecision(
            completed_section_id=snapshot.section_id,
            completed_chapter_id=snapshot.chapter_id,
            available_practice_id=snapshot.practice_id,
            completed_book_id=snapshot.book_id,
            available_capstone_id=snapshot.capstone_id,
            unlocked_book_id=snapshot.next_book_id,
            unlocked_chapter_id=snapshot.next_book_first_chapter_id,
            unlocked_section_id=snapshot.next_book_first_section_id,
        )
