#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""探针: 回测采样网格漂移对「指标日间波动」的定量影响(2026-10-21 审计用, 非门禁)。

只跑不依赖类比库的 momentum 基线, 因此可在十几秒内给出读数, 无需等整条管线。
用法: python probe_grid_drift_impact.py
自校验: 先用修复前的网格复算, 必须复现「相邻两天样本交集为 0」这一已知事实,
        否则本次读数作废。
"""
import io
import json
import os
import sys

import numpy as np

BASE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE)
import obos_core as oc  # noqa: E402


def old_grid(T, H=30, BT=900, FLOOR=320):
    te = T - H - 1
    return list(range(max(FLOOR, te - BT), te + 1, H))


def load_scores():
    b = json.load(io.open(os.path.join(BASE, "data", "benchmark.json"), encoding="utf-8"))
    k = json.load(io.open(os.path.join(BASE, "data", "industry_klines.json"), encoding="utf-8"))
    ref = b["dates"]
    n_t = len(ref)
    ind_meta, closes_all, codes = [], [], []
    for code, v in k.items():
        cmap = {r[0]: r[2] for r in v["rows"]}
        close = [cmap.get(d) for d in ref]
        rs, pos, bias = oc.sub_indicators(close)
        ind_meta.append({"rs": rs, "pos": pos, "bias": bias})
        closes_all.append(close)
        codes.append(code)
    segs = oc.walkforward_weights(ind_meta, closes_all, n_t)
    S = np.full((len(codes), n_t), np.nan)
    for i in range(len(codes)):
        sc = oc.make_score_pit(ind_meta[i]["rs"], ind_meta[i]["pos"],
                               ind_meta[i]["bias"], segs, n_t, smooth=True)
        for t, x in enumerate(sc):
            if x is not None:
                S[i, t] = x
    return S, ref, codes


def dir_acc_at(S, tpts, H=30):
    """momentum 基线的块级方向准确率(与 run_backtest 同口径, 非重叠块, |变动|>=1.0 才算方向)"""
    NK, T = S.shape
    ok, nn = 0, 0
    for t in tpts:
        if t + H > T - 1:
            continue
        for k in range(NK):
            s_t = S[k, t]
            if not np.isfinite(s_t):
                continue
            real = S[k, t + 1:t + 1 + H]
            if len(real) < H or not np.isfinite(real).all():
                continue
            r = oc.base_momentum(S, k, t, H)
            if r is None:
                continue
            med = r[0]
            if abs(real[-1] - s_t) < 1.0:
                continue
            nn += 1
            if abs(med[-1] - s_t) > 1e-9 and np.sign(med[-1] - s_t) == np.sign(real[-1] - s_t):
                ok += 1
    return (ok / nn if nn else float("nan")), nn


def main():
    S, ref, codes = load_scores()
    NK, T0 = S.shape
    print("数据: %d 个行业 x %d 个交易日 (%s ~ %s)" % (NK, T0, ref[0], ref[-1]))

    # 自校验: 必须复现「旧网格相邻两天交集为空」
    a, b = set(old_grid(T0)), set(old_grid(T0 + 1))
    if a & b:
        print("!! 自校验失败: 旧网格两天仍有交集 %d, 与已知事实不符 -> 读数作废" % len(a & b))
        return 2
    print("自校验通过: 旧网格相邻两天交集为 0(与代码审查结论一致)\n")

    DAYS = 31
    old_series, new_series = [], []
    for d in range(DAYS):
        T = T0 + d
        g_old = [t for t in old_grid(T) if t + 30 <= T - 1]
        g_new = [t for t in oc.backtest_timepoints(T) if t + 30 <= T - 1]
        vo, _ = dir_acc_at(S, g_old)
        vn, _ = dir_acc_at(S, g_new)
        old_series.append(vo)
        new_series.append(vn)

    def stat(name, v):
        v = [x for x in v if np.isfinite(x)]
        dd = [abs(v[i] - v[i - 1]) for i in range(1, len(v))]
        print("%-14s n=%-3d 均值=%.4f 最小=%.4f 最大=%.4f  |日间Δ|均值=%.4f 最大=%.4f  σ=%.4f"
              % (name, len(v), np.mean(v), np.min(v), np.max(v),
                 np.mean(dd) if dd else 0, np.max(dd) if dd else 0,
                 float(np.std(dd)) if len(dd) > 1 else 0))

    print("模拟连续 %d 个交易日, 每天用当日网格重算 momentum 方向准确率:" % DAYS)
    stat("旧网格(漂移)", old_series)
    stat("新网格(锚定)", new_series)
    ov = [x for x in old_series if np.isfinite(x)]
    nv = [x for x in new_series if np.isfinite(x)]
    if len(ov) > 2 and len(nv) > 2:
        so = np.std([abs(ov[i] - ov[i - 1]) for i in range(1, len(ov))])
        sn = np.std([abs(nv[i] - nv[i - 1]) for i in range(1, len(nv))])
        print("\n日间波动标准差: 旧 %.4f -> 新 %.4f  (降低 %.0f%%)"
              % (so, sn, 100 * (1 - sn / so) if so else 0))
    return 0


if __name__ == "__main__":
    sys.exit(main())
