# -*- coding: utf-8 -*-
"""深度回测 v1: 复用 compute.py 的 PIT 管线, 对 1300 日 x 31 行业历史做多维度验证.

维度:
  [D1] 信号有效性: 状态分档(超买/偏热/中性/偏冷/超卖) 出现后的未来 5/10/20/30 日
       行业指数收益(绝对) 与 相对沪深300超额, 验证均值回复假设是否成立、方向是否对称.
  [D2] 分数预测力: score 分桶(每5分) 的横截面下期收益 IC 与分桶收益, 检查单调性.
  [D3] 预测校准: p_up 概率校准(预测0.6实际升温频率), median 方向准确率(分年度/分行业).
  [D4] 极端样本: 分数>=90 / <=10 的尾部行为, 反转力度与持续时间.

只读数据与 obos_core/compute 的纯函数, 不写任何 data/ 产物.
"""
import json
import math
import os
import sys

import numpy as np

BASE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE)

from obos_core import (HORIZON, OB_Q, OS_Q, HOT_Q, COLD_Q, PIT_MIN_N,
                       W_SMOOTH, DEFAULT_W, IC_REBAL, FC_K, FC_MODE, FC_SEP, FC_MPI,
                       FWD, AnalogLib, regime_of,
                       expanding_quantile, future_trade_dates, ic_on_range, ma,
                       make_score_pit, pct_rank_series, pit_weight_path,
                       run_backtest, sub_indicators, walkforward_weights,
                       weights_from_ic, base_persist, base_combo, base_combo_mkt)

np.set_printoptions(suppress=True)


def load():
    with open(os.path.join(BASE, "data", "industry_klines.json"), encoding="utf-8") as f:
        raw = json.load(f)
    with open(os.path.join(BASE, "data", "benchmark.json"), encoding="utf-8") as f:
        hs = json.load(f)
    ref_dates = hs["dates"]
    bclose = hs["close"]
    n_t = len(ref_dates)
    base = []
    ind_meta = []
    closes_all = []
    for code, v in raw.items():
        cmap = {r[0]: r[2] for r in v["rows"]}
        close = [cmap.get(d) for d in ref_dates]
        rs, p_close, p_bias = sub_indicators(close)
        rel = [close[i] / bclose[i] if (close[i] and bclose[i]) else None for i in range(n_t)]
        ind_meta.append({"rs": rs, "pos": p_close, "bias": p_bias})
        closes_all.append(close)
        base.append({"code": code, "name": v["name"], "close": close,
                     "rs_pct": pct_rank_series(rel)})
    segs = walkforward_weights(ind_meta, closes_all, n_t)
    S = np.full((len(base), n_t), np.nan)
    for k, b in enumerate(base):
        sc = make_score_pit(ind_meta[k]["rs"], ind_meta[k]["pos"],
                            ind_meta[k]["bias"], segs, n_t, smooth=True)
        b["score"] = sc
        for i, v in enumerate(sc):
            if v is not None:
                S[k, i] = v
    return raw, hs, ref_dates, bclose, base, segs, S


def pct_ret(close, i, n):
    """close[i] -> close[i+n] 收益率(%); 数据不存在返回 None"""
    if i + n >= len(close) or close[i] in (None, 0) or close[i + n] is None:
        return None
    return (close[i + n] / close[i] - 1) * 100


def state_of(cs, ob, os_, hot, cold):
    if cs is None:
        return "-"
    if cs >= ob:
        return "超买"
    if cs <= os_:
        return "超卖"
    if cs >= hot:
        return "偏热"
    if cs <= cold:
        return "偏冷"
    return "中性"


