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
                       weights_from_ic)

BASE = os.path.dirname(os.path.abspath(__file__))

# [PJ] 预测日志(审计资产, 2026-09-04): 每次主模式计算把当日的"预测快照"追加落盘.
#   复核工具 audit_predictions.py 在预测成熟(30 交易日后)后, 用当时的快照 vs 后来实际
#   分数做无泄漏复核(方向命中/p_up Brier/区间覆盖) —— 让"预测准确性"可被持续审计,
#   而不只依赖 walk-forward 模拟. 由 CI 独家提交(见 daily.yml commit 清单), 本地不提交.
#   存储紧凑化: 只留复核必需的终点值(s/med/lo/hi@30d + p_up), 每行业每天 ~120B.
PRED_LOG = os.path.join(BASE, "data", "prediction_log.jsonl")


def append_prediction_log(asof, industries):
    """按 asof 幂等追加预测快照行(JSON Lines). 只对主模式(31 一级行业)启用."""
    if os.path.exists(PRED_LOG):
        with open(PRED_LOG, encoding="utf-8") as f:
            for line in f:
                # [2026-09-05] 原实现按行首前缀 '{"asof":"..."}' 匹配, 与 json.dumps 的
                #   键顺序强耦合: 哪天给本行加个字段且排在 asof 前面, 幂等就静默失效,
                #   而 CI 一天最多跑 48 次 —— 同一天会被重复追加, 日志膨胀、复核时
                #   Brier/命中率被重复样本带偏, 且这种污染事后极难察觉。
                #   改为"裸日期子串预筛 + 解析确认": 预筛只认日期本身, 不含键名/引号/
                #   分隔符, 键顺序、separators 怎么变都不影响; 命中后再解析整行,
                #   确认是 asof 字段本身等于当日(而非某处恰好出现同形字符串)。
                #   ⚠️ 别把子串写成 '"asof":"%s"' —— 那又把生死交给序列化格式了
                #      (json.dumps 默认带空格 ": ", 与写入时的紧凑 separators 不同)。
                if asof not in line:
                    continue
                try:
                    if json.loads(line).get("asof") == asof:
                        return  # 当日已记录, 幂等
                except ValueError:
                    continue
    lines = []
    for ind in industries:
        fc = ind.get("forecast") or {}
        md, lo, hi = fc.get("median"), fc.get("p25"), fc.get("p75")
        if not md or not lo or not hi:
            continue
        lines.append(json.dumps({
            "asof": asof, "code": ind["code"], "name": ind.get("name"),
            "s": ind.get("cur_score"), "state": ind.get("state"),
            "med": round(float(md[-1]), 2),
            "lo": round(float(lo[-1]), 2),
            "hi": round(float(hi[-1]), 2),
            "p_up": fc.get("p_up"),
        }, ensure_ascii=False, separators=(",", ":")))
    if lines:
        with open(PRED_LOG, "a", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")
        print("prediction log appended: %d industries @ %s (→%s)"
              % (len(lines), asof, PRED_LOG))


# ---------------- 数据质量门禁 ----------------
def quality_gate(raw, bdates, bclose, expect_n=31, relax_prefix_vacuum=False):
    issues = []
    n_ind = len(raw)
    n_dt = len(bdates)
    bset = set(bdates)
    miss = dup = zero_c = zero_v = unsorted = 0
    # [2026-09-05] asof 日(=基准最后一日)无收盘数据的行业数。
    #   asof 取自基准而非行业数据本身, 若行业整体滞后一天(数据源延迟), 看板会宣称一个
    #   自己根本没有数据的截止日, 且 daily.yml 的 freshness gate 会误判"已含最新交易日"
    #   而不再重试 —— 双重说谎。故单独统计, 整体缺失时按致命处理。
    miss_last = 0
    miss_last_codes = []
    spans = []
    fq_keys = set()
    vacuum = {}  # [2026-09-03 二级模式] 指数口径真空期: 缺失全部集中在"连续有效段起点"之前
    for code, v in raw.items():
        fq_keys.add(v.get("fq_key") or "unknown")
        rows = v["rows"]
        ds = [r[0] for r in rows]
        cmap = {r[0]: r[2] for r in rows}
        n_miss_code = sum(1 for d in bdates if d not in cmap)
        if relax_prefix_vacuum and n_miss_code:
            # 判据: 最后一个缺失日之后全有(连续有效段), 且该段 >= 60% 全程 ->
            # 缺失是"指数尚未按现口径每日发布"的历史真空(如 801179 铁路公路, 申万2021版
            # 2021-12-13 生效), 非取数故障. 真空期不计入 miss(指标/回测对 None 天然兼容),
            # 单独如实披露. 中间有洞或近期缺失仍按真实缺失处理.
            has = [d in cmap for d in bdates]
            last_miss_idx = max(i for i, h in enumerate(has) if not h)
            if all(has[last_miss_idx + 1:]) and (n_dt - last_miss_idx - 1) >= int(n_dt * 0.6):
                vacuum[code] = {"name": v.get("name"), "cells": n_miss_code,
                                "valid_from": bdates[last_miss_idx + 1]}
                n_miss_code = 0
        miss += n_miss_code
        if bdates[-1] not in cmap:
            miss_last += 1
            miss_last_codes.append(v.get("name") or code)
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
    if n_ind != expect_n:
        issues.append("行业数 %d != %d" % (n_ind, expect_n))
    # [2026-09-04] lag 判定必须用北京时间: GitHub Actions runner 是 UTC, 若用 date.today()
    # (进程本地时区), 在北京 23:00-次日 08:00 (UTC 15:00-24:00) 运行的 CI 会把"今日收盘"
    # 误判成滞后 1 天, 披露失真。统一按 UTC+8 取"今天"。
    _bj = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=8))).date()
    lag_days = (_bj - datetime.date.fromisoformat(bdates[-1])).days
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
    # [2026-09-05] 阈值必须用 CAL_FULL_UNTIL(官方安排完整的年份末), 不能用 CAL_COVER_UNTIL。
    #   后者由 max(HOLIDAYS 里的年份) 推导 —— HOLIDAYS_2027 只要塞了元旦/劳动/国庆这 13 天,
    #   CAL_COVER_UNTIL 就变成 2027-12-31, 于是"日历覆盖到 2027 年底", 预警要等到
    #   asof > 2027-11 才触发。可 2027 年缺的是春节/清明/端午/中秋, 预测窗口从
    #   asof≈2026-11-16 起就会跨进 2027 年并撞上这些缺失 —— 预警整整晚一年, 等于没有。
    #   CAL_FULL_UNTIL 是"官方安排已确定的年份末", 才对应"此后只剩元旦/劳动/国庆固定段"。
    if fc_last and fc_last > CAL_FULL_UNTIL:
        issues.append("预测窗口(%s)超出官方日历完整覆盖(%s)：此后年份仅含元旦/劳动节/国庆固定段，"
                      "春节/清明/端午/中秋会被当作交易日，预测日期系统性错位，"
                      "请补充 obos_core.HOLIDAYS_20XX 表" % (fc_last, CAL_FULL_UNTIL))
    # [A5] 复权口径断言: 复权序列会随未来除权事件整体重算历史 -> 历史指标不可复现(违背 PIT)
    bad_fq = sorted(k for k in fq_keys if k not in ("day", "unknown"))
    if bad_fq:
        issues.append("检测到复权序列 %s: 复权价会随未来除权事件重算历史, 历史指标不可复现" % ",".join(bad_fq))
    # [2026-09-05] asof 日无数据的行业: 看板会宣称一个自己没有数据的截止日。
    #   整体缺失 = 数据源整体延迟(重试可解) -> 按致命处理, 让 daily.yml 失败并 30 分钟后重试;
    #   个别缺失 = 该行业可能已停止发布(重试无解) -> 只点名披露, 不因此阻断整条流水线
    #   (否则一个停更的行业会让看板永久无法更新, 比披露出来更糟)。
    if miss_last and miss_last == n_ind:
        issues.append("全部 %d 个行业在 asof 日(%s) 无收盘数据：行业数据整体滞后于基准，"
                      "若按此发布，宣称的截止日与实际数据不符，且会被误判为已最新而停止重试"
                      % (miss_last, bdates[-1]))
    elif miss_last:
        issues.append("%d 个行业在 asof 日(%s) 无收盘数据(可能已停止发布)，其分数止于前一交易日：%s"
                      % (miss_last, bdates[-1], "、".join(sorted(miss_last_codes)[:8])
                         + ("等" if miss_last > 8 else "")))
    cover = 1.0 - (miss / float(n_ind * n_dt)) if n_ind * n_dt else 0.0
    # [2026-09-05] 结构性/口径性问题必须 FAIL —— 它们不是"数据脏了一点"，而是"看板在说谎"：
    #   行业数残缺、复权口径污染(PIT 不可复现)、asof 日整体无数据。
    #   旧公式只把 dup / 零值 / 乱序 当 FAIL，这三类因此全部落进 WARN；而 WARN 不阻断部署
    #   （门禁只在 FAIL 时 SystemExit），等于这三条防御形同虚设。受控注入实测确认：
    #   26/31 行业、fq_key=qfq、全行业缺末日 三种故障注入后均为 WARN 放行。
    fatal = bool(dup or zero_c or unsorted or n_ind != expect_n or bad_fq
                 or (miss_last and miss_last == n_ind))
    return {
        "n_industries": n_ind, "n_dates": n_dt,
        "span": [bdates[0], bdates[-1]],
        "align_coverage": round(cover, 5),
        "missing_cells": miss, "dup_dates": dup,
        "missing_last_day": miss_last,
        "prefix_vacuum": vacuum,
        "nonpositive_close": zero_c, "negative_volume": zero_v,
        "unsorted_industries": unsorted,
        "lag_days": lag_days,
        "price_basis": "/".join(sorted(fq_keys)),
        "calendar_official_until": CAL_FULL_UNTIL,
        "calendar_cover_until": CAL_COVER_UNTIL,
        "forecast_last_date": fc_last,
        "issues": issues,
        # fatal 见上方说明：结构性/口径性问题一律 FAIL，不再降级为可被部署的 WARN。
        "status": "PASS" if not issues else ("WARN" if cover > 0.999 and not fatal else "FAIL"),
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
def main(sub_mode=False):
    # [2026-09-03] 二级行业模式: 同一条 PIT/回测/校准管线跑申万二级 109 个,
    # 输入 data/sub_klines.json(fetch_sub_data.py), 输出 data/sub_obos.json.
    # 数据源契约与一级完全一致(不复权日K/沪深300基准/同一套质量门禁),
    # 优化纪律(拉伸度 C11 启用、E2/E3 停用)自动继承.
    klines_file = "sub_klines.json" if sub_mode else "industry_klines.json"
    out_file = "sub_obos.json" if sub_mode else "industry_obos.json"
    expect_n = 109 if sub_mode else 31
    with open(os.path.join(BASE, "data", klines_file), encoding="utf-8") as f:
        raw = json.load(f)
    with open(os.path.join(BASE, "data", "benchmark.json"), encoding="utf-8") as f:
        hs = json.load(f)
    ref_dates = hs["dates"]
    bclose = hs["close"]
    bfq = hs.get("fq_key")  # [E] 基准复权口径(对齐行业 fq_key 透明度)
    asof = ref_dates[-1]
    n_t = len(ref_dates)

    quality = quality_gate(raw, ref_dates, bclose, expect_n=expect_n,
                           relax_prefix_vacuum=sub_mode)
    # [E] 跨基准一致性软告警: 基准前复权而行业未复权 -> rs_pct 相对强度跨基准偏估
    # [2026-09-05] 基准口径缺失时不能静默：data/benchmark.json 是入库的历史快照，
    #   仓库里那份没有 fq_key（字段是后来加的）。用它跑 compute 会让下面的跨基准一致性
    #   告警永远不可能触发（bfq=None 恒不等于 "qfq"）—— 防御形同虚设且毫无提示。
    if bfq is None:
        quality["issues"].append(
            "基准 benchmark.json 缺 fq_key 字段，无法核验跨基准复权口径一致性"
            "（多半用的是仓库内的历史快照，而非 fetch_benchmark.py 当次产物）")
        if quality["status"] == "PASS":
            quality["status"] = "WARN"
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
                     "parent": v.get("parent"), "n_constituents": v.get("n_constituents"),
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

    # [E2][E3] 深度回测产出的两套校准(全部由历史回测窗口拟合, PIT 安全) —— 2026-09-03 起仅诊断存档:
    #   pup_calib: p_up 分箱保序映射. 样本外 walk-forward 验证无一配置改善 -> 交付原始 p_up.
    #   mbias:     median 分区偏差. 样本外验证半量/全量平移均无改善(+0.34%/+2.52%) -> 交付原始 median.
    #   读取仅用于打印存档, 不再应用于主流程交付(详见下方交付段与 obos_core.py [E2][E3] 注).
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
            # [E3][E2 停用(2026-09-03 样本外否决)] median 平移与 p_up isotonic 映射经 walk-forward
            # 滚动验证均无样本外改善(median MAE 16.927->16.985 +0.34%; p_up Brier 0.2003->0.2003~0.2274),
            # 样本内收益为拟合窗口假象 —— 偏差/概率-频率关系随市场 regime 漂移, 固定映射无法外推.
            # 故交付原始 median 与原始 p_up(AUC 排序能力不受影响), 两表仅存档诊断供审计.
            adj = 0.0
            pup = fc["p_up"]
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
            "parent": b.get("parent"), "n_constituents": b.get("n_constituents"),
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

    # FDR (BH) — 标准算法: 排序后从后往前累积 min。
    # 原实现从前往后累积 max，方向反了，导致 q 恒等于 1.0、显著标签永不触发。
    ps = [x["p_extreme"] for x in industries]
    order = sorted(range(len(industries)), key=lambda i: ps[i])
    m = len(industries)
    q = [1.0] * m
    prev = 1.0
    for rank in range(m - 1, -1, -1):
        prev = min(prev, ps[order[rank]] * m / (rank + 1))
        q[order[rank]] = min(prev, 1.0)
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
        # [2026-09-04] 量能方向翻转(深度回测 2021-04→2026-09 定界, 见 docs/deep_backtest2):
        # 原规则"缩量+偏冷=机会加分 / 放量+偏热=风险减分"与 31 行业 × 1301 日实测相反:
        #   深冷(<=20) 放量 → 20d 超额 +3.46%(胜率66%) vs 缩量 +0.46%(≈无反弹) vs 平量 +0.19%
        #   偏热(50-65) 放量 → +1.10% vs 缩量 -0.70%   (放量=资金活跃代理, 全分数段成立)
        # 综合信号据此修正: 放量超卖=资金进场确认(机会加分), 偏热缩量=滞涨(风险微加分)。
        # 系数 0.3→1.0: 原 vol 项仅 ±0.09 分, 远不足以影响"机会/风险"边界, 属死权重。
        vol_conf = 0.0
        if ind["vol_state"] == "放量" and cs < 50:
            vol_conf = 0.3
        elif ind["vol_state"] == "缩量" and cs > 50:
            vol_conf = -0.2
        rs_now = ind["rs_pct_now"]
        rs_opp = (50 - rs_now) / 100.0 if (rs_now is not None and cs < 50) else 0.0
        rs_risk = (rs_now - 50) / 100.0 if (rs_now is not None and cs > 50) else 0.0
        opp = max(0.0, 50 - cs) / 50.0 * (0.6 + 0.4 * sig_b) + vol_conf + rs_opp * 0.2 \
            + (macro * 0.1 if cs < 50 else 0)
        risk = max(0.0, cs - 50) / 50.0 * (0.6 + 0.4 * sig_b) - vol_conf + rs_risk * 0.2
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
    # [2026-09-03] 不再声称"已保序校准(Brier x->y)": isotonic 映射样本外无改善(见 obos_core [E2] 注),
    # 交付为原始概率. Brier raw 若可得则如实披露, 便于读者判断概率校准质量.
    bt["conclusion"] = (
        "方向准确率 %.1f%%（块级 t 检验 p=%s，已按“%d行业同期算1块”消除横截面相关导致的显著性夸大），"
        "升温概率 AUC %s%s。点位误差比“持平”基线好 %s%%，但 Diebold-Mariano 块级检验 t=%s、p=%s，"
        "该点位优势%s——可信的是方向与区间，不是具体分数。"
        "稳健性：%d 个可比年份中 %s 年方向有效，最差年份 %s 仅 %.1f%%；"
        "条件覆盖最偏的一档是「%s」实测 %.1f%%（目标 50%%）。"
        % ((mm.get("dir_acc") or 0) * 100, mm.get("block_p"), expect_n,
           auc_txt,
           (", Brier %.3f（概率未做后验映射，样本外验证无改善）" % _pb_raw
            if _pb_raw is not None else ""),
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
    path = os.path.join(BASE, "data", out_file)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False)

    if not sub_mode:
        append_prediction_log(asof, industries)

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
    import sys
    main(sub_mode=("--sub" in sys.argv))
