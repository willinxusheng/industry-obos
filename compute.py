# -*- coding: utf-8 -*-
"""v5 主流程: 数据质量门禁 -> PIT 指标/权重/阈值 -> 跨行业类比推演 -> 无重叠回测+区间校准
-> data/industry_obos.json

准确性关键点见 obos_core.py 顶部清单 (H1-H7 历史统计 / F1-F4 预测 / B1-B3 回测 / C1 日历)
"""
import datetime
import json
import math
import os

import numpy as np

from obos_core import (HORIZON, HOT_Q, COLD_Q, MIN_N, OB_Q, OS_Q, PIT_MIN_N,
                       T_FULL, VOL_WIN,
                       W_SMOOTH, WIN, CAL_FULL_UNTIL, CAL_COVER_UNTIL, DEFAULT_W, FC_K, FC_MODE,
                       FC_MPI, FC_SEP, FWD, IC_REBAL,                        AnalogLib, COMBO_W, base_combo, base_combo_mkt,
                       regime_of, REGIME_CN,
                       expanding_quantile, future_trade_dates, ic_on_range, ma,
                       make_score_pit, pct_rank_series, pit_weight_path,
                       run_backtest, sub_indicators, walkforward_weights,
                       weights_from_ic, apply_pup_calib, median_bias_of)

BASE = os.path.dirname(os.path.abspath(__file__))


# ---------------- 数据质量门禁 ----------------
def quality_gate(raw, bdates, bclose):
    issues = []
    n_ind = len(raw)
    n_dt = len(bdates)
    bset = set(bdates)
    miss = dup = zero_c = zero_v = unsorted = 0
    spans = []
    fq_keys = set()
    for code, v in raw.items():
        fq_keys.add(v.get("fq_key") or "unknown")
        rows = v["rows"]
        ds = [r[0] for r in rows]
        cmap = {r[0]: r[2] for r in rows}
        miss += sum(1 for d in bdates if d not in cmap)
        dup += len(ds) - len(set(ds))
        zero_c += sum(1 for r in rows if r[2] is None or r[2] <= 0)
        zero_v += sum(1 for r in rows if r[5] is None or r[5] < 0)
        if ds != sorted(ds):
            unsorted += 1
        spans.append((ds[0], ds[-1], len(ds)))
    if len(bdates) != len(bset):
        issues.append("基准日期存在重复")
    if bdates != sorted(bdates):
        issues.append("基准日期未升序")
    if miss:
        issues.append("行业与基准日期对齐缺失 %d 个单元" % miss)
    if dup:
        issues.append("行业K线存在重复日期 %d 条" % dup)
    if zero_c:
        issues.append("非正收盘价 %d 条" % zero_c)
    if unsorted:
        issues.append("%d 个行业K线未按日期升序" % unsorted)
    if n_ind != 31:
        issues.append("行业数 %d != 31" % n_ind)
    today = datetime.date.today()
    lag_days = (today - datetime.date.fromisoformat(bdates[-1])).days
    if lag_days > 5:
        issues.append("数据滞后 %d 天" % lag_days)
    # [日历过期预警] 预测窗口若超出交易日历覆盖年份，future_trade_dates 只会排除周末，
    # 真实节假日会被误当作交易日 -> 预测日期系统性错位。
    # 这里只追加 issue(=> WARN)，绝不 FAIL —— 绝不能因日历没及时更新而阻断每日更新。
    fc_last = ""
    try:
        fd = future_trade_dates(bdates[-1], HORIZON)
        if fd:
            fc_last = fd[-1]
    except Exception:
        fc_last = ""
    if fc_last and fc_last > CAL_COVER_UNTIL:
        issues.append("预测窗口(%s)超出交易日历覆盖(%s)：未来交易日退化为仅排除周末，会把节假日误当交易日，"
                      "请补充 obos_core.HOLIDAYS 表" % (fc_last, CAL_COVER_UNTIL))
    # [A5] 复权口径断言: 复权序列会随未来除权事件整体重算历史 -> 历史指标不可复现(违背 PIT)
    bad_fq = sorted(k for k in fq_keys if k not in ("day", "unknown"))
    if bad_fq:
        issues.append("检测到复权序列 %s: 复权价会随未来除权事件重算历史, 历史指标不可复现" % ",".join(bad_fq))
    cover = 1.0 - (miss / float(n_ind * n_dt)) if n_ind * n_dt else 0.0
    return {
        "n_industries": n_ind, "n_dates": n_dt,
        "span": [bdates[0], bdates[-1]],
        "align_coverage": round(cover, 5),
        "missing_cells": miss, "dup_dates": dup,
        "nonpositive_close": zero_c, "negative_volume": zero_v,
        "unsorted_industries": unsorted,
        "lag_days": lag_days,
        "price_basis": "/".join(sorted(fq_keys)),
        "calendar_official_until": CAL_FULL_UNTIL,
        "calendar_cover_until": CAL_COVER_UNTIL,
        "forecast_last_date": fc_last,
        "issues": issues,
        "status": "PASS" if not issues else ("WARN" if cover > 0.999 and not (dup or zero_c or unsorted) else "FAIL"),
    }