def main():
    raw, hs, ref_dates, bclose, base, segs, S = load()
    n_t = len(ref_dates)
    K = len(base)
    print("数据: %d 行业 x %d 交易日 (%s -> %s)" % (K, n_t, ref_dates[0], ref_dates[-1]))

    # 每行业的 PIT 阈值线
    th = {}
    for k, b in enumerate(base):
        sc = b["score"]
        ob_s = expanding_quantile(sc, OB_Q)
        os_s = expanding_quantile(sc, OS_Q)
        hot_s = expanding_quantile(sc, HOT_Q)
        cold_s = expanding_quantile(sc, COLD_Q)
        th[k] = (ob_s, os_s, hot_s, cold_s)

    H_LIST = [5, 10, 20, 30]
    # ============ [D1] 状态分档未来收益 ============
    print("\n===== [D1] 状态分档 -> 未来 N 日行业收益(绝对%) / 超额(相对沪深300%) =====")
    # 只用 t+30 <= n_t-1 的样本(未来完整)
    res = {}
    for st in ["超买", "偏热", "中性", "偏冷", "超卖"]:
        res[st] = {h: [] for h in H_LIST}
    for k, b in enumerate(base):
        ob_s, os_s, hot_s, cold_s = th[k]
        close = b["close"]
        for t in range(PIT_MIN_N, n_t - 30):
            cs = b["score"][t]
            if cs is None or ob_s[t] is None:
                continue
            st = state_of(cs, ob_s[t], os_s[t], hot_s[t], cold_s[t])
            if st == "-":
                continue
            for h in H_LIST:
                r = pct_ret(close, t, h)
                rb = pct_ret(bclose, t, h)
                if r is None:
                    continue
                ex = (r - rb) if rb is not None else None
                res[st][h].append((r, ex))
    hdr = "%-4s | " + " | ".join("%12s" % h for h in ["%s" % h for h in H_LIST])
    print("%-4s | %s" % ("状态", " | ".join("未来%2dd 绝对/超额  (n)" % h for h in H_LIST)))
    for st in ["超买", "偏热", "中性", "偏冷", "超卖"]:
        cells = []
        for h in H_LIST:
            arr = [x for x in res[st][h]]
            if not arr:
                cells.append("%14s" % "-")
                continue
            ra = [x[0] for x in arr if x[0] is not None]
            ex = [x[1] for x in arr if x[1] is not None]
            m_abs = sum(ra) / len(ra) if ra else float("nan")
            m_ex = sum(ex) / len(ex) if ex else float("nan")
            win = sum(1 for x in ra if x > 0) / len(ra) if ra else float("nan")
            cells.append("%7.2f/%-6.2f (n=%d)" % (m_abs, m_ex, len(arr)))
        print("%-4s | %s" % (st, " | ".join(cells)))

    # ============ [D2] 分数分桶 -> 下期收益 IC / 分桶均值 ============
    print("\n===== [D2] score 分桶(5分宽) -> 未来 20 日绝对收益 / 超额 =====")
    buckets = {i: [] for i in range(0, 100, 5)}
    ics = {h: [] for h in H_LIST}
    for k, b in enumerate(base):
        close = b["close"]
        for t in range(PIT_MIN_N, n_t - 30):
            cs = b["score"][t]
            if cs is None or not (0 <= cs <= 100):
                continue
            for h in H_LIST:
                r = pct_ret(close, t, h)
                rb = pct_ret(bclose, t, h)
                if r is None:
                    continue
                ics[h].append((cs, r))
            r20 = pct_ret(close, t, 20)
            rb20 = pct_ret(bclose, t, 20)
            if r20 is not None:
                ex20 = (r20 - rb20) if rb20 is not None else None
                bucket = min(95, int(cs // 5) * 5)
                buckets[bucket].append((r20, ex20))
    # 桶均值
    print("%-6s | %8s | %8s | %6s" % ("桶", "绝对%", "超额%", "n"))
    for bkey in range(0, 100, 5):
        arr = buckets[bkey]
        if len(arr) < 30:
            continue
        ra = [x[0] for x in arr if x[0] is not None]
        ex = [x[1] for x in arr if x[1] is not None]
        print("%3d-%-3d | %8.2f | %8.2f | %6d" % (bkey, bkey + 5, sum(ra) / len(ra),
                                                  sum(ex) / len(ex) if ex else float("nan"), len(arr)))
    # Spearman IC
    for h in H_LIST:
        arr = ics[h]
        if len(arr) < 200:
            continue
        cs = [x[0] for x in arr]
        rs = [x[1] for x in arr]
        from obos_core import spearman
        ic = spearman(cs, rs)
        print("未来%2d日 截面 IC(spearman, 全部样本): %.4f (n=%d)" % (h, ic, len(arr)))

    # ============ [D3] p_up 校准 ============
    print("\n===== [D3] p_up 概率校准 (knn 预测, 无重叠采样) =====")
    lib = AnalogLib(S)
    bins = [(0.0, 0.35), (0.35, 0.45), (0.45, 0.55), (0.55, 0.65), (0.65, 1.01)]
    cal = {b: [0, 0] for b in bins}
    t_end = n_t - HORIZON - 1
    t_start = max(320, t_end - 900)
    for t in range(t_start, t_end + 1, HORIZON):
        for k in range(K):
            if not np.isfinite(S[k, t]):
                continue
            r = lib.forecast(k, t)
            if r is None or r.get("p_up") is None:
                continue
            real = S[k, t + 1:t + 1 + HORIZON]
            if len(real) < HORIZON or not np.isfinite(real).all():
                continue
            p = r["p_up"]
            up = 1 if real[-1] > S[k, t] else 0
            for lo, hi in bins:
                if lo <= p < hi:
                    cal[(lo, hi)][0] += up
                    cal[(lo, hi)][1] += 1
                    break
    print("%-14s | %6s | %8s | %8s" % ("p_up 区间", "n", "实际升温率", "预测中值"))
    for (lo, hi), (u, n) in cal.items():
        if n >= 20:
            print("%.2f-%.2f   | %6d | %8.3f | %8.2f" % (lo, min(hi, 1.0), n, u / n, (lo + min(hi, 1.0)) / 2))

    # ============ [D4] 尾部行为 ============
    print("\n===== [D4] 极端尾部: score>=90 / <=10 的反转力度 =====")
    for cond, lab in [(lambda cs: cs >= 90, ">=90"), (lambda cs: cs <= 10, "<=10")]:
        for h in [5, 10, 20, 30]:
            arr = []
            for k, b in enumerate(base):
                for t in range(PIT_MIN_N, n_t - 30):
                    cs = b["score"][t]
                    if cs is None or not cond(cs):
                        continue
                    r = pct_ret(b["close"], t, h)
                    if r is not None:
                        arr.append(r)
            if arr:
                print("score %s -> 未来%2dd: 平均 %6.2f%% | 胜率 %5.1f%% | n=%d"
                      % (lab, h, sum(arr) / len(arr), sum(1 for x in arr if x > 0) / len(arr) * 100, len(arr)))


if __name__ == "__main__":
    main()
