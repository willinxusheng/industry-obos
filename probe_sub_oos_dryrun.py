#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""R10 故障注入：二级看板 OOS 接线干跑验证。
背景：R9 给二级看板新增了 sub_prediction_log.jsonl（2026-11-06 起），
但 OOS 成熟度要等 30 个交易日（约 12 月中旬）。如果接线有 bug（code 对不上、
日期轴错位、字段缺失），要到 12 月才发现 —— 损失一个月证据。
方法：用 horizon=5 的缩短口径干跑 audit_rows，验证「机制」能匹配出成熟样本。
这不会进任何产物，纯探针。
"""
import io, json, os, re, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from oos_audit import audit_rows, load_log

BASE = os.path.dirname(os.path.abspath(__file__))

def load_js_json(path, varname=None):
    """解析 var X = {...}; 结构；多变量文件需指定 varname。"""
    s = io.open(path, encoding="utf-8").read()
    if varname:
        m = re.search(r"^var %s = " % re.escape(varname), s, re.M)
        if not m:
            raise KeyError(varname)
        start = s.find("{", m.end() - 1)
    else:
        start = s.find("{")
    # 平衡大括号提取单个 JSON 对象
    depth, end, in_str, esc = 0, start, False, False
    for i in range(start, len(s)):
        ch = s[i]
        if in_str:
            if esc: esc = False
            elif ch == "\\": esc = True
            elif ch == '"': in_str = False
        else:
            if ch == '"': in_str = True
            elif ch == "{": depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    end = i + 1
                    break
    return json.loads(s[start:end])

# 1) sub 日期轴与分数序列（从已提交产物恢复）
sub_data = load_js_json(f"{BASE}/sub_data.js", "DATA")
print("sub_data 顶层 keys:", list(sub_data.keys())[:10])
sub_dates = None
bm = sub_data.get("benchmark")
if isinstance(bm, dict) and "dates" in bm:
    sub_dates = bm["dates"]
if sub_dates is None:
    # 回退：从 market.recent_dates 无法取全轴，报错
    print("FATAL: sub_data.js 无 benchmark.dates，无法构造日期轴")
    sys.exit(1)
print("sub 日期轴:", sub_dates[0], "~", sub_dates[-1], len(sub_dates))

sub_series = load_js_json(f"{BASE}/sub_series.js")
scores = {c: v["score"] for c, v in sub_series.items()
          if isinstance(v, dict) and "score" in v}
print("sub_series 行业数:", len(scores))

# 2) 字段完整性：sub 日志每行必须带 s/med/lo/hi/p_up
rows = load_log(f"{BASE}/data/sub_prediction_log.jsonl")
print("sub 日志行数:", len(rows))
need = ("s", "med", "lo", "hi", "p_up", "code", "asof")
bad = [r for r in rows if any(r.get(k) is None for k in need)]
print("字段缺失行数:", len(bad), bad[:2] if bad else "")

# 3) code 匹配率：日志 code 是否都在 sub_series 里
log_codes = {r["code"] for r in rows}
unmatched = log_codes - set(scores)
print("日志 code 数:", len(log_codes), "| 无法匹配分数序列:", len(unmatched), sorted(unmatched)[:5])

# 4) asof 都在日期轴上？
idx = set(sub_dates)
bad_asof = [r["asof"] for r in rows if r["asof"] not in idx]
print("asof 不在日期轴的行数:", len(bad_asof), sorted(set(bad_asof))[:5])

# 5) 干跑：horizon=5（注入缩短周期，验证匹配机制）
r5 = audit_rows(rows, sub_dates, scores, horizon=5, min_n=1)
print("\n=== horizon=5 干跑 ===")
print(json.dumps({k: v for k, v in r5.items() if k != "by_regime"}, ensure_ascii=False))
if r5.get("matured", 0) == 0:
    print("❌ FAIL: 缩短到 5 天仍 0 行成熟 —— 接线有 bug，12 月也等不到证据")
    sys.exit(1)
n_asof = r5.get("n_asof", 0)
print(f"✅ PASS: {r5['matured']} 行成熟 / {n_asof} 个 asof —— 接线机制有效")
print("（dir/coverage 数值本身无意义，仅验证匹配；真实 OOS 仍等 30 交易日）")
