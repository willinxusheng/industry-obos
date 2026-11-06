# -*- coding: utf-8 -*-
"""探针: 序列起点少 K 根 K 线, 回测指标会变多少?

背景: fetch 请求 1300 根, 接口有时回 1301 根。backtest_timepoints() 的网格锚定在
【绝对索引】320 上, 于是多/少一根前导 K 线 = 整个 30 日网格平移 1 天 = 换一批样本。
线上 2026-11-06 构建(n_dates=1301)与本地重取(1300)同名指标: dir_acc 0.660 vs 0.681、
block_t 4.44 vs 4.94、mae 15.97 vs 15.66 —— 同一 asof、同一数据源, 差异全部来自起点。

用法: python probe_lead_drop.py
"""
import os
import sys

import numpy as np

BASE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE)

from obos_core import (HORIZON, AnalogLib, make_score_pit, run_backtest,
                       sub_indicators, backtest_timepoints, BT_FLOOR)
from backtest_deep import load

raw, hs, ref_dates, bclose, base, segs, S0 = load()
K = len(base)
n_t0 = len(ref_dates)
print("原始: %d 日 %s → %s" % (n_t0, ref_dates[0], ref_dates[-1]))
print("%-6s %-12s %-9s %-8s %-8s %-8s %-8s" %
      ("drop", "起点", "dir_acc", "block_t", "mae_end", "cov_raw", "n_tpts"))

for drop in (0, 1, 2, 3, 5, 10):
    S = S0[:, drop:]
    bclose2 = bclose[drop:]
    ref2 = ref_dates[drop:]
    n_t = len(ref2)
    m_rs, m_pos, m_bias = sub_indicators(bclose2)
    mkt = np.array([x if x is not None else np.nan for x in
                    make_score_pit(m_rs, m_pos, m_bias, segs, n_t, smooth=True)],
                   dtype=float)
    S2 = np.full((2 * K + 1, n_t), np.nan)
    S2[:K] = S
    S2[K] = mkt
    base2 = []
    for b in base:
        b2 = dict(b)
        b2["score"] = b["score"][drop:]
        base2.append(b2)
    beta_map = {}
    for k in range(K):
        a, b = S[k], mkt
        msk = np.isfinite(a) & np.isfinite(b)
        if msk.sum() < 120:
            continue
        bb = b[msk] - b[msk].mean()
        aa = a[msk] - a[msk].mean()
        den = float((bb ** 2).sum())
        beta = float((bb * aa).sum() / den) if den > 1e-12 else 0.0
        C = float(a[msk].mean()) - beta * float(b[msk].mean())
        S2[K + 1 + k] = a - beta * b - C
        beta_map[k] = (beta, C)
    lib = AnalogLib(S2, mkt_idx=K, idio_of=lambda kk: K + 1 + kk, beta_map=beta_map)
    lib_ind = AnalogLib(S)
    bt, cals = run_backtest(S, lib_ind, lib_mkt=lib, dates=ref2, industry_rows=range(K))
    c = bt.get("combo", {})
    print("%-6d %-12s %-9.3f %-8.2f %-8.2f %-8.3f %-8d" %
          (drop, ref2[0], c.get("dir_acc") or 0, c.get("block_t") or 0,
           c.get("mae_end") or 0, c.get("coverage_raw") or 0,
           len(backtest_timepoints(n_t))))
    if drop == 0:
        z0 = c.get("dir_acc")
    elif drop == 1:
        print("   -> 仅少 1 根前导 K 线, dir_acc 变动 %+0.3f (%.1fpp)"
              % ((c.get("dir_acc") or 0) - (z0 or 0), 100 * ((c.get("dir_acc") or 0) - (z0 or 0))))
