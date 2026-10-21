#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""探针: PIT 扩张阈值对「序列起点」的敏感性(2026-10-21 审计用, 非门禁)。

背景: fetch 按"最近 1300 条"取数, 接口实际返回 1300 或 1301 条不等
(实测 10-16 产物 1301 条/起 2021-06-08, 10-21 重取 1300 条/起 2021-06-09)。
      score 本身 = f(rsi, pos, bias, weights):
        · rsi / pos / bias 都是固定窗口 -> 与序列起点无关(仅 min_n 边界附近受影响)
        · 权重走 walk-forward 且 EMA 平滑 -> 起点记忆在 ~100 步内衰减殆尽
      但【阈值】走 expanding_quantile(), 从序列第一个有效点开始累积 —— 起点一动,
      整条阈值序列的历史值就跟着动, state 分档可能因此翻转。
      本探针用产物里已存的 score 序列直接量化这一点(不重算 score, 秒级)。
用法: python probe_pit_start_sensitivity.py
"""
import io
import json
import os
import sys

BASE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE)
import obos_core as oc  # noqa: E402
import compute  # noqa: E402


def main():
    path = os.path.join(BASE, "data", "industry_obos.json")
    d = json.load(io.open(path, encoding="utf-8"))
    inds = d.get("industries") or []
    if not inds:
        print("!! 产物无 industries")
        return 2
    print("产物 asof=%s, %d 个行业, score 长度 %d"
          % (d.get("asof"), len(inds), len(inds[0].get("score") or [])))

    # 自校验: 对同一序列算两遍必须逐位一致(确定性), 否则读数作废
    s0 = [x for x in inds[0]["score"]]
    if oc.expanding_quantile(s0, oc.OB_Q) != oc.expanding_quantile(s0, oc.OB_Q):
        print("!! 自校验失败: expanding_quantile 非确定性")
        return 2
    print("自校验通过: expanding_quantile 对同一输入确定\n")

    print("%-5s %-9s %-10s %-10s %-12s %-10s" %
          ("后移", "n_点", "阈值max|Δ|", "阈值均值|Δ|", "越线翻转", "翻转率"))
    for dd in (1, 2, 3, 5, 10, 20, 40):
        tmax, tsum, tn = 0.0, 0.0, 0
        flip, seg, tot = 0, 0, 0
        for ind in inds:
            s = ind.get("score") or []
            if len(s) <= dd + 600:
                continue
            full_ob = oc.expanding_quantile(s, oc.OB_Q)
            full_os = oc.expanding_quantile(s, oc.OS_Q)
            sub = s[dd:]
            sub_ob = oc.expanding_quantile(sub, oc.OB_Q)
            sub_os = oc.expanding_quantile(sub, oc.OS_Q)
            for j, v in enumerate(sub):
                if v is None:
                    continue
                a_ob, b_ob = full_ob[dd + j], sub_ob[j]
                a_os, b_os = full_os[dd + j], sub_os[j]
                if a_ob is None or b_ob is None or a_os is None or b_os is None:
                    continue
                dmax = max(abs(a_ob - b_ob), abs(a_os - b_os))
                tmax = max(tmax, dmax)
                tsum += dmax
                tn += 1
                # 「越线翻转」: 相对阈值的不等号方向在两版之间反了 => state 分档可能变
                fa = (v >= a_ob, v <= a_os)
                fb = (v >= b_ob, v <= b_os)
                tot += 1
                if fa != fb:
                    flip += 1
        if tn:
            print("%-5d %-9d %-10.4f %-10.5f %-12d %.3f%%"
                  % (dd, tn, tmax, tsum / tn, flip, 100.0 * flip / tot if tot else 0))
    return 0


if __name__ == "__main__":
    sys.exit(main())
