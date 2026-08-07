from app.modules.learning.domain import ProgressionPolicy, ProgressionSnapshot


def test_progression_unlocks_next_section_without_completing_parent():
    decision = ProgressionPolicy().after_quiz_passed(
        ProgressionSnapshot(
            section_id="section-1",
            chapter_id="chapter-1",
            book_id="book-1",
            next_section_id="section-2",
        )
    )
    assert decision.completed_section_id == "section-1"
    assert decision.unlocked_section_id == "section-2"
    assert decision.completed_chapter_id is None


def test_progression_completes_chapter_and_unlocks_pregenerated_section():
    decision = ProgressionPolicy().after_quiz_passed(
        ProgressionSnapshot(
            section_id="section-last",
            chapter_id="chapter-1",
            book_id="book-1",
            next_chapter_id="chapter-2",
            next_chapter_first_section_id="section-pregenerated",
            practice_id="practice-1",
        )
    )
    assert decision.completed_chapter_id == "chapter-1"
    assert decision.unlocked_chapter_id == "chapter-2"
    assert decision.unlocked_section_id == "section-pregenerated"
    assert decision.available_practice_id == "practice-1"


def test_progression_completes_book_and_unlocks_next_active_book():
    decision = ProgressionPolicy().after_quiz_passed(
        ProgressionSnapshot(
            section_id="section-last",
            chapter_id="chapter-last",
            book_id="book-1",
            next_book_id="book-3",
            next_book_first_chapter_id="chapter-3-1",
            next_book_first_section_id="section-3-1-1",
            practice_id="practice-last",
            capstone_id="capstone-1",
        )
    )
    assert decision.completed_book_id == "book-1"
    assert decision.available_capstone_id == "capstone-1"
    assert decision.unlocked_book_id == "book-3"
    assert decision.unlocked_chapter_id == "chapter-3-1"
    assert decision.unlocked_section_id == "section-3-1-1"


def test_progression_keeps_next_book_locked_until_outline_is_confirmed():
    decision = ProgressionPolicy().after_quiz_passed(
        ProgressionSnapshot(
            section_id="section-last",
            chapter_id="chapter-last",
            book_id="book-1",
            next_book_id="book-2",
            next_book_outline_status="draft",
            next_book_first_chapter_id="chapter-2-1",
        )
    )
    assert decision.completed_book_id == "book-1"
    assert decision.unlocked_book_id is None
    assert decision.unlocked_chapter_id is None
