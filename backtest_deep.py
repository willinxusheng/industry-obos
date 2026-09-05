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
                       weights_from_ic, base_persist, base_combo, base_combo_mkt,
                       norm_sf)

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


def overlap_stats(pairs, h, h0=0.0):
    """逐日重叠采样的稳健汇总.

    pairs: [(t, value)] —— 前瞻窗口 h 日, 相邻样本互相重叠 h-1 日, 自相关极强。
    直接拿全样本 n 算 t 值会把显著性放大约 sqrt(h) 倍(有效样本仅约 n/h)。
    故: 均值用全样本(无偏且更稳), 显著性只用无重叠子样本(每 h 个取 1 个)。

    h0: 检验基准。收益类用 0(是否显著不为零); 胜率必须用 50 —— 拿 0 当基准
        只会得出"胜率显著不等于 0%"这种恒真且毫无意义的结论。
    """
    if not pairs:
        return None
    vals = [v for _, v in pairs]
    m = sum(vals) / len(vals)
    if len(pairs) < 5 * h:
        return {"mean": m, "n": len(vals), "sub_mean": None,
                "sub_n": 0, "t": None, "p": None}
    base_t = pairs[0][0]
    sub = [v for t, v in pairs if (t - base_t) % h == 0]
    if len(sub) < 5:
        return {"mean": m, "n": len(vals), "sub_mean": None,
                "sub_n": len(sub), "t": None, "p": None}
    sm = sum(sub) / len(sub)
    sd = (sum((x - sm) ** 2 for x in sub) / (len(sub) - 1)) ** 0.5
    if sd <= 1e-12:
        return {"mean": m, "n": len(vals), "sub_mean": sm,
                "sub_n": len(sub), "t": None, "p": None}
    tt = (sm - h0) / (sd / (len(sub) ** 0.5))
    return {"mean": m, "n": len(vals), "sub_mean": sm,
            "sub_n": len(sub), "t": tt, "p": norm_sf(tt)}


