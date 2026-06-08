"""
SRT 字幕时间戳检查工具
检测文件夹下所有 .srt 文件，找出时间戳顺序有问题的文件。
只检查时间戳是否倒退、结束早于开始、无法解析等问题。
"""

import re
import sys
from pathlib import Path


def srt_time_to_ms(t: str) -> int:
    t = t.strip()
    try:
        h, m, rest = t.split(":")
        s, ms = rest.replace(".", ",").split(",")
        return int(h) * 3600000 + int(m) * 60000 + int(s) * 1000 + int(ms)
    except Exception:
        return -1


def check_file(content: str) -> list[str]:
    """检查时间戳顺序，返回问题描述列表（空表示正常）"""
    content = content.replace("\r\n", "\n").replace("\r", "\n")
    raw_blocks = re.split(r"\n{2,}", content.strip())

    issues = []
    prev_start_ms = -1
    prev_start_raw = ""

    for pos, raw in enumerate(raw_blocks):
        lines = raw.strip().splitlines()
        if len(lines) < 2:
            continue
        m = re.match(
            r"(\d{2}:\d{2}:\d{2}[,\.]\d{3})\s*-->\s*(\d{2}:\d{2}:\d{2}[,\.]\d{3})",
            lines[1].strip() if len(lines) > 1 else ""
        )
        if not m:
            continue

        start_raw, end_raw = m.group(1), m.group(2)
        start_ms = srt_time_to_ms(start_raw)
        end_ms = srt_time_to_ms(end_raw)

        if start_ms < 0:
            issues.append(f"  pos={pos+1}  时间戳无法解析: {start_raw}")
        elif end_ms >= 0 and end_ms < start_ms:
            issues.append(f"  pos={pos+1}  结束早于开始: {start_raw} --> {end_raw}")
        elif prev_start_ms >= 0 and start_ms < prev_start_ms:
            issues.append(f"  pos={pos+1}  时间戳倒退: {prev_start_raw} → {start_raw}")

        if start_ms >= 0:
            prev_start_ms = start_ms
            prev_start_raw = start_raw

    return issues


def scan_folder(folder: str, verbose: bool = False):
    folder_path = Path(folder)
    srt_files = sorted(folder_path.rglob("*.srt"))

    if not srt_files:
        print(f"[!] 未找到任何 .srt 文件: {folder}")
        return

    print(f"扫描目录: {folder_path.resolve()}")
    print(f"共找到 {len(srt_files)} 个 .srt 文件\n")
    print("=" * 70)

    ok = 0
    error = 0
    parse_fail = 0
    problem_files = []

    for srt_path in srt_files:
        rel = srt_path.relative_to(folder_path)
        try:
            content = srt_path.read_text(encoding="utf-8-sig", errors="replace")
        except Exception as e:
            print(f"[读取失败] {rel}  ← {e}")
            parse_fail += 1
            continue

        issues = check_file(content)

        if not issues:
            ok += 1
            if verbose:
                print(f"[OK]  {rel}")
        else:
            error += 1
            problem_files.append((str(rel), issues))
            print(f"[ERROR]  {rel}  ({len(issues)} 处时间戳问题)")
            for iss in issues[:5]:
                print(iss)
            if len(issues) > 5:
                print(f"  ... 还有 {len(issues) - 5} 处")
            print()

    print("=" * 70)
    print(f"扫描结果:")
    print(f"  ✅ 正常   : {ok} 个")
    print(f"  ❌ 有问题 : {error} 个")
    if parse_fail:
        print(f"  💔 读取失败: {parse_fail} 个")
    print()

    if not problem_files:
        print("🎉 所有字幕文件时间戳顺序正常。")
    else:
        print("问题文件列表：")
        for path, _ in problem_files:
            print(f"  {path}")


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(
        description="检查文件夹下所有 SRT 字幕的时间戳顺序"
    )
    parser.add_argument("folder", help="要扫描的文件夹路径")
    parser.add_argument("-v", "--verbose", action="store_true",
                        help="同时显示正常文件")
    args = parser.parse_args()

    scan_folder(args.folder, verbose=args.verbose)