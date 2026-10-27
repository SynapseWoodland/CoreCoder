"""Dogfood 阶段 1 的记录脚手架：只读 + 记卡点，绝不碰 corecoder/。

对应 optimize/dogfood-plan.md 阶段 1 的「只跑不改」纪律：本脚本只做两件事——

- 读已有的 audit 日志，把每次真实运行（build=dogfood）的
  rounds / outcome / tokens_in 三个数聚出来；
- 给你一行卡点模板，把「现象 + 当时调用的工具 + outcome」这种
  机器替代不了的人的体感，追加进 notes.md，攒成阶段 2 要固化的用例素材。

它不进 CI、不改 agent、不联网。运行位置在 corecoder/ 与 tests/ 之外。

用法（Windows cmd，仓库根目录）：
    .venv\\Scripts\\python.exe dogfood\\dogfood_log.py summary
    .venv\\Scripts\\python.exe dogfood\\dogfood_log.py last
    .venv\\Scripts\\python.exe dogfood\\dogfood_log.py note ^
        --phenomenon "找 parse_config 连用 glob 没命中，最后靠 grep" ^
        --tools glob,grep --outcome answer
"""

from __future__ import annotations

import argparse
import json
import os
import re
from datetime import datetime
from pathlib import Path

BUILD_TAG = os.getenv("CORECODER_BUILD", "dogfood")
NOTE_FIELDS = ("session", "outcome", "error_type", "rounds",
               "tokens_in", "cost_usd", "model")


def audit_dir() -> Path:
    """Where the audit stream lands; overridable via CORECODER_AUDIT_DIR."""
    return Path(os.getenv("CORECODER_AUDIT_DIR")
               or Path.home() / ".corecoder")


def parse_fields(line: str) -> dict:
    """Split an ``event=xxx k=v k=v`` audit line into a dict.

    Values with spaces/= are JSON-quoted by audit._format, so a bare token
    ends at the next space while a quoted one ends at its closing quote
    (escapes respected). Scanning per field keeps embedded ``=`` from
    breaking the split.
    """
    out: dict = {}
    i, n = 0, len(line)
    while i < n:
        while i < n and line[i] == " ":
            i += 1
        j = line.find("=", i)
        if j == -1:
            break
        key, i = line[i:j], j + 1
        if i < n and line[i] == '"':
            k, buf = i + 1, []
            while k < n:
                if line[k] == "\\":
                    buf.append(line[k:k + 2])
                    k += 2
                    continue
                if line[k] == '"':
                    break
                buf.append(line[k])
                k += 1
            out[key] = json.loads('"' + "".join(buf) + '"')
            i = k + 1
        else:
            k = i
            while k < n and line[k] != " ":
                k += 1
            out[key] = line[i:k]
            i = k
    return out


def load_summaries(tag: str = BUILD_TAG) -> list[dict]:
    """Every run_summary line from the real (dogfood) runs, oldest first."""
    rows = []
    for path in sorted(audit_dir().glob("audit.*.log")):
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for line in text.splitlines():
            if not line.startswith("event=run_summary"):
                continue
            fields = parse_fields(line)
            if fields.get("build", "") == tag:
                rows.append(fields)
    return rows


def _int(row: dict, key: str) -> int:
    try:
        return int(row.get(key, 0) or 0)
    except (TypeError, ValueError):
        return 0


def cmd_summary(args) -> int:
    """Print the dogfood ledger: three numbers per run + acceptance progress."""
    rows = load_summaries(args.build)
    if not rows:
        print(f"没有 build={args.build} 的运行记录。"
              f"确认 .env 里 CORECODER_BUILD={args.build} 且真的跑过 corecoder。")
        return 1
    dist: dict = {}
    for row in rows:
        dist[row.get("outcome", "?")] = dist.get(row.get("outcome", "?"), 0) + 1
    print(f"build={args.build} 共 {len(rows)} 次运行"
          f"（阶段 1 验收需 >=20）：\n")
    print(f"  {'session':<32} {'outcome':<11} {'rounds':>6} "
          f"{'tokens_in':>10}  cost_usd")
    for row in rows[-args.tail:]:
        print(f"  {row.get('session', '-'):<32} "
              f"{row.get('outcome', '-'):<11} "
              f"{_int(row, 'rounds'):>6} {_int(row, 'tokens_in'):>10}  "
              f"{row.get('cost_usd', '-')}")
    non_answer = sum(v for k, v in dist.items() if k != "answer")
    print(f"\n  outcome 分布：{dist}")
    print(f"  非 answer 占比：{non_answer}/{len(rows)}"
          f"（{non_answer * 100 // max(len(rows), 1)}%）")
    print(f"  验收进度：{min(len(rows), 20)}/20 会话")
    return 0


def cmd_last(args) -> int:
    """Dump full fields of the most recent dogfood runs (for a卡点 line)."""
    rows = load_summaries(args.build)
    if not rows:
        print(f"没有 build={args.build} 的运行记录。")
        return 1
    for row in rows[-args.n:]:
        print("-" * 60)
        for key in NOTE_FIELDS:
            print(f"  {key:<12} {row.get(key, '-')}")
    return 0


