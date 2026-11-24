#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""生成人工盲评表 / 回填盲评分数（A-2）。

自动判据只能查硬伤，`needs_human` 的题必须由人看过才知道行不行。
本脚本把多个模型的回复**隐去模型名、随机分配匿名编号**后并排列出，
避免「看到是 v5 就倾向于打高分」的确认偏差。

用法：
    # 1) 生成盲评表（跑完各版评测后）
    python scripts/make_blind_review.py --tags b_v5gguf c_v5gguf --mode runtime --prefix a_review

    # 只想先试手：每个意图簇随机抽 1 题，十几条就能走完整个流程
    python scripts/make_blind_review.py --tags b_v5gguf c_v5gguf --mode runtime \
        --limit-per-cluster 1 --prefix try_review

    # 2) 打分页点「复制分数」，把文本直接粘给助手；或下载 CSV 后回填
    python scripts/make_blind_review.py --paste 'BLIND_V1|try_review|12/12\n1-A=2,...'
    python scripts/make_blind_review.py --collect output/filled_try_review.csv --prefix try_review

    # 打分页功能升级时只重刷 HTML，不动 CSV 与匿名密钥（已填分数仍有效）
    python scripts/make_blind_review.py --refresh-html

回填会按「程序化回复 / 模型回复」分组统计：程序化那部分是规则说的，
**不算模型能力**，比较模型优劣只看「模型」那几行。

评分标准（写在表头，避免口径漂移）：
    0 = 答非所问 / 明显违背 expect / 完全不能用
    1 = 沾边但有明显问题（跑题、说教、编造、没接住情绪、像模板句）
    2 = 合格：像熟人一样接住了这句话，符合 expect

产物（`<prefix>` 默认 blind_review，可用 --prefix 让多套表并存）：
    output/<prefix>.md          给人看的题目+匿名回复
    output/<prefix>.csv         填写 score 用
    output/<prefix>_key.json    匿名编号 ↔ 模型映射（回填时用，评卷人勿看）
    output/<prefix>_scores.md   回填后的各模型得分
