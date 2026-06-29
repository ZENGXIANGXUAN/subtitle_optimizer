from core.batch_glossary import (
    GlossaryFileFailure,
    GlossaryCandidate,
    approved_candidates_to_text,
    merge_batch_glossary_candidates,
    unresolved_conflict_sources,
)


def test_merge_candidates_combines_same_source_target_and_counts_files():
    result = merge_batch_glossary_candidates(
        [
            ("file_a.srt", "order flow->订单流\nliquidity sweep->流动性扫单"),
            ("file_b.srt", "order flow->订单流"),
        ],
        fixed_glossary="",
    )

    assert result.failures == []
    assert result.skipped_existing == []
    assert result.candidates == [
        GlossaryCandidate(
            source="order flow",
            target="订单流",
            files=("file_a.srt", "file_b.srt"),
            status="新词",
        ),
        GlossaryCandidate(
            source="liquidity sweep",
            target="流动性扫单",
            files=("file_a.srt",),
            status="新词",
        ),
    ]


def test_merge_candidates_keeps_differently_cased_sources_separate():
    result = merge_batch_glossary_candidates(
        [
            ("file_a.srt", "US->美国"),
            ("file_b.srt", "us->美国"),
        ],
        fixed_glossary="",
    )

    assert result.candidates == [
        GlossaryCandidate(
            source="US",
            target="美国",
            files=("file_a.srt",),
            status="新词",
        ),
        GlossaryCandidate(
            source="us",
            target="美国",
            files=("file_b.srt",),
            status="新词",
        ),
    ]


def test_merge_candidates_keeps_conflict_variants_next_to_each_other():
    result = merge_batch_glossary_candidates(
        [
            ("file_a.srt", "order flow->订单流"),
            ("file_b.srt", "order flow->委托流"),
            ("file_c.srt", "delta->德尔塔"),
        ],
        fixed_glossary="",
    )

    assert [candidate.source for candidate in result.candidates] == [
        "order flow",
        "order flow",
        "delta",
    ]
    assert [candidate.target for candidate in result.candidates[:2]] == ["订单流", "委托流"]
    assert [candidate.status for candidate in result.candidates[:2]] == ["译法冲突", "译法冲突"]
    assert result.candidates[2].status == "新词"


def test_merge_candidates_filters_sources_already_in_fixed_glossary():
    result = merge_batch_glossary_candidates(
        [
            ("file_a.srt", "order flow->另一个译法\nliquidity sweep->流动性扫单"),
        ],
        fixed_glossary="ORDER FLOW->固定译法",
    )

    assert result.candidates == [
        GlossaryCandidate(
            source="liquidity sweep",
            target="流动性扫单",
            files=("file_a.srt",),
            status="新词",
        )
    ]
    assert result.skipped_existing == ["order flow"]


def test_merge_candidates_parses_unicode_arrow_separator():
    result = merge_batch_glossary_candidates(
        [
            ("file_a.srt", "gamma→伽马"),
        ],
        fixed_glossary="",
    )

    assert result.candidates == [
        GlossaryCandidate(
            source="gamma",
            target="伽马",
            files=("file_a.srt",),
            status="新词",
        )
    ]


def test_merge_candidates_preserves_file_failures():
    failure = GlossaryFileFailure("bad.srt", "无法解析字幕文件")

    result = merge_batch_glossary_candidates(
        [("file_a.srt", "gamma->伽马")],
        fixed_glossary="",
        failures=[failure],
    )

    assert result.failures == [failure]


def test_unresolved_conflict_sources_detects_remaining_multi_target_sources():
    candidates = [
        GlossaryCandidate("order flow", "订单流", ("a.srt",), "译法冲突"),
        GlossaryCandidate("order flow", "委托流", ("b.srt",), "译法冲突"),
        GlossaryCandidate("delta", "德尔塔", ("c.srt",), "新词"),
    ]

    assert unresolved_conflict_sources(candidates) == ["order flow"]


def test_approved_candidates_to_text_ignores_blank_rows():
    candidates = [
        GlossaryCandidate("order flow", "订单流", ("a.srt",), "新词"),
        GlossaryCandidate("", "空", ("b.srt",), "新词"),
        GlossaryCandidate("empty", "", ("c.srt",), "新词"),
    ]

    assert approved_candidates_to_text(candidates) == "order flow->订单流"