def notes_path() -> Path:
    return Path(__file__).with_name("notes.md")


def cmd_note(args) -> int:
    """Append one卡点 line: 现象 + 当时调用的工具 + outcome."""
    path = notes_path()
    if not path.exists():
        path.write_text(
            "# Dogfood 阶段 1 卡点记录\n\n"
            "每行：时间 · session · outcome · rounds · 工具 · 现象。"
            "阶段 2 从这里挑高频现象固化成 examples/ 脚本。\n\n",
            encoding="utf-8",
        )
    stamp = datetime.now().strftime("%Y-%m-%d %H:%M")
    entry = (f"- {stamp} · session={args.session or '-'} · "
             f"outcome={args.outcome} · rounds={args.rounds} · "
             f"工具={args.tools or '-'} · 现象：{args.phenomenon}\n")
    with path.open("a", encoding="utf-8") as fh:
        fh.write(entry)
    print(f"已记入 {path}:\n{entry}", end="")
    return 0


def _note_fields(line: str) -> dict:
    """Pull the ``k=v`` tokens out of one卡点 line.

    The line is `` · ``-separated; only the ``key=value`` segments matter
    here, so a phenomenon text that happens to contain an ``=`` just adds a
    stray key we never read. The tool-sequence key is written as 工具.
    """
    out: dict = {}
    for part in line.split(" · "):
        if "=" in part:
            key, _, value = part.partition("=")
            out[key.strip()] = value.strip()
    return out


def _slug(tools: str, outcome: str) -> str:
    """Suggest an examples/ filename from the tool sequence and outcome."""
    toks = re.split(r"[\s,]+", tools)
    stem = "_".join(t for t in (re.sub(r"[^0-9a-zA-Z_]", "", x) for x in toks) if t)
    tail = re.sub(r"[^0-9a-zA-Z_]", "", outcome) or "x"
    return f"examples/df_{stem or 'tool'}_{tail}.py"


def cmd_examples(args) -> int:
    """Group卡点 lines by (tool sequence, outcome) into阶段 2 候选清单."""
    path = notes_path()
    if not path.exists():
        print("notes.md 还不存在——先用 note 记几条卡点，再跑 examples 归并。")
        return 1
    groups: dict = {}
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        if not line.startswith("- "):
            continue
        fields = _note_fields(line)
        tools = (fields.get("工具") or fields.get("tools") or "").strip()
        outcome = (fields.get("outcome") or "?").strip()
        if not tools or tools == "-":
            continue
        # sequence matters: normalize spacing but keep tool order
        seq = ",".join(t for t in re.split(r"[\s,]+", tools) if t)
        bucket = groups.setdefault((seq, outcome), {"n": 0, "samples": []})
        bucket["n"] += 1
        phenomenon = line.partition("现象：")[2].strip()
        if phenomenon and len(bucket["samples"]) < 3:
            bucket["samples"].append(phenomenon)
    ranked = sorted(groups.items(), key=lambda kv: kv[1]["n"], reverse=True)
    hits = [(k, v) for k, v in ranked if v["n"] >= args.min]
    print(f"按『工具序列+outcome』归并（min={args.min}），"
          f"{len(hits)} 条候选 / 共 {sum(v['n'] for v in groups.values())} 张卡点：\n")
    for (seq, outcome), bucket in hits:
        print(f"  [{bucket['n']}x] 工具={seq} outcome={outcome}")
        print(f"        → 建议固化成 {_slug(seq, outcome)}")
        for sample in bucket["samples"]:
            print(f"        样例：{sample}")
    if not hits:
        print(f"  没有重复 >= {args.min} 次的现象；继续跑阶段 1 攒样本，"
              f"或降低 --min。")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Dogfood 阶段 1 记录脚手架（只读）")
    p.add_argument("--build", default=BUILD_TAG,
                   help="运行标签，默认取 CORECODER_BUILD（dogfood）")
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("summary", help="三数聚合 + 验收进度")
    s.add_argument("--tail", type=int, default=20, help="最多列最近几条")
    s.set_defaults(func=cmd_summary)
    la = sub.add_parser("last", help="打印最近运行的全部字段")
    la.add_argument("-n", type=int, default=3)
    la.set_defaults(func=cmd_last)
    nt = sub.add_parser("note", help="追加一行卡点记录")
    nt.add_argument("--phenomenon", required=True, help="现象（必填）")
    nt.add_argument("--tools", default="", help="当时调用的工具，逗号分隔")
    nt.add_argument("--outcome", default="?")
    nt.add_argument("--rounds", default="?")
    nt.add_argument("--session", default="")
    nt.set_defaults(func=cmd_note)
    ex = sub.add_parser("examples", help="按工具序列+outcome 归并出阶段 2 候选")
    ex.add_argument("--min", type=int, default=2,
                    help="出现次数阈值，默认 2（只列重复现象）")
    ex.set_defaults(func=cmd_examples)
    return p


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
