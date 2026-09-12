# -*- coding: utf-8 -*-
"""官方口径 walk-forward 复现 —— 等价于 compute.py 的阶段5, 用于在最新数据上核验线上模型指标.

与 compute.py 的唯一差别: 不写任何产物, 只打印. 这样可以在不影响 data/ 的前提下,
用不同 FC_K (近邻数) 重跑, 做参数敏感度实验.

用法:  FC_K=20 python bt_official.py
"""
import os
import sys

import numpy as np

BASE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE)

from obos_core import (BT_BACK, FC_K, FC_MPI, FC_SEP, HORIZON, AnalogLib,
                       run_backtest, sub_indicators, make_score_pit)
from backtest_deep import load

METHODS = ["combo", "knn", "combo_mkt", "meanrev", "momentum", "randomwalk",
           "persist"]


def main():
    raw, hs, ref_dates, bclose, base, segs, S = load()
    # 受控对比: CUT_DATE=YYYY-MM-DD 把样本截断到该日(含), 用于分离
    # "数据窗口滚动的效应" 与 "新增交易日的效应".
    cut = os.environ.get("CUT_DATE")
    if cut:
        idx = max(i for i, d in enumerate(ref_dates) if d <= cut)
        ref_dates = ref_dates[:idx + 1]
        bclose = bclose[:idx + 1]
        S = S[:, :idx + 1]
        for b in base:
            b["close"] = b["close"][:idx + 1]
            b["score"] = b["score"][:idx + 1]
    n_t, K = len(ref_dates), len(base)
    print("=" * 86)
    print("官方口径 walk-forward 复现 | 数据 %d 行业 x %d 日 (%s -> %s)"
          % (K, n_t, ref_dates[0], ref_dates[-1]))
    if cut:
        print(">>> CUT_DATE=%s 生效: 样本已截断, 用于与历史报告做受控对比" % cut)
    print("超参: FC_K=%d  FC_SEP=%d  FC_MPI=%d  HORIZON=%d  BT_BACK=%d"
          % (FC_K, FC_SEP, FC_MPI, HORIZON, BT_BACK))
    print("=" * 86)

    # --- 复现 compute.py 的 S2(含市场行 + 特质残差行) 与 beta_map ---
    m_rs, m_pos, m_bias = sub_indicators(bclose)
    mkt = np.array([x if x is not None else np.nan for x in
                    make_score_pit(m_rs, m_pos, m_bias, segs, n_t, smooth=True)],
                   dtype=float)
    S2 = np.full((2 * K + 1, n_t), np.nan)
    S2[:K] = S
    S2[K] = mkt
    beta_map = {}
    for k in range(K):
        a, b = S[k], mkt
        msk = np.isfinite(a) & np.isfinite(b)
        if msk.sum() < 120:
            beta_map[k] = (0.0, float(np.nanmean(a)) if msk.any() else 0.0)
        else:
            bv = b[msk] - np.nanmean(b[msk])
            av = a[msk] - np.nanmean(a[msk])
            den = float(np.nansum(bv ** 2))
            beta = float(np.nansum(bv * av) / den) if den > 1e-12 else 0.0
            Cc = float(np.nanmean(a[msk])) - beta * float(np.nanmean(b[msk]))
            S2[K + 1 + k] = a - beta * b - Cc
            beta_map[k] = (beta, Cc)

    print("building analog libraries ...", flush=True)
    lib_ind = AnalogLib(S)
    lib = AnalogLib(S2, mkt_idx=K, idio_of=lambda kk: K + 1 + kk,
                    beta_map=beta_map)
    print("segments ind/mkt: %d / %d" % (lib_ind.M, lib.M), flush=True)

    print("running walk-forward backtest ...", flush=True)
    bt, cals = run_backtest(S, lib_ind, lib_mkt=lib, dates=ref_dates,
                            industry_rows=range(K))

    print("\n--- 各方法指标 (方向=预测终点变动方向与实际的符号一致率) ---")
    print("%-12s | %8s | %8s | %8s | %8s | %9s | %6s"
          % ("方法", "方向%", "块级t", "MAE", "RMSE", "校准覆盖", "样本n"))
    for m in METHODS:
        o = bt.get(m)
        if not o:
            continue
        da = o.get("dir_acc")
        print("%-12s | %8s | %8s | %8.2f | %8.2f | %9s | %6d"
              % (m, ("%.1f%%" % (100 * da)) if da is not None else "n/a",
                 ("%+.2f" % o["block_t"]) if o.get("block_t") is not None else "n/a",
                 o.get("mae_end") or float("nan"), o.get("rmse_path") or float("nan"),
                 ("%.1f%%" % (100 * o["coverage_cal"])) if o.get("coverage_cal") is not None else "n/a",
                 o.get("n") or 0))

    print("\np_up AUC (knn): %s" % bt.get("p_up_auc"))
    print("主推演方法: %s" % bt.get("diag_on"))
    print("分年度胜率: %s" % bt.get("year_win_rate"))
    print("最差年度: %s" % bt.get("worst_year"))

    dm = bt.get("dm_combo_vs_persist") or {}
    print("DM combo vs persist: gain_mae=%s t=%s p=%s"
          % (dm.get("gain_mae"), dm.get("dm_t"), dm.get("dm_p")))
    dmk = bt.get("dm_combo_vs_knn") or {}
    print("DM combo vs knn    : gain_mae=%s t=%s p=%s"
          % (dmk.get("gain_mae"), dmk.get("dm_t"), dmk.get("dm_p")))

    cd = bt.get("cov_diag") or []
    print("\n条件覆盖诊断(偏离 50% 最大的三档):")
    for r in cd[:3]:
        print("  %-16s 覆盖 %.1f%%  偏离 %.3f  Kupiec p=%s  n=%d"
              % (r["bin"], 100 * r["cov"], r["off"], r.get("kupiec_p"), r["n"]))

    print("\n全部顶层字段: %s" % sorted(bt.keys()))

    ys = bt.get("year_stability") or []
    if ys:
        print("\n分年度稳定性 (主推演方法 %s):" % bt.get("diag_on"))
        print("%-6s | %-10s | %-10s | %-8s | %-8s | %s"
              % ("年度", "方向%", "MAE", "RMSE", "覆盖%", "样本n"))
        for r in ys:
            da = r.get("dir_acc")
            print("%-6s | %-10s | %-10s | %-8s | %-8s | %s"
                  % (r.get("year"),
                     ("%.1f%%" % (100 * da)) if da is not None else "n/a",
                     ("%.2f" % r["mae"]) if r.get("mae") is not None else "n/a",
                     ("%.2f" % r["rmse"]) if r.get("rmse") is not None else "n/a",
                     ("%.1f%%" % (100 * r["cov"])) if r.get("cov") is not None else "n/a",
                     r.get("n")))
    print("=" * 86)


if __name__ == "__main__":
    main()