# ---------------- 辅助 ----------------
def ret(closes, n):
    if len(closes) <= n or closes[-1] is None or closes[-1 - n] in (None, 0):
        return None
    return round((closes[-1] / closes[-1 - n] - 1) * 100, 2)


def vol_ratio_state(vol):
    """[H7] 基准均量剔除当日, 避免自我污染"""
    if not vol or vol[-1] is None:
        return None, "-"
    cur = vol[-1]
    prev = [x for x in vol[:-1] if isinstance(x, (int, float)) and x > 0]
    if not isinstance(cur, (int, float)) or cur <= 0 or len(prev) < VOL_WIN:
        return None, "-"
    ma_prev = sum(prev[-VOL_WIN:]) / VOL_WIN
    if not ma_prev:
        return None, "-"
    r = round(cur / ma_prev, 2)
    return r, ("放量" if r >= 1.5 else ("缩量" if r <= 0.7 else "平量"))


def daily_returns(close):
    out = []
    for i in range(len(close)):
        if i == 0 or close[i] is None or close[i - 1] in (None, 0):
            out.append(None)
        else:
            out.append(close[i] / close[i - 1] - 1)
    return out


def corr_matrix(rets_list):
    N = len(rets_list)
    C = [[0.0] * N for _ in range(N)]
    for i in range(N):
        for j in range(i + 1, N):
            pair = [(rets_list[i][k], rets_list[j][k]) for k in range(len(rets_list[i]))
                    if rets_list[i][k] is not None and rets_list[j][k] is not None]
            if len(pair) < 60:
                continue
            va = [p[0] for p in pair]
            vb = [p[1] for p in pair]
            m1 = sum(va) / len(va)
            m2 = sum(vb) / len(vb)
            num = sum((va[k] - m1) * (vb[k] - m2) for k in range(len(va)))
            d1 = sum((x - m1) ** 2 for x in va) ** 0.5
            d2 = sum((x - m2) ** 2 for x in vb) ** 0.5
            v = num / (d1 * d2) if d1 and d2 else 0.0
            C[i][j] = C[j][i] = round(max(-1.0, min(1.0, v)), 3)
    return C


def hclust(C):
    N = len(C)
    nodes = [{"members": [i], "left": None, "right": None, "height": 0.0, "is_leaf": True} for i in range(N)]
    D = {}
    for i in range(N):
        for j in range(i + 1, N):
            D[(i, j)] = 1.0 - C[i][j]

    def dist(a, b):
        s = 0.0
        c = 0
        for x in a["members"]:
            for y in b["members"]:
                i, j = (x, y) if x < y else (y, x)
                s += D.get((i, j), 0.0)
                c += 1
        return s / c if c else 1.0

    while len(nodes) > 1:
        bi = bj = None
        bd = 1e18
        for a in range(len(nodes)):
            for b in range(a + 1, len(nodes)):
                dd = dist(nodes[a], nodes[b])
                if dd < bd:
                    bd, bi, bj = dd, a, b
        na, nb = nodes[bi], nodes[bj]
        new = {"members": na["members"] + nb["members"], "left": na, "right": nb,
               "height": bd, "is_leaf": False}
        nodes = [nodes[k] for k in range(len(nodes)) if k not in (bi, bj)]
        nodes.append(new)
    return nodes[0]


def dendro_layout(root):
    leaf_pos = {}
    counter = [0]

    def assign(node):
        if node["is_leaf"]:
            p = counter[0]
            counter[0] += 1
            leaf_pos[id(node)] = p
            return p
        lp = assign(node["left"])
        rp = assign(node["right"])
        node["x"] = (lp + rp) / 2.0
        node["xl"], node["xr"] = lp, rp
        return node["x"]

    assign(root)
    order = [0] * counter[0]

    def place(node):
        if node["is_leaf"]:
            order[leaf_pos[id(node)]] = node["members"][0]
            return
        place(node["left"])
        place(node["right"])

    place(root)
    segs = []

    def collect(node):
        if node["is_leaf"]:
            return
        segs.append({"lx": node["xl"], "lh": node["left"]["height"],
                     "rx": node["xr"], "rh": node["right"]["height"], "h": node["height"]})
        collect(node["left"])
        collect(node["right"])

    collect(root)
    return order, segs


def cut_k(node, K):
    internal = []

    def collect(n):
        if not n["is_leaf"]:
            internal.append(n)
            collect(n["left"])
            collect(n["right"])

    collect(node)
    hs = sorted((n["height"] for n in internal), reverse=True)
    if K - 1 >= len(hs):
        thr = -1.0
    else:
        hi = hs[K - 1]
        lo = hs[K] if K < len(hs) else 0.0
        thr = (hi + lo) / 2.0
    keep = set(id(n) for n in internal if n["height"] > thr)

    def cut(n):
        if n["is_leaf"]:
            return [[n["members"][0]]]
        if id(n) in keep:
            return cut(n["left"]) + cut(n["right"])
        return [n["members"]]

    return cut(node)


