from core.batch_glossary import (
    BatchGlossarySummary,
    GlossaryFileFailure,
    GlossaryCandidate,
    approved_candidates_to_text,
    batch_glossary_review_reasoning_options,
    batch_glossary_unifier_candidate_limit,
    batch_glossary_reasoning_options,
    batch_glossary_unifier_timeout_seconds,
    filter_human_review_glossary_candidates,
    merge_batch_glossary_candidates,
    relevant_fixed_glossary,
    unresolved_conflict_sources,
)


def test_batch_glossary_unifier_uses_supported_high_reasoning_by_default():
    assert batch_glossary_reasoning_options() == {"reasoning_effort": "high"}


def test_batch_glossary_ai_review_disables_expensive_reasoning():
    assert batch_glossary_review_reasoning_options() == {"reasoning_effort": "none"}


def test_ai_review_fixed_glossary_only_keeps_sources_in_current_batch():
    fixed_glossary = "\n".join(
        [
            "order flow->订单流",
            "SPX->标普500指数",
            "unrelated term->无关术语",
        ]
    )

    assert relevant_fixed_glossary(
        fixed_glossary,
        ["Order Flow", "spx"],
    ) == "order flow->订单流\nSPX->标普500指数"


def test_batch_glossary_unifier_is_bounded():
    assert batch_glossary_unifier_candidate_limit() == 40
    assert batch_glossary_unifier_timeout_seconds() == 300


def test_batch_glossary_summary_can_carry_session_glossary_without_fixed_candidates():
    summary = BatchGlossarySummary(
        candidates=[],
        skipped_existing=[],
        failures=[],
        session_glossary="scalp->剥头皮\nstop placement->止损位",
    )

    assert summary.candidates == []
    assert summary.session_glossary == "scalp->剥头皮\nstop placement->止损位"


def test_batch_glossary_summary_can_carry_auto_fixed_glossary():
    summary = BatchGlossarySummary(
        candidates=[],
        skipped_existing=[],
        failures=[],
        session_glossary="order flow->订单流",
        fixed_glossary="SPX->标普500指数",
    )

    assert summary.session_glossary == "order flow->订单流"
    assert summary.fixed_glossary == "SPX->标普500指数"


def test_human_review_filter_keeps_only_terms_that_need_a_decision():
    candidates = [
        GlossaryCandidate("iron condor", "iron condor strategy", ("a.srt", "b.srt", "c.srt"), "new"),
        GlossaryCandidate("calendar spread", "calendar spread strategy", ("a.srt", "b.srt"), "new"),
        GlossaryCandidate("lot", "hand", ("a.srt",), "new"),
        GlossaryCandidate("get the hang of", "learn", ("a.srt",), "new"),
        GlossaryCandidate("SPY", "S&P 500 ETF", ("a.srt", "b.srt"), "translation conflict"),
        GlossaryCandidate("SPY", "SPY", ("c.srt",), "translation conflict"),
        GlossaryCandidate("order flow", "order flow", ("a.srt",), "translation conflict"),
        GlossaryCandidate("order flow", "commission flow", ("b.srt",), "translation conflict"),
    ]

    assert filter_human_review_glossary_candidates(candidates) == [
        GlossaryCandidate("SPY", "S&P 500 ETF", ("a.srt", "b.srt"), "translation conflict"),
        GlossaryCandidate("SPY", "SPY", ("c.srt",), "translation conflict"),
        GlossaryCandidate("order flow", "order flow", ("a.srt",), "translation conflict"),
        GlossaryCandidate("order flow", "commission flow", ("b.srt",), "translation conflict"),
    ]


def test_human_review_filter_ignores_generic_status_without_real_conflict():
    candidates = [
        GlossaryCandidate("Bank of Japan", "Bank of Japan", ("a.srt",), "new or translation conflict"),
        GlossaryCandidate("top of the hour", "hour marker", ("a.srt",), "translation conflict"),
        GlossaryCandidate("theta", "theta decay", ("a.srt",), "new"),
        GlossaryCandidate("theta", "time decay", ("b.srt",), "new"),
    ]

    assert filter_human_review_glossary_candidates(candidates) == [
        GlossaryCandidate("theta", "theta decay", ("a.srt",), "new"),
        GlossaryCandidate("theta", "time decay", ("b.srt",), "new"),
    ]


def test_merge_candidates_filters_common_single_words_before_review():
    result = merge_batch_glossary_candidates(
        [
            (
                "file_a.srt",
                "\n".join(
                    [
                        "order->订单",
                        "make->制作",
                        "look->看",
                        "order flow->订单流",
                        "liquidity sweep->流动性扫单",
                        "API->接口",
                        "Delta->Delta",
                    ]
                ),
            ),
        ],
        fixed_glossary="",
    )

    assert [candidate.source for candidate in result.candidates] == [
        "order flow",
        "liquidity sweep",
        "API",
        "Delta",
    ]


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


def test_merge_candidates_collapses_case_only_source_variants():
    result = merge_batch_glossary_candidates(
        [
            ("file_a.srt", "theta->theta衰减\nOptionStrat->期权策略工具"),
            ("file_b.srt", "Theta->Theta（时间衰减）\noption strat->OptionStrat（期权策略工具）"),
        ],
        fixed_glossary="",
    )

    by_source = {candidate.source: candidate for candidate in result.candidates}
    assert set(by_source) == {"Theta", "OptionStrat"}
    assert by_source["Theta"].files == ("file_a.srt", "file_b.srt")
    assert by_source["OptionStrat"].files == ("file_a.srt", "file_b.srt")


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
