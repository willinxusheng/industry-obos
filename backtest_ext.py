# -*- coding: utf-8 -*-
"""深度回测 · 稳健性扩展 (backtest_deep.py 的补充维度).

backtest_deep.py 回答"信号是什么"; 本脚本回答"这些信号稳不稳、能不能当规则用":

  [E2] 分年度稳健性   —— 把 D1/D4 的关键结论按年度拆开, 检查是否由某一年(如 2024)主导.
  [E3] 市场状态 x 行业状态 —— 复现首轮"市场热+行业深冷最差", 并逐年度验证其稳定性.
  [E4] 阈值敏感度     —— 把 PIT 分位阈值(0.95/0.05/0.75/0.25)整体放宽/收窄,
                        检查 D1 的关键结论是否依赖这组具体数字(过拟合诊断).
  [E5] 校准指标的抽样稳定性 —— D3 的 ECE 是单次采样点估计, 用 bootstrap 给出其置信区间,
                        回答"ECE 从 0.0953 变到 0.0346 是校准变好了还是抽样噪声".

只读 data/, 不写任何产物. 口径与 backtest_deep.py 严格一致(无重叠子样本检验、超额对沪深300).
"""
import os
import sys

import numpy as np

BASE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE)

from obos_core import (HORIZON, OB_Q, OS_Q, HOT_Q, COLD_Q, PIT_MIN_N,
                       AnalogLib, expanding_quantile, make_score_pit,
                       regime_of, sub_indicators)
from backtest_deep import load, pct_ret, state_of, overlap_stats, fmt_stat

H_LIST = [5, 10, 20, 30]
STATES = ["超买", "偏热", "中性", "偏冷", "超卖"]


def build_returns(base, bclose, n_t):
    """R[h][k,t] 行业收益%; RB[h][t] 基准收益%; EX = R - RB(超额 pp)"""
    K = len(base)
    R = {h: np.full((K, n_t), np.nan) for h in H_LIST}
    for k, b in enumerate(base):
        for t in range(PIT_MIN_N, n_t - 30):
            for h in H_LIST:
                r = pct_ret(b["close"], t, h)
                if r is not None:
                    R[h][k, t] = r
    RB = {}
    for h in H_LIST:
        arr = np.full(n_t, np.nan)
        for t in range(PIT_MIN_N, n_t - 30):
            r = pct_ret(bclose, t, h)
            if r is not None:
                arr[t] = r
        RB[h] = arr
    EX = {h: R[h] - RB[h] for h in H_LIST}
    return R, RB, EX


def state_series(base, th):
    """逐 (k,t) 的状态矩阵(字符串), 与 backtest_deep 的 state_of 同源"""
    K = len(base)
    n_t = len(base[0]["score"])
    out = np.full((K, n_t), "", dtype=object)
    for k, b in enumerate(base):
        ob_s, os_s, hot_s, cold_s = th[k]
        for t in range(PIT_MIN_N, n_t - 30):
            cs = b["score"][t]
            if cs is None or ob_s[t] is None:
                continue
            out[k, t] = state_of(cs, ob_s[t], os_s[t], hot_s[t], cold_s[t])
    return out


def thresholds(base, obq, osq, hotq, coldq):
    th = {}
    for k, b in enumerate(base):
        sc = b["score"]
        th[k] = (expanding_quantile(sc, obq), expanding_quantile(sc, osq),
                 expanding_quantile(sc, hotq), expanding_quantile(sc, coldq))
    return th