"""

from __future__ import annotations

import argparse
import csv
import json
import random
import re
import sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "output"

SCORE_GUIDE = "0=答非所问/完全不能用  1=沾边但明显有问题  2=合格（像熟人一样接住了这句话）"


def esc(text: str) -> str:
    return (text or "").replace("|", "\\|").replace("\n", " ").replace("\r", " ").strip()


_HTML_TEMPLATE = r"""<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>慕心 · 人工盲评</title>
<style>
  :root { color-scheme: light; }
  * { box-sizing: border-box; }
  body { margin:0; background:#F6F5F2; color:#23252B;
         font-family:"Microsoft YaHei UI","Microsoft YaHei",system-ui,sans-serif;
         line-height:1.6; }
  .wrap { max-width:860px; margin:0 auto; padding:28px 20px 90px; }
  h1 { font-size:22px; margin:0 0 6px; }
  .sub { color:#6B6F7A; font-size:13px; margin-bottom:20px; }
  .guide { background:#FFF; border:1px solid #E4E2DC; border-left:4px solid #C08A4A;
           border-radius:8px; padding:14px 18px; font-size:13.5px; margin-bottom:22px; }
  .guide b { color:#8A5A22; }
  .bar { position:sticky; top:0; z-index:5; background:#F6F5F2F2; backdrop-filter:blur(6px);
         padding:10px 0 12px; border-bottom:1px solid #E4E2DC; margin-bottom:18px;
         display:flex; align-items:center; gap:14px; font-size:13px; }
  .track { flex:1; height:7px; background:#E6E4DE; border-radius:6px; overflow:hidden; }
  .fill { height:100%; width:0; background:#C08A4A; transition:width .25s; }
  .card { background:#FFF; border:1px solid #E4E2DC; border-radius:10px;
          padding:18px 20px; margin-bottom:16px; }
  .q { font-size:16px; font-weight:600; margin:0 0 4px; }
  .tag { display:inline-block; font-size:11px; color:#7A6A54; background:#F3EEE5;
         border-radius:4px; padding:1px 7px; margin-right:8px; vertical-align:2px; }
  .exp { font-size:12.5px; color:#737782; margin:0 0 14px; }
  .ctx { font-size:12.5px; color:#8A6D45; margin:0 0 12px; }
  .ans { display:flex; gap:12px; align-items:flex-start; padding:10px 0;
         border-top:1px dashed #E8E6E0; }
  .anon { flex:none; width:26px; height:26px; border-radius:6px; background:#EFEDE7;
          text-align:center; line-height:26px; font-weight:700; font-size:13px; }
  .txt { flex:1; font-size:14px; padding-top:2px; }
  .opts { flex:none; display:flex; gap:4px; }
  .opts button { width:30px; height:30px; border:1px solid #D9D6CE; background:#FFF;
                 border-radius:6px; cursor:pointer; font-size:13px; color:#555;
                 font-family:inherit; }
  .opts button:hover { border-color:#C08A4A; }
  .opts button.on { background:#C08A4A; border-color:#C08A4A; color:#FFF; font-weight:700; }
  .foot { position:fixed; bottom:0; left:0; right:0; background:#FFF;
          border-top:1px solid #E4E2DC; padding:12px 20px; display:flex;
          justify-content:center; gap:12px; align-items:center; }
  .foot button { padding:10px 22px; border-radius:8px; border:1px solid #C08A4A;
                 background:#C08A4A; color:#FFF; font-size:14px; cursor:pointer;
                 font-family:inherit; font-weight:600; }
  .foot button.ghost { background:#FFF; color:#8A5A22; }
  .foot span { font-size:12.5px; color:#737782; }
</style>
</head>
<body>
<div class="wrap">
  <h1>慕心 · 人工盲评</h1>
  <div class="sub">只看「这句话回复得怎么样」，不要猜哪个字母是哪个版本。</div>
  <div class="guide">
    <b>评分标准</b>　0 = 答非所问 / 完全不能用　·　1 = 沾边但明显有问题（跑题、说教、编造、像模板句）　·　2 = 合格，像熟人一样接住了这句话<br>
    <b>怎么填</b>　给每条回复点一个数字即可，进度会自动保存在本机浏览器里。<br>
    <b>交给助手</b>　点「复制分数」后直接粘贴到对话里最省事；或点「下载已填分数」把文件放到项目的 <code>output/</code> 目录。
  </div>
  <div class="bar"><span id="cnt">0 / 0</span><div class="track"><div class="fill" id="fill"></div></div></div>
  <main id="cards"></main>
</div>
<div class="foot">
  <span id="hint">填几条就可以交，未填的会留空</span>
  <button class="ghost" id="clear">清空重填</button>
  <button class="ghost" id="copy">复制分数</button>
  <button id="download">下载已填分数</button>
</div>
<script>
const PREFIX = "__PREFIX__";
const DATA = __DATA__;
const KEY = "blind_" + PREFIX;
let saved = {};
try { saved = JSON.parse(localStorage.getItem(KEY) || "{}"); } catch (e) { saved = {}; }

const groups = {};
DATA.rows.forEach(r => { (groups[r.eid] = groups[r.eid] || []).push(r); });
const cards = document.getElementById("cards");
Object.keys(groups).sort((a,b) => a - b).forEach(eid => {
  const g = groups[eid], m = g[0];
  const box = document.createElement("section");
  box.className = "card";
  let html = '<div class="q"><span class="tag">#' + eid + ' · ' + m.cluster + '</span>' + esc(m.instruction) + '</div>';
  if (m.input) html += '<div class="ctx">上下文：' + esc(m.input) + '</div>';
  html += '<div class="exp">期望：' + esc(m.expect) + '</div>';
  g.forEach(r => {
    html += '<div class="ans"><div class="anon">' + r.anon + '</div><div class="txt">' + esc(r.reply) + '</div>' +
            '<div class="opts" data-id="' + r.item_id + '">' +
            [0,1,2].map(n => '<button data-v="' + n + '">' + n + '</button>').join('') + '</div></div>';
  });
  box.innerHTML = html;
  cards.appendChild(box);
});

function esc(s) { return String(s || "").replace(/&/g,"&amp;").replace(/</g,"&lt;")
  .replace(/>/g,"&gt;").replace(/"/g,"&quot;"); }
function refresh() {
  const n = Object.keys(saved).length, total = DATA.rows.length;
  document.getElementById("cnt").textContent = n + " / " + total + " 条已评";
  document.getElementById("fill").style.width = (total ? n/total*100 : 0) + "%";
  document.querySelectorAll(".opts").forEach(o => {
    const v = saved[o.dataset.id];
    o.querySelectorAll("button").forEach(b => b.classList.toggle("on", b.dataset.v === v));
  });
}
cards.addEventListener("click", e => {
  const b = e.target.closest("button[data-v]");
  if (!b) return;
  const id = b.closest(".opts").dataset.id;
  saved[id] = b.dataset.v;
  localStorage.setItem(KEY, JSON.stringify(saved));
  refresh();
});
document.getElementById("clear").onclick = () => {
  if (!confirm("清空本页已填的分数？")) return;
  saved = {}; localStorage.removeItem(KEY); refresh();
};
document.getElementById("download").onclick = () => {
  const cols = ["item_id","eid","anon","cluster","split","instruction","expect","reply","score","note"];
  const q = s => '"' + String(s == null ? "" : s).replace(/"/g,'""') + '"';
  const lines = [cols.join(",")];
  DATA.rows.forEach(r => {
    const o = Object.assign({}, r, { score: saved[r.item_id] || "", note: "" });
    lines.push(cols.map(c => q(o[c])).join(","));
  });
  const blob = new Blob(["\ufeff" + lines.join("\r\n")], {type:"text/csv;charset=utf-8"});
  const a = document.createElement("a");
  a.href = URL.createObjectURL(blob);
  a.download = "filled_" + PREFIX + ".csv";
  a.click();
};
function payload() {
  const parts = DATA.rows
    .filter(r => saved[r.item_id] !== undefined)
    .map(r => r.item_id + "=" + saved[r.item_id]);
  return "BLIND_V1|" + PREFIX + "|" + parts.length + "/" + DATA.rows.length + "\n" + parts.join(",");
}
document.getElementById("copy").onclick = async () => {
  const text = payload(), hint = document.getElementById("hint");
  let ok = false;
  try { await navigator.clipboard.writeText(text); ok = true; } catch (e) {}
  if (!ok) {
    const ta = document.createElement("textarea");
    ta.value = text; ta.style.position = "fixed"; ta.style.opacity = "0";
    document.body.appendChild(ta); ta.select();
    try { ok = document.execCommand("copy"); } catch (e) {}
    ta.remove();
  }
  if (ok) {
    hint.textContent = "已复制 " + Object.keys(saved).length + " 条，粘贴到对话里发给我即可";
  } else {
    // 剪贴板被禁用时至少让文本可见可手选
    window.prompt("手动全选复制下面这段，粘贴到对话里：", text);
    hint.textContent = "已弹出文本框，复制后粘贴到对话里";
  }
};
refresh();
</script>
</body>
</html>
"""


def write_html(rows: list[dict], meta: dict, prefix: str) -> Path:
    """生成自包含的打分页：点选 0/1/2 → 下载可直接回填的 CSV。

    不读外部文件、不联网，数据全部内嵌；评卷人看不到模型名。
    """
    slim = []
    for r in rows:
        m = meta[int(r["eid"])]
        slim.append({
            "item_id": r["item_id"], "eid": r["eid"], "anon": r["anon"],
            "cluster": r["cluster"], "split": r["split"],
            "instruction": m["instruction"], "input": m["input"],
            "expect": m["expect"], "reply": r["reply"],
        })
    payload = json.dumps({"rows": slim}, ensure_ascii=False).replace("</", "<\\/")
    html = (_HTML_TEMPLATE.replace("__PREFIX__", prefix)
            .replace("__DATA__", payload))
    p = OUT / f"{prefix}.html"
    p.write_text(html, encoding="utf-8")
    return p


def load(tags: list[str], mode: str) -> tuple[dict[int, dict], dict[int, dict[str, str]]]:
    meta: dict[int, dict] = {}
    answers: dict[int, dict[str, str]] = defaultdict(dict)
    for tag in tags:
        p = OUT / f"eval_responses_{tag}.jsonl"
        if not p.exists():
            raise FileNotFoundError(f"找不到 {p}，先跑 scripts/eval.py --tag {tag}")
        for line in p.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            r = json.loads(line)
            if r.get("input_mode") != mode:
                continue
            eid = r["eid"]
            meta[eid] = {
                "instruction": r["instruction"],
                "input": r.get("input", ""),
                "expect": r.get("expect", ""),
                "cluster": r.get("cluster", ""),
                "split": r.get("split", ""),
            }
            answers[eid][tag] = r.get("reply", "")
    return meta, answers


def source_map(tags: list[str], mode: str) -> dict[tuple[str, int], bool | None]:
    """从评测明细读回「这条回复是程序说的还是模型说的」。

    旧版明细（A-3/B 阶段）没有该字段，返回 None 以便报告标注「按模型计」。
    """
    out: dict[tuple[str, int], bool | None] = {}
    for tag in tags:
        p = OUT / f"eval_responses_{tag}.jsonl"
        if not p.exists():
            continue
        for line in p.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            r = json.loads(line)
            if r.get("input_mode") != mode:
                continue
            out[(tag, int(r["eid"]))] = r.get("programmatic")
    return out


def build(tags: list[str], mode: str, seed: int, include_functional: bool,
          prefix: str = "blind_review", limit_per_cluster: int = 0) -> None:
    csv_path = OUT / f"{prefix}.csv"
    md_path = OUT / f"{prefix}.md"
    key_path = OUT / f"{prefix}_key.json"
    meta, answers = load(tags, mode)
    # 全局匿名映射：一次洗牌，评卷人看不到模型名，回填时有唯一答案
    shuffled = tags[:]
    random.Random(seed).shuffle(shuffled)
    anon_of = {tag: chr(ord("A") + i) for i, tag in enumerate(shuffled)}
    key = {
        "generated": datetime.now().isoformat(timespec="seconds"),
        "seed": seed,
        "mode": mode,
        "anon_to_tag": {v: k for k, v in anon_of.items()},
        "warning": "评卷人请勿查看本文件；它会在评分时揭示模型身份。",
    }
    key_path.write_text(json.dumps(key, ensure_ascii=False, indent=2), encoding="utf-8")

    rows = []
    counted = 0
    # 抽样：每个意图簇随机取 N 题，避免「只抽到前面的题」造成偏差
    eligible = [eid for eid in sorted(meta)
                if include_functional or meta[eid]["split"] != "functional"]
    if limit_per_cluster > 0:
        by_cl: dict[str, list[int]] = defaultdict(list)
        for eid in eligible:
            by_cl[meta[eid]["cluster"]].append(eid)
        rng = random.Random(seed)
        chosen = set()
        for cl, ids in by_cl.items():
            chosen.update(rng.sample(ids, min(limit_per_cluster, len(ids))))
        eligible = [eid for eid in eligible if eid in chosen]
    sample_note = (f"　抽样：每簇 {limit_per_cluster} 题" if limit_per_cluster > 0 else "")
    md = [
        "# 慕心 · 人工盲评表",
        "",
        f"生成时间：{datetime.now():%Y-%m-%d %H:%M}　输入模式：`{mode}`　模型数：{len(tags)}{sample_note}",
        "",
        f"**评分标准**：{SCORE_GUIDE}",
        "",
        "不要猜测哪个字母是哪个模型；只看「这句话回复得怎么样」。",
        f"分数填进 `output/{prefix}.csv` 的 `score` 列，然后运行 "
        f"`--collect output/{prefix}.csv --prefix {prefix}`。",
        "",
    ]
    for eid in eligible:
        m = meta[eid]
        counted += 1
        md.append(f"## #{eid} · {m['cluster']} · {esc(m['instruction'])}")
        md.append("")
        if m["input"]:
            md.append(f"- 上下文：{esc(m['input'])}")
        md.append(f"- 期望：{esc(m['expect'])}")
        md.append("")
        md.append("| 编号 | 回复 | 得分(0/1/2) |")
        md.append("|---|---|---|")
        for tag in tags:
            a = anon_of[tag]
            reply = answers[eid].get(tag, "")
            rows.append({
                "item_id": f"{eid}-{a}",
                "eid": eid,
                "anon": a,
                "cluster": m["cluster"],
                "split": m["split"],
                "instruction": m["instruction"],
                "expect": m["expect"],
                "reply": reply,
                "score": "",
                "note": "",
            })
            md.append(f"| {a} | {esc(reply)} | 　 |")
        md.append("")

    md_path.write_text("\n".join(md), encoding="utf-8")
    with csv_path.open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)
    html_path = write_html(rows, meta, prefix)
    print(f"[blind] 参评 {counted} 题 × {len(tags)} 模型 = {len(rows)} 条待评"
          f"（题库 {len(meta)} 题，functional 默认不计入{sample_note}）")
    print(f"  打分页: {html_path}  ← 推荐，点选 0/1/2 后下载 CSV")
    print(f"  题目表: {md_path}")
    print(f"  填写表: {csv_path}")
    print(f"  密钥:   {key_path}（评卷人勿看）")
    print(f"  回填:   python scripts/make_blind_review.py --collect {csv_path.relative_to(ROOT)}"
          f" --prefix {prefix}")


def refresh_html(prefix: str = "ALL") -> None:
    """只重刷打分页 HTML，不动 CSV 与匿名密钥。

    打分页的分数存在浏览器 localStorage（key = "blind_" + prefix），
    只要 prefix 不变，刷新页面后已填的分数还在。这里刻意**不重新洗牌**，
    否则新的匿名映射会让用户此前填的分全部对不上号。
    """
    prefixes = ([p.stem for p in sorted(OUT.glob("*.html"))
                 if (OUT / f"{p.stem}.csv").exists() and (OUT / f"{p.stem}_key.json").exists()]
                if prefix == "ALL" else [prefix])
    # 上下文只用于显示，从两套题库按题面回补；匹配不到就留空
    inputs: dict[str, str] = {}
    for q in (ROOT / "data/eval/dev_v2.jsonl", ROOT / "data/eval/real_usage.jsonl"):
        if q.exists():
            for line in q.read_text(encoding="utf-8").splitlines():
                if line.strip():
                    r = json.loads(line)
                    inputs[r["instruction"]] = r.get("input", "")
    for pfx in prefixes:
        csv_path = OUT / f"{pfx}.csv"
        if not csv_path.exists():
            print(f"[blind] 跳过 {pfx}：找不到 {csv_path.name}")
            continue
        with csv_path.open("r", encoding="utf-8-sig", newline="") as f:
            rows = list(csv.DictReader(f))
        meta = {
            int(r["eid"]): {
                "instruction": r["instruction"],
                "input": inputs.get(r["instruction"], ""),
                "expect": r["expect"],
                "cluster": r["cluster"],
                "split": r["split"],
            }
            for r in rows
        }
        out = write_html(rows, meta, pfx)
        print(f"[blind] 已刷新 {out.name}（{len(rows)} 条，匿名映射未改动）")


def apply_paste(text: str) -> None:
    """解析打分页「复制分数」产出的紧凑文本，写回 CSV 并立即回填统计。"""
    t = (text or "").strip()
    m = re.match(r"^BLIND_V1\|([A-Za-z0-9_\-]+)\|(\d+)/(\d+)\s*\n(.+)$", t, re.S)
    if not m:
        raise SystemExit("[blind] 粘贴内容格式不对：需以 BLIND_V1|<表名>|已填/总数 开头，"
                         "换行后跟 1-A=2,1-B=0,... 这样的分数")
    prefix, done, total, body = m.group(1), int(m.group(2)), int(m.group(3)), m.group(4)
    scores: dict[str, str] = {}
    for item in body.replace("\n", "").replace("\r", "").split(","):
        item = item.strip()
        if not item:
            continue
        if "=" not in item:
            raise SystemExit(f"[blind] 分数片段格式不对: {item!r}")
        k, v = item.split("=", 1)
        k, v = k.strip(), v.strip()
        if v not in ("0", "1", "2"):
            raise SystemExit(f"[blind] 分数只能是 0/1/2，收到 {v!r}（{k}）")
        scores[k] = v
    csv_path = OUT / f"{prefix}.csv"
    if not csv_path.exists():
        raise SystemExit(f"[blind] 找不到 {csv_path}，表名 {prefix!r} 对不上")
    with csv_path.open("r", encoding="utf-8-sig", newline="") as f:
        rows = list(csv.DictReader(f))
    unknown = set(scores) - {r["item_id"] for r in rows}
    if unknown:
        raise SystemExit(f"[blind] 有 {len(unknown)} 个编号不属于这张表: {sorted(unknown)[:5]}；"
                         "可能复制了别的打分页，或表已重新生成过")
    for r in rows:
        r["score"] = scores.get(r["item_id"], "")
    with csv_path.open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)
    print(f"[blind] 已写入 {len(scores)} 条分数到 {csv_path.name}"
          f"（共 {len(rows)} 条；页面显示 {done}/{total}）")
    collect(str(csv_path), prefix)


def collect(csv_path: str, prefix: str = "blind_review") -> None:
    key_path = OUT / f"{prefix}_key.json"
    scores_path = OUT / f"{prefix}_scores.md"
    key = json.loads(key_path.read_text(encoding="utf-8"))
    anon_to_tag = key["anon_to_tag"]
    p = Path(csv_path)
    with p.open("r", encoding="utf-8-sig", newline="") as f:
        rows = list(csv.DictReader(f))
    scored = [r for r in rows if str(r.get("score", "")).strip() in ("0", "1", "2")]
    if not scored:
        print("[blind] 没有任何已填的 score（只允许 0/1/2），先填表再回填")
        return
    agg = defaultdict(list)
    by_cluster = defaultdict(lambda: defaultdict(list))
    by_source: dict[str, dict[str, list[int]]] = defaultdict(lambda: defaultdict(list))
    src_map = source_map(list(anon_to_tag.values()), key["mode"])
    has_source = any(v is not None for v in src_map.values())
    for r in scored:
        tag = anon_to_tag.get(r["anon"], "?")
        s = int(str(r["score"]).strip())
        agg[tag].append(s)
        by_cluster[r["cluster"]][tag].append(s)
        label = "程序化" if src_map.get((tag, int(r["eid"]))) else "模型"
        by_source[tag][label].append(s)
    lines = [
        "# 慕心 · 人工盲评结果",
        "",
        f"回填时间：{datetime.now():%Y-%m-%d %H:%M}　已评 {len(scored)}/{len(rows)} 条"
        f"　输入模式：`{key['mode']}`",
        "",
        f"评分标准：{SCORE_GUIDE}",
        "",
        "## 总体（满分 2）",
        "",
        "| 模型 | 已评条数 | 平均分 | 合格率(2分占比) | 不合格率(0分占比) |",
        "|---|---:|---:|---:|---:|",
    ]
    for tag, ss in sorted(agg.items(), key=lambda kv: -sum(kv[1]) / len(kv[1])):
        ok = sum(1 for s in ss if s == 2) / len(ss) * 100
        bad = sum(1 for s in ss if s == 0) / len(ss) * 100
        lines.append(f"| {tag} | {len(ss)} | {sum(ss) / len(ss):.2f} | {ok:.1f}% | {bad:.1f}% |")
    lines += ["", "## 按意图簇（平均分）", "", "| 簇 | " +
              " | ".join(sorted(agg)) + " |", "|---|" + "---|" * len(agg)]
    for cl, d in sorted(by_cluster.items()):
        cells = []
        for tag in sorted(agg):
            ss = d.get(tag, [])
            cells.append(f"{sum(ss) / len(ss):.2f}" if ss else "—")
        lines.append(f"| {cl} | " + " | ".join(cells) + " |")
    lines += [
        "", "## 按回复来源（重要）", "",
        "| 模型 | 来源 | 已评条数 | 平均分 | 合格率(2分占比) | 不合格率(0分占比) |",
        "|---|---|---:|---:|---:|---:|",
    ]
    for tag in sorted(by_source):
        for label in ("模型", "程序化"):
            ss = by_source[tag].get(label, [])
            if not ss:
                continue
            ok = sum(1 for s in ss if s == 2) / len(ss) * 100
            bad = sum(1 for s in ss if s == 0) / len(ss) * 100
            lines.append(f"| {tag} | {label} | {len(ss)} | {sum(ss) / len(ss):.2f} | "
                         f"{ok:.1f}% | {bad:.1f}% |")
    if has_source:
        lines += [
            "",
            "> 「程序化」是规则生成的回复，**不算模型能力**，也不参与版本优劣比较；",
            "> 判断某一个模型本身有没有变好，只看「模型」那几行。"
            "上面的总体表含程序化回复，仅供参考，别拿它当模型分数。",
        ]
    else:
        lines += ["", "> 本次参评明细没有回复来源字段（旧版产物），全部按模型计。"]
    scores_path.write_text("\n".join(lines), encoding="utf-8")
    print("\n".join(lines))
    print(f"\n[blind] 已写入 {scores_path}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--tags", nargs="+", default=[], help="eval_responses_<tag>.jsonl 的 tag 列表")
    ap.add_argument("--mode", default="runtime", choices=["plain", "runtime"])
    ap.add_argument("--seed", type=int, default=20261114)
    ap.add_argument("--include-functional", action="store_true", help="连提醒类功能回归题一起盲评")
    ap.add_argument("--limit-per-cluster", type=int, default=0,
                    help="每个意图簇随机抽 N 题，生成小体量试手表（0=全部）")
    ap.add_argument("--collect", default=None, help="回填已填好分数的 CSV")
    ap.add_argument("--paste", default=None, help="粘贴打分页「复制分数」的文本，自动写回并回填")
    ap.add_argument("--refresh-html", nargs="?", const="ALL", default=None,
                    help="只刷新打分页 HTML（可指定单个 prefix，默认全部），不动 CSV 与匿名密钥")
    ap.add_argument("--prefix", default="blind_review",
                    help="输出文件名前缀，多套盲评表并存时用（如 real_review）")
    args = ap.parse_args()
    OUT.mkdir(parents=True, exist_ok=True)
    if args.refresh_html:
        refresh_html(args.refresh_html)
    elif args.paste:
        apply_paste(args.paste)
    elif args.collect:
        collect(args.collect, args.prefix)
    elif args.tags:
        build(args.tags, args.mode, args.seed, args.include_functional,
              args.prefix, args.limit_per_cluster)
    else:
        ap.error("需要 --tags 生成盲评表，或 --paste / --collect 回填分数")


if __name__ == "__main__":
    main()
