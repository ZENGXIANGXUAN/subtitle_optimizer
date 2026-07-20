from core.batch_glossary import (
    BatchGlossarySummary,
    GlossaryCandidate,
    parse_batch_glossary_ai_review_result,
)


def _install_worker_import_stubs():
    import sys
    import types

    class Signal:
        def connect(self, *args, **kwargs):
            return None

        def emit(self, *args, **kwargs):
            return None

    def pyqt_signal(*args, **kwargs):
        return Signal()

    def pyqt_slot(*args, **kwargs):
        def decorate(fn):
            return fn

        return decorate

    qtcore = types.ModuleType("PyQt6.QtCore")
    qtcore.QObject = object
    qtcore.pyqtSignal = pyqt_signal
    qtcore.pyqtSlot = pyqt_slot
    pyqt6 = types.ModuleType("PyQt6")
    aiohttp = types.ModuleType("aiohttp")
    aiohttp.ClientSession = object

    sys.modules.setdefault("PyQt6", pyqt6)
    sys.modules.setdefault("PyQt6.QtCore", qtcore)
    sys.modules.setdefault("aiohttp", aiohttp)


def test_ai_review_parser_preserves_session_and_fixed_glossary():
    original = BatchGlossarySummary(
        candidates=[GlossaryCandidate("SPX", "标普500指数", ("a.srt",), "译法冲突")],
        skipped_existing=[],
        failures=[],
        session_glossary="order flow->订单流",
    )
    raw = (
        '{"session_glossary":[{"source":"order flow","target":"订单流"}],'
        '"fixed_candidates":[{"source":"SPX","target":"标普500指数","reason":"stable cross-context term"}],'
        '"dropped":[{"source":"lot","reason":"common word"}]}'
    )

    reviewed = parse_batch_glossary_ai_review_result(raw, original, fixed_glossary="SPY->已有译法")

    assert reviewed.session_glossary == "order flow->订单流"
    assert reviewed.fixed_glossary == "SPX->标普500指数"
    assert reviewed.candidates == []


def test_ai_review_parser_allows_zero_fixed_candidates():
    original = BatchGlossarySummary(
        candidates=[GlossaryCandidate("order flow", "订单流", ("a.srt",), "译法冲突")],
        skipped_existing=[],
        failures=[],
        session_glossary="order flow->订单流",
    )
    raw = '{"session_glossary":[{"source":"order flow","target":"订单流"}],"fixed_candidates":[],"dropped":[]}'

    reviewed = parse_batch_glossary_ai_review_result(raw, original, fixed_glossary="")

    assert reviewed.session_glossary == "order flow->订单流"
    assert reviewed.fixed_glossary == ""
    assert reviewed.candidates == []


def test_stage_one_auto_terms_do_not_override_batch_glossary_case_variants():
    _install_worker_import_stubs()
    from core.workers import merge_glossaries

    merged = merge_glossaries(
        "OptionStrat->期权策略工具",
        "option strat->期权策略\nGamma->伽马",
    )

    assert merged.raw.splitlines() == [
        "OptionStrat->期权策略工具",
        "Gamma->伽马",
    ]
    assert merged.conflicts == [("option strat", "期权策略工具", "期权策略")]
