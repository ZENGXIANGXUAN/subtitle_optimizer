"""Systematic quality scan of the produced subtitles."""
import io
import json
import re
import sys

RUN = sys.argv[1] if len(sys.argv) > 1 else "run1"


def load_srt(path):
    out = []
    for b in io.open(path, encoding="utf-8").read().strip().split("\n\n"):
        L = [x for x in b.splitlines() if x.strip()]
        if len(L) < 3:
            continue
        a, z = L[1].split(" --> ")
        def sec(x):
            hh, mm, rest = x.split(":")
            ss, ms = rest.split(",")
            return int(hh)*3600 + int(mm)*60 + int(ss) + int(ms)/1000
        out.append({"i": int(L[0]), "s": sec(a), "e": sec(z),
                    "tgt": L[2], "src": L[3] if len(L) > 3 else ""})
    return out


src_cues = load_srt(f"{RUN}/01_transcribed.srt")
final = load_srt(f"{RUN}/07_final.srt")
cjk = lambda t: sum(1 for c in t if "一" <= c <= "鿿")
words = lambda t: len(re.findall(r"[A-Za-z]+(?:['-][A-Za-z]+)?", t))

print("=" * 78)
print(f"字幕质量扫描  ({RUN})")
print("=" * 78)
print(f"转录 {len(src_cues)} 条  ->  成品 {len(final)} 条")

# --- 1. 内容守恒：原文词数是否守恒 -------------------------------------
src_words = sum(words(c["tgt"]) for c in src_cues)   # transcript is monolingual
out_words = sum(words(c["src"]) for c in final)
print(f"\n[1] 原文词数守恒: 转录 {src_words} -> 成品保留 {out_words} "
      f"({'OK' if src_words == out_words else 'DIFF ' + str(out_words - src_words)})")

# --- 2. 时间轴 ---------------------------------------------------------
gaps, overlaps = [], []
for a, b in zip(final, final[1:]):
    d = b["s"] - a["e"]
    if d < 0:
        overlaps.append((a["i"], round(d, 2)))
    elif d > 3:
        gaps.append((a["i"], round(d, 1)))
print(f"[2] 时间轴: 重叠 {len(overlaps)} 处, >3s 空档 {len(gaps)} 处")
if overlaps:
    print(f"    重叠: {overlaps[:6]}")

# --- 3. 阅读压力 -------------------------------------------------------
cps = [(c["i"], round(cjk(c["tgt"]) / max(0.1, c["e"] - c["s"]), 1), c["tgt"][:26])
       for c in final]
hot = [x for x in cps if x[1] >= 9]
slow = [x for x in cps if x[1] < 1.2]
print(f"[3] 中文阅读速度: >=9 字/秒 {len(hot)} 条, <1.2 字/秒 {len(slow)} 条")
for x in hot[:5]:
    print(f"    #{x[0]} {x[1]} 字/秒  {x[2]}")

# --- 4. 单条长度 -------------------------------------------------------
long_cues = [(c["i"], cjk(c["tgt"]), c["tgt"][:34]) for c in final if cjk(c["tgt"]) > 28]
print(f"[4] 单条超 28 汉字: {len(long_cues)} 条")
for x in long_cues[:6]:
    print(f"    #{x[0]} {x[1]} 字  {x[2]}...")

# --- 5. 疑似漏译：原文长但译文过短 -------------------------------------
sus = []
for c in final:
    w, ch = words(c["src"]), cjk(c["tgt"])
    if w >= 12 and ch < w * 0.85:
        sus.append((c["i"], w, ch, c["src"][:52], c["tgt"][:30]))
print(f"\n[5] 疑似压缩/漏译 (原文>=12词 且 汉字数 < 词数*0.85): {len(sus)} 条")
for x in sus[:8]:
    print(f"    #{x[0]} {x[1]}词 -> {x[2]}字")
    print(f"        原 {x[3]}")
    print(f"        译 {x[4]}")

# --- 6. 未翻译残留 -----------------------------------------------------
untranslated = [(c["i"], c["tgt"]) for c in final
                if cjk(c["tgt"]) == 0 and words(c["tgt"]) > 0]
print(f"\n[6] 译文行完全没有中文: {len(untranslated)} 条  {untranslated[:4]}")

# --- 7. 术语污染 -------------------------------------------------------
bad_terms = {"回测": "retest->回测 (期货术语)", "猪肉": "HE->猪肉期货",
             "阻力位": "resistance->阻力位", "支撑位": "support->支撑位",
             "盘口": "DOM->盘口", "价格结构": "structure->价格结构",
             "模拟交易": "sim->模拟交易", "风险回报": "R->风险回报比"}
print("\n[7] 交易术语表污染:")
found = False
for cue in final:
    for k, why in bad_terms.items():
        if k in cue["tgt"]:
            found = True
            print(f"    #{cue['i']} 命中「{k}」  <- {why}")
            print(f"        原 {cue['src'][:60]}")
            print(f"        译 {cue['tgt'][:50]}")
if not found:
    print("    无")

# --- 8. ASR 幻觉专名 ---------------------------------------------------
halluc = ["Ascensivore", "PataBort", "CentaBort"]
print("\n[8] ASR 误识专名被术语表锁定:")
for h in halluc:
    hits = [c["i"] for c in final if h in c["tgt"]]
    if hits:
        print(f"    {h:13} 出现在成品第 {hits} 条（已被写入强制术语表）")

# --- 9. 审计标记 -------------------------------------------------------
audit = json.load(io.open(f"{RUN}/08_audit.json", encoding="utf-8"))
from collections import Counter
print("\n[9] 质量标记分布:")
for k, v in Counter(a["stage"] for a in audit).most_common():
    print(f"    stage_status  {k:34} {v}")
flags = Counter(f for a in audit for f in a["flags"])
print(f"    quality_flags {dict(flags) or '无'}")
print(f"    status        {dict(Counter(a['status'] for a in audit))}")
