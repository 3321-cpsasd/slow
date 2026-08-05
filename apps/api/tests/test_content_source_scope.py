import pytest
from pydantic import ValidationError

from app.ai.contracts import ContentBlock, GeneratedContent, Source


def _source():
    return Source(
        title="规范",
        url="https://example.com/spec",
        kind="official",
        version="v1",
    )


def _block(role: str, *, sourced: bool):
    return ContentBlock(
        kind="text",
        role=role,
        heading=role,
        content=f"{role} 内容。",
        source_indexes=[0] if sourced else [],
    )


def test_pedagogical_synthesis_does_not_require_fake_sentence_level_sources():
    content = GeneratedContent(
        confidence="medium",
        sources=[_source()],
        blocks=[
            _block("conclusion", sourced=True),
            _block("mechanism", sourced=False),
            _block("example", sourced=False),
            _block("boundary", sourced=True),
            _block("practice", sourced=False),
        ],
    )

    assert content.blocks[1].source_indexes == []
    assert content.blocks[2].source_indexes == []


def test_generated_content_rejects_a_block_ending_mid_sentence():
    blocks = [
        _block("conclusion", sourced=True),
        _block("mechanism", sourced=False),
        _block("example", sourced=False),
        _block("boundary", sourced=True),
        _block("practice", sourced=False),
    ]
    blocks[2].content = "一位数据分析师使用散点图矩阵将"

    with pytest.raises(ValidationError, match="content block ends mid-sentence"):
        GeneratedContent(
            confidence="medium",
            sources=[_source()],
            blocks=blocks,
        )


@pytest.mark.parametrize("kind", ["code", "formula"])
def test_generated_content_allows_code_or_formula_without_sentence_punctuation(kind):
    blocks = [
        _block("conclusion", sourced=True),
        _block("mechanism", sourced=False),
        _block("example", sourced=False),
        _block("boundary", sourced=True),
        _block("practice", sourced=False),
    ]
    blocks[2].kind = kind
    blocks[2].content = "x = y + 1"

    content = GeneratedContent(
        confidence="medium",
        sources=[_source()],
        blocks=blocks,
    )

    assert content.blocks[2].content == "x = y + 1"


@pytest.mark.parametrize("role", ["conclusion", "boundary"])
def test_strict_claim_roles_still_require_explicit_sources(role):
    blocks = [
        _block("conclusion", sourced=True),
        _block("mechanism", sourced=False),
        _block("example", sourced=False),
        _block("boundary", sourced=True),
        _block("practice", sourced=False),
    ]
    blocks[0 if role == "conclusion" else 3] = _block(role, sourced=False)

    with pytest.raises(ValidationError):
        GeneratedContent(
            confidence="low",
            sources=[_source()],
            blocks=blocks,
        )
