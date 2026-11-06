# -*- coding: utf-8 -*-
"""探针: 经济含义(IC/多空)对回测网格相位的敏感性。

econ_diag 用的是同一族 31 个时点。虽然网格已按日历锚定(起点抖动不再换样本),
但"锚在哪个日历日"仍是一个自由度 —— 若换 5 天相位结论就翻符号, 那这个 IC 就是噪声,
不能对外披露成"弱正信号"。本脚本只换 BT_ANCHOR, 其余完全不变。
"""
import json
import os
import sys

import numpy as np

BASE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE)

import obos_core as oc
from backtest_deep import load

raw, hs, ref_dates, bclose, base, segs, S = load()
K = len(base)
n_t = len(ref_dates)
m_rs, m_pos, m_bias = oc.sub_indicators(bclose)
mkt = np.array([x if x is not None else np.nan for x in
                oc.make_score_pit(m_rs, m_pos, m_bias, segs, n_t, smooth=True)], dtype=float)
S2 = np.full((2 * K + 1, n_t), np.nan)
S2[:K] = S
S2[K] = mkt
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
lib = oc.AnalogLib(S2, mkt_idx=K, idio_of=lambda kk: K + 1 + kk, beta_map=beta_map)
closes = [b["close"] for b in base]

print("anchor      tpts  ic_mean   ic_t   spread%  spread_t  正IC占比")
for off in range(0, 6):
    # 用基准日历推算 anchor 日期
    i0 = next(i for i, d in enumerate(ref_dates) if d >= oc.BT_ANCHOR)
    oc.BT_ANCHOR = ref_dates[i0 + off]
    r = oc.econ_diag(S, lambda k, t: oc.base_combo_mkt(S, lib, k, t), closes, bclose,
                     dates=ref_dates, industry_rows=range(K))
    print("%-11s %-5s %+.4f  %+5.2f  %+6.2f  %+6.2f   %.2f"
          % (oc.BT_ANCHOR, r.get("n_pts"), r.get("ic_mean") or 0, r.get("ic_t") or 0,
             r.get("spread_pct") or 0, r.get("spread_t") or 0, r.get("ic_pos_share") or 0))
    oc.BT_ANCHOR = "2021-07-02"