def main():
    raw, hs, ref_dates, bclose, base, segs, S = load()
    n_t, K = len(ref_dates), len(base)
    years = [d[:4] for d in ref_dates]
    R, RB, EX = build_returns(base, bclose, n_t)
    th = thresholds(base, OB_Q, OS_Q, HOT_Q, COLD_Q)
    ST = state_series(base, th)

    print("=" * 78)
    print("深度回测 · 稳健性扩展")
    print("数据: %d 行业 x %d 交易日 (%s -> %s)"
          % (K, n_t, ref_dates[0], ref_dates[-1]))
    print("=" * 78)

    # ================= [E2] 分年度稳健性 =================
    print("\n===== [E2] 分年度稳健性: 关键结论是不是某一年撑起来的 =====")
    print("口径: 各年独立统计 未来20d 超额收益均值(pp) / 胜率%; n 为行业-日样本数.")
    print("      样本不足 50 的格子标 '-'.")

    keys = [("超卖", 20, "超卖 -> 20d"),
            ("偏冷", 20, "偏冷 -> 20d"),
            ("超买", 5, "超买 -> 5d"),
            ("偏热", 5, "偏热 -> 5d")]
    ylist = sorted(set(years))
    print("\n%-14s | %s" % ("信号", " | ".join("%-13s" % y for y in ylist)))
    for st, h, lab in keys:
        cells = []
        for y in ylist:
            arr = []
            for k in range(K):
                for t in range(PIT_MIN_N, n_t - 30):
                    if ST[k, t] != st or years[t] != y:
                        continue
                    e = EX[h][k, t]
                    if np.isfinite(e):
                        arr.append(e)
            cells.append("  n=%4d 超%+5.2f" % (len(arr), float(np.mean(arr)))
                         if len(arr) >= 50 else "     -        ")
        print("%-14s | %s" % (lab, " | ".join(cells)))

    # 尾部极值(分数字面值, 与 D4 同口径)
    print("\n%-14s | %s" % ("极值", " | ".join("%-13s" % y for y in ylist)))
    for cond, lab in ((lambda cs: cs <= 10, "score<=10 -> 20d"),
                      (lambda cs: cs >= 90, "score>=90 -> 20d")):
        cells = []
        for y in ylist:
            arr = []
            for k in range(K):
                for t in range(PIT_MIN_N, n_t - 30):
                    cs = base[k]["score"][t]
                    if cs is None or not cond(cs) or years[t] != y:
                        continue
                    e = EX[20][k, t]
                    if np.isfinite(e):
                        arr.append(e)
            cells.append("  n=%4d 超%+5.2f" % (len(arr), float(np.mean(arr)))
                         if len(arr) >= 50 else "     -        ")
        print("%-14s | %s" % (lab, " | ".join(cells)))

    # ================= [E3] 市场状态 x 行业状态 =================
    print("\n===== [E3] 市场状态 x 行业极端 -> 未来20d 超额 =====")
    print("市场状态由沪深300 自身的 PIT-分数分区定义(<40 冷 / 40-60 中 / >60 热),")
    print("只用到 t 时刻已知信息. 行业极端用分数原始值 (<=20 深冷 / >=80 深热).")
    m_rs, m_pos, m_bias = sub_indicators(bclose)
    mkt = np.array([x if x is not None else np.nan for x in
                    make_score_pit(m_rs, m_pos, m_bias, segs, n_t, smooth=True)],
                   dtype=float)
    print("沪深300 分数有效区间: %s -> %s"
          % (ref_dates[int(np.argmax(np.isfinite(mkt)))], ref_dates[-1]))

    mreg = np.full(n_t, "", dtype=object)
    for t in range(n_t):
        if np.isfinite(mkt[t]):
            mreg[t] = regime_of(mkt[t])

    # 「市场状态」有多种可辩护的定义, 首轮报告未固化口径(缺陷), 故这里并排三套,
    # 既避免"换了定义就说推翻旧结论", 也能定位旧数字究竟出自哪一套.
    mkt_list = [float(x) if np.isfinite(x) else None for x in mkt]
    mq_lo = expanding_quantile(mkt_list, COLD_Q)   # 25% 分位
    mq_hi = expanding_quantile(mkt_list, HOT_Q)    # 75% 分位
    mq_mid = expanding_quantile(mkt_list, 0.50)    # 中位数

    def _mk(pred):
        a = np.full(n_t, "", dtype=object)
        for t in range(n_t):
            if np.isfinite(mkt[t]):
                a[t] = pred(t)
        return a

    schemes = [
        ("A 绝对区间 (<40/40-60/>60)", mreg),
        ("B PIT 25/75 分位", _mk(lambda t: "cold" if (mq_lo[t] is not None and mkt[t] <= mq_lo[t])
                                 else ("hot" if (mq_hi[t] is not None and mkt[t] >= mq_hi[t]) else "mid"))),
        ("C PIT 中位数二分", _mk(lambda t: "cold" if (mq_mid[t] is not None and mkt[t] <= mq_mid[t])
                                 else "hot")),
    ]

    for slab, mreg_x in schemes:
        n_valid = sum(1 for t in range(PIT_MIN_N, n_t - 30) if mreg_x[t])
        print("\n--- 口径 %s | 有效时点 %d ---" % (slab, n_valid))
        print("%-10s | %-34s | %-34s"
              % ("市场状态", "行业深冷(<=20) 绝对/超额 胜率(n)", "行业深热(>=80) 绝对/超额 胜率(n)"))
        for rg, cn in (("cold", "偏冷"), ("mid", "中性"), ("hot", "偏热")):
            cells = []
            for cond in (lambda cs: cs <= 20, lambda cs: cs >= 80):
                ab, ex, win = [], [], []
                for k in range(K):
                    for t in range(PIT_MIN_N, n_t - 30):
                        if mreg_x[t] != rg:
                            continue
                        cs = base[k]["score"][t]
                        if cs is None or not cond(cs):
                            continue
                        if np.isfinite(EX[20][k, t]) and np.isfinite(R[20][k, t]):
                            ab.append(R[20][k, t])
                            ex.append(EX[20][k, t])
                            win.append(1.0 if R[20][k, t] > 0 else 0.0)
                if len(ab) >= 50:
                    cells.append("%+5.2f/%+5.2f 胜%4.1f%%(n=%4d)"
                                 % (float(np.mean(ab)), float(np.mean(ex)),
                                    100 * float(np.mean(win)), len(ab)))
                else:
                    cells.append("        样本不足 (n=%d)" % len(ab))
            if rg == "mid" and slab.startswith("C"):
                cells = ["—", "—"]
            print("%-10s | %-34s | %-34s" % (cn, cells[0], cells[1]))

    print("\n--- 逐年度: 口径A 市场偏热 x 行业深冷 (首轮报的最差组合) 绝对/超额 ---")
    for y in ylist:
        ab = [R[20][k, t] for k in range(K) for t in range(PIT_MIN_N, n_t - 30)
              if mreg[t] == "hot" and years[t] == y and base[k]["score"][t] is not None
              and base[k]["score"][t] <= 20 and np.isfinite(EX[20][k, t])]
        ex = [EX[20][k, t] for k in range(K) for t in range(PIT_MIN_N, n_t - 30)
              if mreg[t] == "hot" and years[t] == y and base[k]["score"][t] is not None
              and base[k]["score"][t] <= 20 and np.isfinite(EX[20][k, t])]
        if ab:
            print("  %s: 绝对 %+6.2f%%  超额 %+6.2f%%  胜率(绝对) %4.1f%%  n=%d"
                  % (y, float(np.mean(ab)), float(np.mean(ex)),
                     100 * float(np.mean([1.0 if v > 0 else 0.0 for v in ab])), len(ab)))

    # ================= [E4] 阈值敏感度 =================
    print("\n===== [E4] 阈值敏感度: 结论是否依赖这组分位数字 =====")
    print("把 (OB,OS,HOT,COLD) 四档分位整体放宽/收窄, 重算状态后看关键格子是否翻向.")
    grids = [(0.95, 0.05, 0.75, 0.25, "生产 (0.95/0.05/0.75/0.25)"),
             (0.90, 0.10, 0.70, 0.30, "放宽 (0.90/0.10/0.70/0.30)"),
             (0.975, 0.025, 0.85, 0.15, "收窄 (0.975/0.025/0.85/0.15)")]
    print("\n%-28s | %-21s | %-21s | %-21s"
          % ("阈值配置", "超卖20d 绝对/超额", "超买5d 绝对/超额", "偏热5d 绝对/超额"))
    for obq, osq, hotq, coldq, lab in grids:
        thg = thresholds(base, obq, osq, hotq, coldq)
        STg = state_series(base, thg)
        cells = []
        for st, h in (("超卖", 20), ("超买", 5), ("偏热", 5)):
            pa = [R[h][k, t] for k in range(K) for t in range(PIT_MIN_N, n_t - 30)
                  if STg[k, t] == st and np.isfinite(R[h][k, t])]
            pe = [EX[h][k, t] for k in range(K) for t in range(PIT_MIN_N, n_t - 30)
                  if STg[k, t] == st and np.isfinite(EX[h][k, t])]
            sz = sum(1 for k in range(K) for t in range(PIT_MIN_N, n_t - 30)
                     if STg[k, t] == st)
            cells.append("%+6.2f / %+6.2f (n=%d)"
                         % (float(np.mean(pa)), float(np.mean(pe)), sz))
        print("%-28s | %-21s | %-21s | %-21s" % (lab, cells[0], cells[1], cells[2]))

    # ================= [E5] 校准指标的抽样稳定性 =================
    print("\n===== [E5] ECE 的抽样稳定性 (bootstrap) =====")
    print("D3 的 ECE 是在 ~960 个样本上算的单点估计. 若其抽样区间很宽, 则")
    print("『上次 0.0953 → 这次 0.0346』可能只是采样噪声, 不能据此判断校准变好/变坏.")
    lib = AnalogLib(S)
    bins = [(0.0, 0.35), (0.35, 0.45), (0.45, 0.55), (0.55, 0.65), (0.65, 1.01)]
    rec = []
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
            rec.append((float(r["p_up"]), 1.0 if real[-1] > S[k, t] else 0.0))
    print("样本数: %d" % len(rec))

    def ece_of(sample):
        tot = len(sample)
        if tot == 0:
            return float("nan")
        num = 0.0
        for lo, hi in bins:
            sub = [x for x in sample if lo <= x[0] < hi]
            if len(sub) < 20:
                continue
            acc = sum(u for _, u in sub) / len(sub)
            conf = sum(p for p, _ in sub) / len(sub)
            num += (len(sub) / tot) * abs(acc - conf)
        return num

    point = ece_of(rec)
    rng = np.random.default_rng(20260912)
    boot = []
    arr = np.asarray(rec)
    for _ in range(400):
        idx = rng.integers(0, len(rec), len(rec))
        boot.append(ece_of([tuple(x) for x in arr[idx]]))
    boot = np.asarray(boot, dtype=float)
    lo95, hi95 = np.nanpercentile(boot, [2.5, 97.5])
    print("点估计 ECE = %.4f" % point)
    print("bootstrap 400 次: 中位 %.4f | 95%% 区间 [%.4f, %.4f] | 标准差 %.4f"
          % (float(np.nanmedian(boot)), lo95, hi95, float(np.nanstd(boot))))
    print("→ %s" % ("抽样区间宽, 单次 ECE 不能作为校准改善/恶化的证据"
                    if (hi95 - lo95) > 0.03 else "抽样区间较窄, ECE 可作为稳定指标"))

    print("\n" + "=" * 78)
    print("说明: 本脚本只读 data/, 不写任何产物; 全部为行业指数层面的统计规律,")
    print("      不构成投资建议. 超额收益均相对沪深300 同期.")
    print("=" * 78)


if __name__ == "__main__":
    main()