def detect_divergence(close, score, win=120):
    n = len(close)
    if n < win * 2:
        return "none"
    p1, p2 = close[n - 2 * win:n - win], close[n - win:n]
    s1, s2 = score[n - 2 * win:n - win], score[n - win:n]

    def mn(a):
        v = [x for x in a if isinstance(x, (int, float))]
        return min(v) if v else None

    def mx(a):
        v = [x for x in a if isinstance(x, (int, float))]
        return max(v) if v else None

    pl1, pl2, ph1, ph2 = mn(p1), mn(p2), mx(p1), mx(p2)
    sl1, sl2, sh1, sh2 = mn(s1), mn(s2), mx(s1), mx(s2)
    if None not in (pl1, pl2, sl1, sl2) and pl2 < pl1 * 0.985 and sl2 > sl1 + 3:
        return "bullish"
    if None not in (ph1, ph2, sh1, sh2) and ph2 > ph1 * 1.015 and sh2 < sh1 - 3:
        return "bearish"
    return "none"


def r1(x):
    return round(x, 1) if isinstance(x, (int, float)) and math.isfinite(x) else None


# ---------------- 主流程 ----------------
def main():
    with open(os.path.join(BASE, "data", "industry_klines.json"), encoding="utf-8") as f:
        raw = json.load(f)
    with open(os.path.join(BASE, "data", "benchmark.json"), encoding="utf-8") as f:
        hs = json.load(f)
    ref_dates = hs["dates"]
    bclose = hs["close"]
    bfq = hs.get("fq_key")  # [E] 基准复权口径(对齐行业 fq_key 透明度)
    asof = ref_dates[-1]
    n_t = len(ref_dates)

    quality = quality_gate(raw, ref_dates, bclose)
    # [E] 跨基准一致性软告警: 基准前复权而行业未复权 -> rs_pct 相对强度跨基准偏估
    if bfq == "qfq" and "day" in (quality.get("price_basis") or ""):
        quality["issues"].append(
            "基准沪深300为前复权(fq_key=qfq)，与行业未复权(day)口径不一致，rs_pct 相对强度将跨基准偏估")
        if quality["status"] == "PASS":
            quality["status"] = "WARN"
    quality["benchmark_fq_key"] = bfq  # [E] 基准复权口径透明度(持久化进 artifact, 供下游/审计核对)
    print("QUALITY:", json.dumps(quality, ensure_ascii=False))
    if quality["status"] == "FAIL":
        raise SystemExit("数据质量门禁 FAIL, 拒绝产出: %s" % quality["issues"])

    # 阶段1: 子指标
    base = []
    ind_meta = []
    closes_all = []
    for code, v in raw.items():
        cmap = {r[0]: r[2] for r in v["rows"]}
        vmap = {r[0]: r[5] for r in v["rows"]}
        close = [cmap.get(d) for d in ref_dates]
        vol = [vmap.get(d) for d in ref_dates]
        rs, p_close, p_bias = sub_indicators(close)
        rel = [close[i] / bclose[i] if (close[i] and bclose[i]) else None for i in range(n_t)]
        ind_meta.append({"rs": rs, "pos": p_close, "bias": p_bias})
        closes_all.append(close)
        base.append({"code": code, "name": v["name"], "sw": v.get("sw", code.replace("pt01", "")),
                     "close": close, "vol": vol, "rs": rs,
                     "rs_pct": pct_rank_series(rel), "m200": ma(close, 200),
                     "rets": daily_returns(close)})

    # 阶段2: walk-forward IC 权重 (PIT, 非重叠采样)
    print("calibrating walk-forward IC weights ...", flush=True)
    segs = walkforward_weights(ind_meta, closes_all, n_t)
    ic_full = ic_on_range(ind_meta, closes_all, 60, n_t - 1)
    _, flip_full, lam_full, t_full = weights_from_ic(ic_full)
    w_now = segs[-1]["w"]
    flip_now = segs[-1]["flip"]
    w_hist = [{"from": ref_dates[min(s["start"], n_t - 1)], "w": s["w"], "lam": s["lam"]}
              for s in segs]
    print("weight windows: %d, latest=%s lam=%s" %
          (len(segs), json.dumps(w_now, ensure_ascii=False), segs[-1]["lam"]))

    for i, b in enumerate(base):
        b["score"] = make_score_pit(ind_meta[i]["rs"], ind_meta[i]["pos"],
                                    ind_meta[i]["bias"], segs, n_t, smooth=True)

    # [H9] 纯权重伪影: 固定当日指标值, 只把权重从 w[t-1] 换成 w[t] 产生的分数差.
    # 这样剔除了市场自身波动, 度量的完全是"重算日造成的历史读数被改写"的幅度.
    def _artifact(wpath, spath):
        js = []
        for t in range(1, n_t):
            dw = {k: wpath[t][k] - wpath[t - 1][k] for k in wpath[t]}
            ds = {k: spath[t][k] - spath[t - 1][k] for k in spath[t]}
            if all(abs(dw[k]) < 1e-12 for k in dw) and all(abs(ds[k]) < 1e-12 for k in ds):
                continue
            for i in range(len(base)):
                a, b_, c_ = ind_meta[i]["rs"][t], ind_meta[i]["pos"][t], ind_meta[i]["bias"][t]
                if None in (a, b_, c_):
                    continue
                x = {"rsi": a - 50.0, "pos": b_ - 50.0, "bias": c_ - 50.0}
                now = sum(wpath[t][k] * (50.0 + spath[t][k] * x[k]) for k in x)
                prev = sum(wpath[t - 1][k] * (50.0 + spath[t - 1][k] * x[k]) for k in x)
                js.append(abs(now - prev))
        if not js:
            return 0.0, 0.0
        return round(float(np.median(js)), 3), round(max(js), 2)

    keys_w = list(DEFAULT_W.keys())
    raw_w_path, raw_s_path = [], []
    _si = 0
    for t in range(n_t):
        while _si + 1 < len(segs) and segs[_si + 1]["start"] <= t:
            _si += 1
        if t < segs[0]["start"]:
            _w, _f = DEFAULT_W, {k: False for k in keys_w}
        else:
            _w, _f = segs[_si]["w"], segs[_si]["flip"]
        raw_w_path.append({k: float(_w.get(k, DEFAULT_W[k])) for k in keys_w})
        raw_s_path.append({k: (-1.0 if _f.get(k) else 1.0) for k in keys_w})
    sm_w_path, sm_s_path = pit_weight_path(segs, n_t)
    med_jump_raw, max_jump_raw = _artifact(raw_w_path, raw_s_path)
    med_jump, max_jump = _artifact(sm_w_path, sm_s_path)

    # 阶段3: 相关性聚类
    Cm = corr_matrix([b["rets"] for b in base])
    root = hclust(Cm)
    order_idx, dsegs = dendro_layout(root)
    groups_idx = cut_k(root, 6)
    cluster_data = {"order": [base[k]["code"] for k in order_idx],
                    "names": [base[k]["name"] for k in order_idx],
                    "segs": dsegs,
                    "corr": [[Cm[order_idx[i]][order_idx[j]] for j in range(len(order_idx))]
                             for i in range(len(order_idx))],
                    "groups": [[base[i]["code"] for i in g] for g in groups_idx],
                    "n_groups": len(groups_idx)}

    # 阶段4: score 矩阵 + 类比库
    # [C4] 市场因子分解: 行业分 = β·市场(沪深300)分 + 截距 + 特质残差.
    # 把市场分与各行业特质残差作为额外行并入 S, 使类比库能分别推演"系统性"与"特质"两路再重构.
    K = len(base)
    S = np.full((K, n_t), np.nan)
    for k, b in enumerate(base):
        for i, v in enumerate(b["score"]):
            if v is not None:
                S[k, i] = v
    m_rs, m_pos, m_bias = sub_indicators(bclose)
    mkt_score = np.array([x if x is not None else np.nan for x in
                          make_score_pit(m_rs, m_pos, m_bias, segs, n_t, smooth=True)], dtype=float)
    S2 = np.full((2 * K + 1, n_t), np.nan)
    for k in range(K):
        S2[k] = S[k]
    S2[K] = mkt_score
    beta_map = {}
    for k in range(K):
        a = S[k]; b = mkt_score
        msk = np.isfinite(a) & np.isfinite(b)
        if msk.sum() < 120:
            S2[K + 1 + k] = np.full(n_t, np.nan)
            beta_map[k] = (0.0, float(np.nanmean(a)) if msk.any() else 0.0)
        else:
            bv = b[msk] - np.nanmean(b[msk]); av = a[msk] - np.nanmean(a[msk])
            den = float(np.nansum(bv ** 2))
            beta = float(np.nansum(bv * av) / den) if den > 1e-12 else 0.0
            Cc = float(np.nanmean(a[msk])) - beta * float(np.nanmean(b[msk]))
            S2[K + 1 + k] = a - beta * b - Cc
            beta_map[k] = (beta, Cc)
    print("building analog libraries ...", flush=True)
    lib_ind = AnalogLib(S)  # [C4] 行业专属池(不含市场/残差行): 现有 combo/knn 预测不受影响, 避免池污染
    lib = AnalogLib(S2, mkt_idx=K, idio_of=lambda kk: K + 1 + kk, beta_map=beta_map)  # 含市场+特质残差, 仅供 combo_mkt 分解推演
    print("segments ind/mkt:", lib_ind.M, "/", lib.M, "| mkt_idx:", K, flush=True)

    # 阶段5: 无重叠回测 + 区间自动校准
    print("running walk-forward backtest (non-overlapping) ...", flush=True)
    bt, cals = run_backtest(S, lib_ind, lib_mkt=lib, dates=ref_dates, industry_rows=range(K))
    MAIN = bt.get("diag_on", "knn")          # [A6] 主推演方法(组合优先)
    _cm = cals.get(MAIN, {})
    cal = np.asarray(_cm.get("h", np.ones(HORIZON)), dtype=float)   # [A7] 逐步长
    cal_reg = _cm.get("reg", {})                                     # [A8] 分区乘子
    print("main forecaster:", MAIN, "| cal h1=%.3f h30=%.3f | reg=%s"
          % (float(cal[0]), float(cal[-1]), json.dumps(cal_reg)), flush=True)
    print("dm knn vs persist:", bt.get("dm_vs_baseline", {}).get("persist"), flush=True)
    print("dm combo vs knn  :", bt.get("dm_combo_vs_knn"), flush=True)
    print("dm combo vs persist:", bt.get("dm_combo_vs_persist"), flush=True)
    print("dm combo_mkt vs persist:", bt.get("dm_combo_mkt_vs_persist"), flush=True)
    print("worst year:", bt.get("worst_year"), "| year win rate:", bt.get("year_win_rate"), flush=True)
    print("cov diag worst:", (bt.get("cov_diag") or [{}])[0], flush=True)

    # [E2][E3] 深度回测产出的两套校准(全部由历史回测窗口拟合, PIT 安全):
    #   pup_calib: p_up 分箱保序映射(低端原过保守 0.05->实际0.22, 校准后 Brier 改善)
    #   mbias:     median 分区偏差(中枢区预测偏低~7分, 半量保守校正)
    pup_calib = bt.get("p_up_calib")
    mbias = bt.get("median_bias")
    print("p_up calib:", json.dumps(pup_calib, ensure_ascii=False)[:200] if pup_calib else "none", flush=True)
    print("median bias:", json.dumps(mbias, ensure_ascii=False)[:240] if mbias else "none", flush=True)

    # 阶段6: PIT 阈值 / 状态 / 推演 / 量能
    t_last = n_t - 1
    industries = []
    for k, b in enumerate(base):
        score = b["score"]
        ob_s = expanding_quantile(score, OB_Q)
        os_s = expanding_quantile(score, OS_Q)
        # 偏热/偏冷边界同样走 PIT 分位(75/25), 而不是 (ob+os)/2 中点二分.
        # 中点二分会让"中性"档永远为空, 且普跌行情下 20+ 个行业全被压进"偏冷", 分档失去信息量.
        hot_s = expanding_quantile(score, HOT_Q)
        cold_s = expanding_quantile(score, COLD_Q)
        ob_line, os_line = ob_s[-1], os_s[-1]
        hot_line, cold_line = hot_s[-1], cold_s[-1]
        cs = score[-1]
        svals = [x for x in score if x is not None]
        if ob_line is None or os_line is None:
            ob_line = ob_line or (max(svals) if svals else 80)
            os_line = os_line or (min(svals) if svals else 20)
        if hot_line is None:
            hot_line = ob_line - (ob_line - os_line) * 0.25
        if cold_line is None:
            cold_line = os_line + (ob_line - os_line) * 0.25
        # 保证 os <= cold < hot <= ob, 避免样本不足期出现倒挂
        cold_line = min(max(cold_line, os_line), ob_line)
        hot_line = min(max(hot_line, cold_line), ob_line)
        if cs is None:
            state = "-"
        elif cs >= ob_line:
            state = "超买"
        elif cs <= os_line:
            state = "超卖"
        elif cs >= hot_line:
            state = "偏热"
        elif cs <= cold_line:
            state = "偏冷"
        else:
            state = "中性"
        if cs is not None and svals:
            p_ob = sum(1 for x in svals if x >= cs) / len(svals)
            p_os = sum(1 for x in svals if x <= cs) / len(svals)
            p_extreme, extreme_dir = min(p_ob, p_os), ("ob" if p_ob <= p_os else "os")
        else:
            p_extreme, extreme_dir = 1.0, "-"

        # [A7][A8] 交付带宽 = 逐步长系数 x 当前所处区间的乘子(区间由已知的当日分数决定, PIT 安全)
        ca = cal * cal_reg.get(regime_of(cs), 1.0)
        if MAIN == "combo":
            cb = base_combo(S, lib_ind, k, t_last)
            r0 = lib_ind.forecast(k, t_last)
            if cb is None:
                fc = None
            else:
                med0, q25_0, q75_0, pup0 = cb
                fc = {"median": med0,
                      "p25": np.clip(med0 - (med0 - q25_0) * ca, 0, 100),
                      "p75": np.clip(med0 + (q75_0 - med0) * ca, 0, 100),
                      "p_up": pup0,
                      "pool": (r0 or {}).get("pool", 0),
                      "n_used": (r0 or {}).get("n_used", 0)}
        elif MAIN == "combo_mkt":
            cb = base_combo_mkt(S, lib, k, t_last)
            r0 = lib.forecast(lib.idio_of(k), t_last, clip=False)
            if cb is None:
                fc = None
            else:
                med0, q25_0, q75_0, pup0 = cb
                fc = {"median": [r1(x) for x in med0],
                      "p25": [r1(x) for x in np.clip(med0 - (med0 - q25_0) * ca, 0, 100)],
                      "p75": [r1(x) for x in np.clip(med0 + (q75_0 - med0) * ca, 0, 100)],
                      "p_up": round(float(pup0), 3),
                      "pool": (r0 or {}).get("pool", 0),
                      "n_used": (r0 or {}).get("n_used", 0)}
        else:
            fc = lib.forecast(k, t_last, cal=ca)
        if fc is None:
            fdict = {"median": None, "p25": None, "p75": None, "pool": 0, "n_used": 0,
                     "p_up": None, "future_dates": future_trade_dates(asof, HORIZON)}
        else:
            # [E3] median 分区偏差保守校正(半量, 由历史回测拟合): med/p25/p75 同平移保持带宽
            adj = median_bias_of(cs, mbias)
            # [E2] p_up 保序校准(由历史回测拟合; AUC 为排序指标不受单调映射影响)
            pup = apply_pup_calib(fc["p_up"], pup_calib)
            fdict = {"median": [r1(min(max(x + adj, 0), 100)) for x in fc["median"]],
                     "p25": [r1(min(max(x + adj, 0), 100)) for x in fc["p25"]],
                     "p75": [r1(min(max(x + adj, 0), 100)) for x in fc["p75"]],
                     "pool": fc["pool"], "n_used": fc["n_used"],
                     "p_up": round(pup, 3) if pup is not None else None,
                     "future_dates": future_trade_dates(asof, HORIZON)}
        above = (b["close"][-1] is not None and b["m200"][-1] is not None
                 and b["close"][-1] >= b["m200"][-1])
        vr, vst = vol_ratio_state(b["vol"])
        industries.append({
            "code": b["code"], "name": b["name"], "sw": b["sw"],
            "close": [round(c, 2) if c is not None else None for c in b["close"]],
            "vol": [round(x, 0) if isinstance(x, (int, float)) else None for x in b["vol"]],
            "score": [r1(x) for x in score],
            "rs_pct": [r1(x) for x in b["rs_pct"]],
            "rel_now": round(b["close"][-1] / bclose[-1], 4) if (b["close"][-1] and bclose[-1]) else None,
            "ma200": b["m200"], "above_ma200": above,
            "ob_line": r1(ob_line), "os_line": r1(os_line),
            "hot_line": r1(hot_line), "cold_line": r1(cold_line),
            "ob_series": [r1(x) for x in ob_s], "os_series": [r1(x) for x in os_s],
            "forecast": fdict,
            "cur_score": r1(cs), "cur_rsi": r1(b["rs"][-1]),
            "rs_pct_now": r1(b["rs_pct"][-1]),
            "p_extreme": round(p_extreme, 3), "extreme_dir": extreme_dir, "state": state,
            "chg5": r1(cs - score[-6]) if (len(score) > 5 and score[-6] is not None and cs is not None) else None,
            "ret20": ret(b["close"], 20), "ret60": ret(b["close"], 60), "ret250": ret(b["close"], 250),
            "vol_ratio": vr, "vol_state": vst, "valuation": None,
        })

    # FDR (BH)
    ps = [x["p_extreme"] for x in industries]
    order = sorted(range(len(industries)), key=lambda i: ps[i])
    m = len(industries)
    q = [1.0] * m
    prev = 0.0
    for rank, i in enumerate(order):
        prev = max(prev, ps[i] * m / (rank + 1))
        q[i] = min(prev, 1.0)
    for i, ind in enumerate(industries):
        ind["fdr_q"] = round(q[i], 3)
        sig = "-"
        if ind["cur_score"] is not None and q[i] < 0.05:
            if ind["extreme_dir"] == "ob" and ind["cur_score"] >= ind["ob_line"]:
                sig = "显著超买"
            elif ind["extreme_dir"] == "os" and ind["cur_score"] <= ind["os_line"]:
                sig = "显著超卖"
        ind["sig"] = sig

    # 市场宽度
    breadth_pct, hot_cnt, cold_cnt, above_cnt = [], [], [], []
    for i in range(n_t):
        an = sum(1 for x in industries
                 if x["close"][i] is not None and x["ma200"][i] is not None and x["close"][i] >= x["ma200"][i])
        sc = [x["score"][i] for x in industries if x["score"][i] is not None]
        breadth_pct.append(round(an / len(industries) * 100, 1))
        hot_cnt.append(sum(1 for s in sc if s >= 65))
        cold_cnt.append(sum(1 for s in sc if s <= 35))
        above_cnt.append(an)

    # 综合信号 + 背离
    breadth_now = breadth_pct[-1] if breadth_pct else 50
    macro = max(0.0, (50 - breadth_now) / 100.0)
    bmap = {b["code"]: b for b in base}
    for ind in industries:
        b = bmap.get(ind["code"])
        ind["divergence"] = detect_divergence(b["close"], ind["score"]) if b else "none"
        cs = ind["cur_score"]
        if cs is None:
            ind.update({"opp_score": None, "risk_score": None, "sig_kind": "neutral",
                        "sig_label": "-", "sig_score": None})
            continue
        sig_b = 1.0 if ind["sig"] != "-" else 0.0
        vol_conf = 0.0
        if ind["vol_state"] == "缩量" and cs < 50:
            vol_conf = 0.3
        elif ind["vol_state"] == "放量" and cs > 50:
            vol_conf = -0.3
        rs_now = ind["rs_pct_now"]
        rs_opp = (50 - rs_now) / 100.0 if (rs_now is not None and cs < 50) else 0.0
        rs_risk = (rs_now - 50) / 100.0 if (rs_now is not None and cs > 50) else 0.0
        opp = max(0.0, 50 - cs) / 50.0 * (0.6 + 0.4 * sig_b) + vol_conf * 0.3 + rs_opp * 0.2 \
            + (macro * 0.1 if cs < 50 else 0)
        risk = max(0.0, cs - 50) / 50.0 * (0.6 + 0.4 * sig_b) - vol_conf * 0.3 + rs_risk * 0.2
        opp = max(0.0, min(1.0, opp))
        risk = max(0.0, min(1.0, risk))
        opp_s, risk_s = round(opp * 100), round(risk * 100)
        if cs <= 35:
            kind, s = "opportunity", opp_s
            label = "强机会" if s >= 70 else ("机会" if s >= 50 else "中性")
        elif cs >= 65:
            kind, s = "risk", risk_s
            label = "强风险" if s >= 70 else ("风险" if s >= 50 else "中性")
        else:
            kind, s, label = "neutral", max(opp_s, risk_s), "中性"
        ind.update({"opp_score": opp_s, "risk_score": risk_s, "sig_kind": kind,
                    "sig_label": label, "sig_score": s})

    industries.sort(key=lambda x: -(x["cur_score"] or 0))

    mm = bt.get(MAIN, {})
    pst = bt.get("persist", {})
    edge = None
    if mm.get("mae_end") and pst.get("mae_end"):
        edge = round((pst["mae_end"] - mm["mae_end"]) / pst["mae_end"] * 100, 1)
    dmv = (bt.get("dm_%s_vs_persist" % MAIN) if MAIN in ("combo", "combo_mkt")
           else (bt.get("dm_vs_baseline", {}) or {}).get("persist")) or {}
    dp = dmv.get("dm_p")
    sig = "统计显著" if (dp is not None and dp < 0.05) else "统计上不显著"
    wy = bt.get("worst_year") or {}
    cd = (bt.get("cov_diag") or [{}])[0]
    bt["edge_pct"] = edge  # [B] 单源真值: 点位优势真实值写入 JSON, 前端不再硬编码 8.5%
    _auc = bt.get("p_up_auc")
    auc_txt = ("%.3f" % _auc) if isinstance(_auc, (int, float)) else "N/A"  # [D] 缺失显 N/A 而非 nan
    _pc = bt.get("p_up_calib") or {}
    _pb_raw = _pc.get("brier_raw")
    _pb_cal = _pc.get("brier_cal")
    bt["conclusion"] = (
        "方向准确率 %.1f%%（块级 t 检验 p=%s，已按“31行业同期算1块”消除横截面相关导致的显著性夸大），"
        "升温概率 AUC %s%s。点位误差比“持平”基线好 %s%%，但 Diebold-Mariano 块级检验 t=%s、p=%s，"
        "该点位优势%s——可信的是方向与区间，不是具体分数。"
        "稳健性：%d 个可比年份中 %s 年方向有效，最差年份 %s 仅 %.1f%%；"
        "条件覆盖最偏的一档是「%s」实测 %.1f%%（目标 50%%）。"
        % ((mm.get("dir_acc") or 0) * 100, mm.get("block_p"),
           auc_txt,
           ("，已按历史样本做保序校准(Brier %.3f→%.3f)" % (_pb_raw, _pb_cal)
            if (_pb_raw is not None and _pb_cal is not None) else ""),
           edge if edge is not None else "-", dmv.get("dm_t"), dp, sig,
           len(bt.get("year_stability") or []),
           ("%.0f%%" % ((bt.get("year_win_rate") or 0) * 100)),
           wy.get("year", "-"), (wy.get("dir_acc") or 0) * 100,
           cd.get("bin", "-"), (cd.get("cov") or 0) * 100)
    )
    bt["main_method"] = MAIN
    bt["combo_w"] = COMBO_W
    bt["industries_tested"] = len(industries)

    out = {
        "asof": asof, "win": WIN, "horizon": HORIZON,
        "quality": quality,
        "weights": {"rsi": w_now["rsi"], "pos": w_now["pos"], "bias": w_now["bias"],
                    "ic": ic_full, "flip": flip_now, "wf": w_hist,
                    "wf_windows": len(segs), "rebal": IC_REBAL, "fwd": FWD,
                    "pit_jump_max": max_jump, "pit_jump_median": med_jump,
                    "pit_jump_max_raw": max_jump_raw, "pit_jump_median_raw": med_jump_raw,
                    "w_smooth": W_SMOOTH,
                    "smooth_note": ("纯权重伪影（固定当日指标、只换权重）：硬切换单日最大 %.2f 分、"
                                    "中位 %.3f 分；改为 %d 日因果 EMA 平滑后降至最大 %.2f 分、中位 %.3f 分，"
                                    "平滑只改权重的时间路径，不使用任何未来信息"
                                    % (max_jump_raw, med_jump_raw, W_SMOOTH, max_jump, med_jump)),
                    "lam": segs[-1]["lam"], "lam_full": lam_full,
                    "t_abs": t_full, "t_full_gate": T_FULL, "prior": DEFAULT_W,
                    "shrink_note": ("IC 非重叠采样后 |t| 分别为 %s（阈值 %.1f），均与 0 无法区分；"
                                    "故权重按 λ=%.3f 向先验 %s 收缩，避免拟合噪声"
                                    % (json.dumps(t_full, ensure_ascii=False), T_FULL,
                                       lam_full, json.dumps(DEFAULT_W, ensure_ascii=False)))},
        "method": {"pit_threshold": "扩张窗口(min %d obs): %d/%d 分位定超买超卖线, %d/%d 分位定偏热偏冷线; "
                                    "历史每一天只用当日及之前数据"
                                    % (PIT_MIN_N, int(OB_Q * 100), int(OS_Q * 100),
                                       int(HOT_Q * 100), int(COLD_Q * 100)),
                   "forecaster": "跨行业类比池 K=%d 多尺度%s 核加权 mode=%s 同行业最小间隔%d 每行业上限%d"
                                 % (FC_K, str(list((10, 20, 40))), FC_MODE, FC_SEP, FC_MPI),
                   "forecast_main": ("市场因子分解推演：行业分=β·沪深300分+截距+特质残差，系统性(β·大盘)与特质(残差)分别用各自类比池推演后重构"
                                     if MAIN == "combo_mkt" else
                                     ("类比池与「持平」基线按 %.0f/%.0f 等权组合（收缩保号，方向不变）"
                                      % (COMBO_W * 100, (1 - COMBO_W) * 100)) if MAIN == "combo"
                                     else "纯跨行业类比池"),
                   "cal_factor": round(float(np.mean(cal)), 3),
                   "cal_curve": [round(float(x), 3) for x in np.asarray(cal).ravel()],
                   "cal_reg": {REGIME_CN.get(g, g): round(v, 3) for g, v in cal_reg.items()},
                   "cal_note": ("两层条件校准：① 按预测步长分别反解半宽系数（第1日 %.3f → 第%d日 %.3f），"
                                "单一全局系数只能让平均覆盖率达标、实测长步长系统性偏窄；"
                                "② 再按当日所处区间缩放（%s），因为分数处于中枢时后续波动比模型预期更大"
                                % (float(cal[0]), HORIZON, float(cal[-1]),
                                   json.dumps({REGIME_CN.get(g, g): round(v, 2)
                                               for g, v in cal_reg.items()}, ensure_ascii=False))),
                   "price_basis": "板块指数点位，未做复权（接口对 qfq 参数无响应，已实测与未复权逐日完全一致）",
                   "analog_pool": lib.M},
        "benchmark": {"name": hs["name"], "dates": ref_dates, "close": bclose, "fq_key": bfq},
        "breadth": {"dates": ref_dates, "pct": breadth_pct, "hot_cnt": hot_cnt,
                    "cold_cnt": cold_cnt, "above_cnt": above_cnt, "n_ind": len(industries)},
        "backtest": bt,
        "fdr_note": "BH法多重比较校正，q<0.05 视为统计显著",
        "cluster": cluster_data,
        "industries": industries,
    }
    path = os.path.join(BASE, "data", "industry_obos.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False)

    print("asof:", asof, "industries:", len(industries),
          "with_forecast:", sum(1 for x in industries if x["forecast"]["median"]))
    print("weights(PIT latest):", json.dumps(w_now, ensure_ascii=False), "flip:", flip_now)
    print("IC(full, non-overlap):", json.dumps(ic_full, ensure_ascii=False))
    print("weight artifact (pure): hard-switch max %.2f med %.3f -> EMA%d max %.2f med %.3f"
          % (max_jump_raw, med_jump_raw, W_SMOOTH, max_jump, med_jump))
    print("breadth now:", breadth_pct[-1], "% above200:", above_cnt[-1], "/", len(industries))
    print("FDR 显著超买:", [x["name"] for x in industries if x["sig"] == "显著超买"],
          " 显著超卖:", [x["name"] for x in industries if x["sig"] == "显著超卖"])
    print("BACKTEST:")
    for kk in ("knn", "persist", "meanrev", "momentum", "randomwalk"):
        if kk in bt:
            print("  %-11s" % kk, json.dumps(bt[kk], ensure_ascii=False))
    print("  p_up_auc:", bt.get("p_up_auc"), " cal h1/h30: %.3f/%.3f" % (float(cal[0]), float(cal[-1])))
    print("YEAR STABILITY:", json.dumps(bt.get("year_stability"), ensure_ascii=False))
    print("COV DIAG:", json.dumps(bt.get("cov_diag"), ensure_ascii=False))
    print("CONCLUSION:", bt["conclusion"])


if __name__ == "__main__":
    main()
