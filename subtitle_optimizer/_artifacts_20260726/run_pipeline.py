"""Run the real transcription + optimization pipeline on a media file.

Mirrors BatchWorker._run() exactly, minus Qt: same normalization, same
analyze -> merge glossary -> execute() sequence, same export.
"""
import asyncio
import json
import os
import sys
import time
from pathlib import Path

REPO = r"D:/Users/xuan/Mycode/subtitle_optimizer/subtitle_optimizer"
sys.path.insert(0, REPO)
os.chdir(REPO)

from core.client import MistralClient
from core.models import parse_srt
from core.pipeline import SubtitlePipeline
from core.source_normalization import normalize_source_entries
from core.transcription import (
    is_media_file, should_use_chunked_transcription,
    transcribe_chunked_to_srt, transcribe_media_to_srt,
)
from core.workers import (
    LANGUAGE_MAP, _format_glossary_prompt, filter_glossary_by_entries,
    format_glossary_merge_log, merge_glossaries,
)

MEDIA = sys.argv[1]
OUT = Path(sys.argv[2])
OUT.mkdir(parents=True, exist_ok=True)

cfg = json.load(open(os.path.expanduser("~/.subtitle_optimizer_config.json"), encoding="utf-8"))
INPUT_LANG, OUTPUT_LANG = "English", "中文"
t0 = time.perf_counter()


def log(msg):
    print(f"[{time.perf_counter()-t0:7.1f}s] {msg}", flush=True)


async def main():
    client = MistralClient(
        api_key=cfg["api_key"], model=cfg["model"],
        base_url=cfg["base_url"], transcription_model=cfg.get("transcription_model", ""),
    )
    log(f"client: model={client.model} transcribe={client.transcription_model} "
        f"local_proxy={client.is_local_proxy}")

    # ---- 1. transcribe -------------------------------------------------
    srt_path = OUT / "01_transcribed.srt"
    if not srt_path.exists():
        log(f"transcribing {Path(MEDIA).name} "
            f"(chunked={should_use_chunked_transcription(MEDIA)})")
        if should_use_chunked_transcription(MEDIA):
            _, content = await transcribe_chunked_to_srt(
                client, MEDIA, output_path=str(srt_path),
                model=cfg.get("transcription_model") or None,
                language=None,
                audio_format=cfg.get("transcription_audio_format", "mp3"),
                concurrency=int(cfg.get("chunk_concurrency") or 4),
                on_log=lambda m: log(f"  {m}"),
            )
        else:
            _, content = await transcribe_media_to_srt(
                client, MEDIA, output_path=str(srt_path),
                model=cfg.get("transcription_model") or None,
                audio_format=cfg.get("transcription_audio_format", "mp3"),
                on_log=lambda m: log(f"  {m}"),
            )
    else:
        log("reusing existing transcript")
        content = srt_path.read_text(encoding="utf-8-sig")

    entries = parse_srt(content)
    log(f"parsed {len(entries)} cues from transcript")

    # ---- 2. normalize source layout (same as BatchWorker) --------------
    normalized = normalize_source_entries(
        entries,
        source_line_index=int(cfg.get("original_line", 1)),
        input_lang_code=LANGUAGE_MAP[INPUT_LANG][0],
        output_lang_code=LANGUAGE_MAP[OUTPUT_LANG][0],
        requested_layout=cfg.get("source_layout", "auto"),
    )
    for w in normalized.warnings:
        log(f"  WARN {w}")
    entries = normalized.entries
    log(f"layout={normalized.detected_layout} confidence={normalized.confidence:.0%}")

    pipeline = SubtitlePipeline(
        client, INPUT_LANG, OUTPUT_LANG,
        concurrency=int(cfg.get("concurrency", 16)),
        batch_size=int(cfg.get("batch_size", 30)),
    )
    log(f"pipeline: concurrency={pipeline.concurrency} "
        f"batch_size={pipeline.batch_size} (requested {pipeline.requested_batch_size})")

    # ---- 3. analysis + glossary ---------------------------------------
    user_glossary = filter_glossary_by_entries(cfg.get("glossary", ""), entries)
    log(f"user glossary: {len(cfg.get('glossary','').splitlines())} lines -> "
        f"{len(user_glossary.splitlines())} kept after filtering")
    (OUT / "02_glossary_filtered.txt").write_text(user_glossary, encoding="utf-8")

    log("stage 1: analyze")
    analysis = await pipeline.analyze_details(entries)
    (OUT / "03_analysis_raw.json").write_text(analysis.raw, encoding="utf-8")
    (OUT / "04_context.txt").write_text(analysis.context, encoding="utf-8")
    log(f"  context {len(analysis.context)} chars, "
        f"auto glossary {len(analysis.auto_glossary.splitlines())} lines")

    merge = merge_glossaries(user_glossary, analysis.auto_glossary)
    for line in format_glossary_merge_log(merge):
        log(f"  {line}")
    (OUT / "05_glossary_merged.txt").write_text(merge.raw, encoding="utf-8")
    glossary = _format_glossary_prompt(merge.raw)

    # ---- 4. execute ----------------------------------------------------
    snapshots = {}

    def on_snapshot(stage, stage_entries, use_optimized):
        snapshots[stage] = [
            {"i": e.index, "start": e.start_time, "end": e.end_time,
             "lines": list(e.optimized_lines if (use_optimized and e.optimized_lines) else e.lines),
             "status": e.status, "stage": e.stage_status, "flags": list(e.quality_flags)}
            for e in stage_entries
        ]
        (OUT / f"06_after_{stage}.json").write_text(
            json.dumps(snapshots[stage], ensure_ascii=False, indent=1), encoding="utf-8")
        log(f"  snapshot {stage}: {len(stage_entries)} cues")

    result = await pipeline.execute(
        entries, analysis.context, glossary,
        segment_batch_size=max(10, min(30, int(cfg.get("batch_size", 30)))),
        on_stage=lambda s: log(f"stage: {s}"),
        on_progress=lambda s, c, t: log(f"  {s} {c}/{t}") if c % 40 == 0 or c == t else None,
        on_snapshot=on_snapshot,
        on_log=lambda m: log(f"  {m}"),
    )

    # ---- 5. export ------------------------------------------------------
    final = result.entries
    blocks = [e.to_srt_block(use_optimized=True, target_first=True) for e in final]
    (OUT / "07_final.srt").write_text("\n".join(blocks), encoding="utf-8")

    audit = [
        {"i": e.index, "start": e.start_time, "end": e.end_time,
         "lines": list(e.optimized_lines or e.lines),
         "status": e.status, "stage": e.stage_status, "flags": list(e.quality_flags),
         "err": e.error_msg}
        for e in final
    ]
    (OUT / "08_audit.json").write_text(json.dumps(audit, ensure_ascii=False, indent=1), encoding="utf-8")
    if result.lint_report:
        (OUT / "09_lint.txt").write_text(result.lint_report.to_text(), encoding="utf-8")

    log(f"DONE {len(final)} cues -> {OUT/'07_final.srt'}")
    log(f"quality gate passed={result.quality_report.passed} "
        f"critical={len(result.quality_report.critical_issues)}")


asyncio.run(main())