def fmt_stat(pairs, h, h0=0.0):
    """把 overlap_stats 格式化成紧凑串. t 显著偏离 h0 才标星."""
    st = overlap_stats(pairs, h, h0=h0)
    if st is None:
        return "%14s" % "-"
    if st["t"] is None:
        return "%8.2f (n=%d)" % (st["mean"], st["n"])
    star = "*" if st["p"] is not None and st["p"] < 0.05 else " "
    return "%7.2f(t=%+4.1f%s,n_eff=%d)" % (st["mean"], st["t"], star, st["sub_n"])


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
    print("\n===== [D1] 状态分档 -> 未来 N 日行业收益 =====")
    print("口径: 均值用全样本(无偏); 显著性只看无重叠子样本(每 h 日取 1 个) ——")
    print("      前瞻窗口重叠、相邻样本自相关极强, 拿全样本 n 算 t 值会显著夸大. * = p<0.05")
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
                res[st][h].append((t, r, ex))

    # 胜率原先算出来却从未打印(死代码), 这里一并纳入三张子表
    for title, pick, h0 in (("[绝对收益 %]", lambda t, r, e: r, 0.0),
                            ("[相对沪深300 超额 %]", lambda t, r, e: e, 0.0),
                            ("[胜率 % (检验基准 50%)]", lambda t, r, e: 100.0 if r > 0 else 0.0, 50.0)):
        print("\n%s" % title)
        print("%-4s | %s" % ("状态", " | ".join("未来%2dd (t, n_eff)" % h for h in H_LIST)))
        for st in ["超买", "偏热", "中性", "偏冷", "超卖"]:
            cells = []
            for h in H_LIST:
                arr = [(t, pick(t, r, e)) for (t, r, e) in res[st][h]
                       if pick(t, r, e) is not None]
                cells.append(fmt_stat(arr, h, h0=h0))
            print("%-4s | %s" % (st, " | ".join(cells)))

    # ============ [D2] 分数分桶 -> 下期收益 IC / 分桶均值 ============
    print("\n===== [D2] score 分桶(5分宽) -> 未来 20 日收益 =====")
    # 前瞻收益矩阵 Rm[h][k, t], 供截面 IC 与分桶复用
    Rm = {h: np.full((K, n_t), np.nan) for h in H_LIST}
    for k, b in enumerate(base):
        close = b["close"]
        for t in range(PIT_MIN_N, n_t - 30):
            for h in H_LIST:
                r = pct_ret(close, t, h)
                if r is not None:
                    Rm[h][k, t] = r
    buckets = {i: [] for i in range(0, 100, 5)}
    for t in range(PIT_MIN_N, n_t - 30):
        r20_col = Rm[20][:, t]
        if not np.isfinite(r20_col).any():
            continue
        mkt = float(np.nanmean(r20_col))     # 该时点全行业均值 = 时点效应(全市场同涨同跌)
        for k in range(K):
            cs = S[k, t]
            r20 = r20_col[k]
            if not np.isfinite(cs) or not np.isfinite(r20) or not (0 <= cs <= 100):
                continue
            buckets[min(95, int(cs // 5) * 5)].append((r20, r20 - mkt))
    print("%-8s | %9s | %18s | %6s" % ("桶", "绝对%", "截面中性化超额%", "n"))
    for bkey in range(0, 100, 5):
        arr = buckets[bkey]
        if len(arr) < 30:
            continue
        ra = [x[0] for x in arr]
        ne = [x[1] for x in arr]
        print("%3d-%-4d | %9.2f | %18.2f | %6d"
              % (bkey, bkey + 5, sum(ra) / len(ra), sum(ne) / len(ne), len(arr)))
    print("注: [绝对%] 含市场整体涨跌; [截面中性化超额] = 收益 - 该时点全行业均值,")
    print("    剔除时点效应后, 才反映『在同涨同跌里挑出相对更强行业』的能力.")

    # ---- 截面 IC(标准口径) ----
    from obos_core import spearman
    print("\n--- 截面 IC: 每个时点上对 K 个行业做横截面 spearman, 再按时点平均 ---")
    print("%-10s | %9s | %9s | %8s | %10s | %8s | %7s"
          % ("前瞻", "截面IC均值", "IC标准差", "ICIR", "t(无重叠)", "pooled旧口径", "时点数"))
    for h in H_LIST:
        series, px, py = [], [], []
        for t in range(PIT_MIN_N, n_t - 30):
            col_s, col_r = [], []
            for k in range(K):
                cs, r = S[k, t], Rm[h][k, t]
                if np.isfinite(cs) and np.isfinite(r) and 0 <= cs <= 100:
                    col_s.append(float(cs))
                    col_r.append(float(r))
                    px.append(float(cs))
                    py.append(float(r))
            if len(col_s) < 10:
                continue
            c = spearman(col_s, col_r)
            if c is not None and np.isfinite(c):
                series.append((t, c))
        if len(series) < 30:
            continue
        st = overlap_stats(series, h)
        ic_std = (sum((c - st["mean"]) ** 2 for _, c in series) / (len(series) - 1)) ** 0.5
        icir = st["mean"] / ic_std if ic_std > 1e-12 else float("nan")
        pooled = spearman(px, py)
        print("%-10s | %+9.4f | %9.4f | %+8.3f | %10s | %+12.4f | %7d"
              % ("未来%2d日" % h, st["mean"], ic_std, icir,
                 ("%+.2f%s" % (st["t"], "*" if (st["p"] or 1) < 0.05 else " "))
                 if st["t"] is not None else "n/a",
                 pooled if pooled is not None else float("nan"), len(series)))
    print("注: [pooled旧口径] 把 (行业,时点) 全混进一个池子算 spearman —— 它不是截面 IC,")
    print("    会把『某段行情全市场同涨/同跌』的时点效应也算成预测力, 且样本逐日重叠。")
    print("    两列差距越大, 说明表面预测力越多来自『跟对大盘』而非『选对行业』。")

    # ============ [D3] p_up 校准 ============
    print("\n===== [D3] p_up 概率校准 (knn 预测, 无重叠采样) =====")
    print("口径: 候选片段经 mask(e+H<=t) 过滤, 无前视泄漏; 但评估覆盖整个历史区间,")
    print("      属【样本内校准形态诊断】。样本外校准以 run_backtest 的 walk-forward")
    print("      p_up_calib(Brier/isotonic) 为准, 两者数值不同属正常。")
    lib = AnalogLib(S)
    bins = [(0.0, 0.35), (0.35, 0.45), (0.45, 0.55), (0.55, 0.65), (0.65, 1.01)]
    cal = {b: [0, 0, 0.0] for b in bins}    # [升温数, 样本数, p_up 累加]
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
                    cal[(lo, hi)][2] += p
                    break
    print("%-14s | %6s | %10s | %10s | %8s"
          % ("p_up 区间", "n", "实际升温率", "预测均值", "偏差"))
    tot_n = sum(v[1] for v in cal.values())
    ece = 0.0
    for (lo, hi), (u, n, ps) in cal.items():
        if n >= 20:
            acc, conf = u / n, ps / n
            ece += (n / tot_n) * abs(acc - conf) if tot_n else 0.0
            print("%.2f-%.2f   | %6d | %10.3f | %10.3f | %+8.3f"
                  % (lo, min(hi, 1.0), n, acc, conf, acc - conf))
    print("ECE(期望校准误差, 加权平均 |实际-预测|): %.4f  [越小越准, 0.05 内算良好]"
          % ece)
    print("注: 原实现以『桶中点』代表预测值, 对 0.00-0.35 这种宽桶误差很大")
    print("    (中点 0.17 与桶内真实预测均值可以差 0.1 以上), 故改用桶内预测均值。")

    # ============ [D4] 尾部行为 ============
    print("\n===== [D4] 极端尾部: score>=90 / <=10 的反转力度 =====")
    for cond, lab in [(lambda cs: cs >= 90, ">=90"), (lambda cs: cs <= 10, "<=10")]:
        for h in [5, 10, 20, 30]:
            pr, pw = [], []
            for k, b in enumerate(base):
                for t in range(PIT_MIN_N, n_t - 30):
                    cs = b["score"][t]
                    if cs is None or not cond(cs):
                        continue
                    r = pct_ret(b["close"], t, h)
                    if r is not None:
                        pr.append((t, r))
                        pw.append((t, 100.0 if r > 0 else 0.0))
            if not pr:
                continue
            sr, sw = overlap_stats(pr, h), overlap_stats(pw, h)
            if sr["t"] is None:
                tt = "t=n/a      "
            else:
                tt = "t=%+5.2f%s " % (sr["t"], "*" if (sr["p"] or 1) < 0.05 else " ")
            print("score %-4s 未来%2dd: 平均 %+7.2f%% (%sn_eff=%3d) | 胜率 %5.1f%% (n=%d)"
                  % (lab, h, sr["mean"], tt, sr["sub_n"], sw["mean"], len(pr)))


if __name__ == "__main__":
    main()
