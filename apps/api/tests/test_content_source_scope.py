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


def test_rights_grounded_content_may_bind_only_the_blocks_supported_by_assets():
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


def test_model_only_content_does_not_require_or_invent_sources():
    blocks = [
        _block("conclusion", sourced=False),
        _block("mechanism", sourced=False),
        _block("example", sourced=False),
        _block("boundary", sourced=False),
        _block("practice", sourced=False),
    ]

    content = GeneratedContent(confidence="high", blocks=blocks)

    assert content.sources == []
    assert all(block.source_indexes == [] for block in content.blocks)
