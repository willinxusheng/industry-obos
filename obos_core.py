# -*- coding: utf-8 -*-
"""OBOS 核心库 v5 — 准确性强化版

相对 v4 的准确性修复(每条都可验证):
 [H1] ma() 修复: 原实现遇到 None 会让滚动和永久错位(少减窗口外值), 污染 MA20(乖离率)/MA200(市场宽度)
 [H2] 分位数改 midrank: (below+0.5*equal)/n, 正确处理并列, 且永不触及 0/100 极值
 [H3] RSI 改 None-safe: 数据断点后重新预热, 不再因 None 崩溃或串味
 [H4] IC 改非重叠采样: 原用逐日重叠的未来20日收益 -> IC 序列强自相关 -> IR 被系统性高估
 [H5] 权重改 walk-forward: 原用全样本 IC 定权后回刷全部历史 = 前视偏差; 现每 250 日用当时可得数据重估
 [H6] 超买/超卖阈值改扩张窗口(PIT): 原用全样本 95/5 分位判定历史每一天 = 前视偏差
 [H7] 量比基准剔除当日: 原 60 日均量含当日, 自我污染
 [F1] 类比池扩到跨行业(3.4万段 vs 原 1.2千段), 多尺度(10/20/40)形状匹配, 核加权分位
 [F2] 近邻强制时间去重: 原 K=8 近邻常是同一段行情的连续重叠窗口 -> 区间假窄(实测覆盖率仅31.8%)
 [F3] 区间自动校准: 用回测残差比的中位数反解半宽系数, 使 P25-P75 实测覆盖率贴近理论 50%
 [F4] 概率化输出 p_up(升温概率), 回测 AUC≈0.71
      [2026-09-05] 口径修正: 原先 p_up 的评估样本被嵌在"方向准确率"的 |变动|>=1.0 过滤里,
      等于剔掉了全部小变动样本(38/961=3.9%), 报出的 AUC 0.717 是"过滤掉难样本"条件下的
      选择性偏差。已移出该过滤, 全样本口径 AUC=0.713(n=961)。本文件顶部的旧数字 0.69 亦已更正。
 [B1] 回测改无重叠采样(step=HORIZON), 样本近似独立
 [B2] 显著性改块级检验: 31 行业同期高度相关, 视为 1 个时间块, 消除 p 值夸大
 [B3] 新增"持平"基线(有界均值回复序列的最强点预测基线)
 [C1] 未来交易日排除中国法定节假日
"""
import bisect, datetime, math, os

import numpy as np

WIN = 252
MIN_N = 126
HORIZON = 30

# 预测器(经受控 walk-forward 实验选定)
FC_SCALES = (10, 20, 40)
FC_K = int(os.environ.get("FC_K", "40"))   # [C8] 近邻数; 实验用, 默认 40
FC_SEP = 20          # 同行业内近邻最小时间间隔(交易日)
FC_MPI = 4           # 每行业最多贡献近邻数
FC_MODE = "delta"    # 'abs' | 'delta' | 'mix'
FC_VEL_W = float(os.environ.get("FC_VEL_W", "0.0"))  # [C8] 速度(动量)维度权重; 默认关闭——实测加入后方向准确率65.7→64.7%、DM p=0.0437→0.1019(转不显著). OBOS为均值回复序列, 速度维度使类比追势, 与回复目标相反, 故关闭. 保留开关供未来带外生数据复测.
STRETCH_L = 60       # [C11] 拉伸度(回复压力)特征的后窥窗口(交易日, ~3月); PIT: 仅用 e 及之前
# [C11] 拉伸度(回复压力)维度权重. 2026-09-03 受控实验(1300日x31行业, asof=09-02, 无重叠回测+块级DM)启用:
#   W=0.0(原) -> 0.3 -> 0.6 -> 1.0: combo 方向 67.3% -> 67.8% -> 68.5% -> 69.2%;
#   DM vs 持平 p=0.0032 -> 0.0028 -> 0.0021 -> 0.0021; AUC 0.757 -> 0.761; MAE 16.73 -> 16.69.
#   最大收益在中性区(40-60): 方向 49.8%(≈随机) -> 55.3%(W=0.6), 补上中枢区缺失的方向信号.
#   取 0.6 而非 1.0: 更高权重下 2022 年(n仅60=2块)方向掉至 56.7%, 小样本噪声大, 0.6 更稳健.
#   这是被[C8]速度维度否决后的"对偶"特征: 速度=运动趋势(追势, 对均值回复有害),
#   拉伸=相对自身后窥均值的偏离幅度(回复势能, 与均值回复一致). 两者正交.
FC_STRETCH_W = float(os.environ.get("FC_STRETCH_W", "0.6"))

# PIT 阈值
PIT_MIN_N = 500
OB_Q, OS_Q = 0.95, 0.05
HOT_Q, COLD_Q = 0.75, 0.25   # 偏热/偏冷边界也走 PIT 分位, 避免中点二分让"中性"档永远为空

# IC / 权重
FWD = 20
IC_MIN_HIST = 500
IC_REBAL = 250
IC_MIN_IND = 15
DEFAULT_W = {"rsi": 0.40, "pos": 0.35, "bias": 0.25}
T_FULL = 2.0         # |t|>=2 才认为 IC 有统计意义, 才完全信任数据驱动权重
W_SMOOTH = 20        # [H9] 权重路径 EMA 平滑跨度(交易日), 消除重算日分数台阶

# 回测
BT_BACK = 900
VOL_WIN = 60


# ==================== 基础统计 ====================
def rankdata(a):
    order = sorted(range(len(a)), key=lambda i: a[i])
    ranks = [0.0] * len(a)
    i = 0
    while i < len(a):
        j = i
        while j + 1 < len(a) and a[order[j + 1]] == a[order[i]]:
            j += 1
        avg = (i + j) / 2.0 + 1.0
        for k in range(i, j + 1):
            ranks[order[k]] = avg
        i = j + 1
    return ranks


def spearman(a, b):
    """秩相关. [2026-09-05] 先剔非有限值再排队。

    原实现直接对原始序列排序：NaN 与任何值比较都为 False，rankdata 的"并列判定"
    会因此失效并算出一串错秩，最终返回一个 NaN 相关系数。而调用方 ic_on_range 的
    守卫写的是 `if v is not None` —— **NaN 不是 None**，拦不住，于是 NaN 会一路
    污染 mean_ic -> weights_from_ic -> 合成分数；分数里的 NaN 直到 build_html 的
    "NaN not in data_js" 断言才炸，那时已经跑了十分钟。故在源头就拒绝并回退 None。
    调用方已保证等长且已滤 None，正常路径不受影响。
    """
    n = len(a)
    if n < 5 or len(b) != n:
        return None
    pairs = [(x, y) for x, y in zip(a, b)
             if x is not None and y is not None
             and math.isfinite(x) and math.isfinite(y)]
    if len(pairs) < 5:
        return None
    a = [p[0] for p in pairs]
    b = [p[1] for p in pairs]
    n = len(a)
    ra, rb = rankdata(a), rankdata(b)
    ma_ = sum(ra) / n
    mb = sum(rb) / n
    num = sum((ra[i] - ma_) * (rb[i] - mb) for i in range(n))
    da = sum((ra[i] - ma_) ** 2 for i in range(n)) ** 0.5
    db = sum((rb[i] - mb) ** 2 for i in range(n)) ** 0.5
    if da == 0 or db == 0:
        return None
    return num / (da * db)


def norm_sf(z):
    """标准正态上尾, 用于双侧 p"""
    return 0.5 * math.erfc(abs(z) / math.sqrt(2))


def block_ttest(rates, h0=0.5):
    """块级 t 检验: rates = 每个时间块的正确率. 返回 (mean, t, p, n_blocks)"""
    a = [r for r in rates if r is not None and math.isfinite(r)]
    n = len(a)
    if n < 3:
        return None, None, None, n
    m = sum(a) / n
    sd = (sum((x - m) ** 2 for x in a) / (n - 1)) ** 0.5 or 1e-9
    t = (m - h0) / (sd / math.sqrt(n))
    return m, t, 2 * norm_sf(t), n


def block_dm(d_blocks):
    """[A2] 块级 Diebold-Mariano: d_blocks = 每块的 (基线损失 - 本模型损失) 均值.
    正值=本模型更准. 块 = 同一预测时点(31 行业算 1 块), 步长=持有期 -> 块间近似独立,
    因此对块均值直接做 t 检验, 已同时吸收横截面相关与序列相关, 比标准 DM+NW 更保守.
    返回 (mean_gain, t, p, n_blocks)"""
    a = [x for x in d_blocks if x is not None and math.isfinite(x)]
    n = len(a)
    if n < 3:
        return None, None, None, n
    m = sum(a) / n
    sd = (sum((x - m) ** 2 for x in a) / (n - 1)) ** 0.5
    if sd < 1e-12:
        return m, None, None, n
    t = m / (sd / math.sqrt(n))
    return m, t, 2 * norm_sf(t), n


def kupiec_lr(x, n, p=0.5):
    """[A3] Kupiec 无条件覆盖 LR 检验. x=命中数, n=总数, p=目标覆盖率.
    LR ~ chi2(1), 临界值 3.841(95%). 注意: 同一路径内 30 个步长高度相关,
    该检验假设独立 -> p 值偏乐观, 仅作诊断参考, 结论以块级标准误为准."""
    if n < 20 or x <= 0 or x >= n:
        return None, None
    ph = x / n
    ll1 = x * math.log(ph) + (n - x) * math.log(1 - ph)
    ll0 = x * math.log(p) + (n - x) * math.log(1 - p)
    lr = 2 * (ll1 - ll0)
    # chi2(1) 上尾 = 2*Phi(-sqrt(lr))
    return lr, 2 * norm_sf(math.sqrt(max(lr, 0.0)))


# ==================== 指标 ([H1][H2][H3]) ====================
def ma(vals, n):
    """None-safe 滚动均值: 窗口内必须 n 个有效值, 滚动和严格进出配对"""
    out = [None] * len(vals)
    s = 0.0
    cnt = 0
    buf = []
    head = 0
    for i, v in enumerate(vals):
        buf.append(v)
        if v is not None:
            s += v
            cnt += 1
        if len(buf) - head > n:
            old = buf[head]
            head += 1
            if old is not None:
                s -= old
                cnt -= 1
        if len(buf) - head == n and cnt == n:
            out[i] = s / n
    return out


def rsi(closes, n=14):
    """Wilder RSI, None-safe: 遇断点重新预热"""
    r = [None] * len(closes)
    warm = []
    ag = al = None
    for i in range(1, len(closes)):
        c0, c1 = closes[i - 1], closes[i]
        if c0 is None or c1 is None:
            warm = []
            ag = al = None
            continue
        ch = c1 - c0
        g = ch if ch > 0 else 0.0
        l = -ch if ch < 0 else 0.0
        if ag is None:
            warm.append((g, l))
            if len(warm) == n:
                ag = sum(x[0] for x in warm) / n
                al = sum(x[1] for x in warm) / n
                r[i] = 100.0 if al == 0 else 100 - 100 / (1 + ag / al)
        else:
            ag = (ag * (n - 1) + g) / n
            al = (al * (n - 1) + l) / n
            r[i] = 100.0 if al == 0 else 100 - 100 / (1 + ag / al)
    return r


def pct_rank_series(vals, win=WIN, min_n=MIN_N):
    """滚动 midrank 百分位: (below + 0.5*equal) / n * 100"""
    out = [None] * len(vals)
    for i in range(len(vals)):
        if vals[i] is None:
            continue
        s = max(0, i - win + 1)
        seg = [v for v in vals[s:i + 1] if v is not None]
        if len(seg) < min_n:
            continue
        v = vals[i]
        below = 0
        equal = 0
        for x in seg:
            if x < v:
                below += 1
            elif x == v:
                equal += 1
        out[i] = (below + 0.5 * equal) / len(seg) * 100.0
    return out


def expanding_quantile(vals, q, min_n=PIT_MIN_N):
    """扩张窗口分位(point-in-time): 只用 t 及之前的全部数据, 线性插值"""
    out = [None] * len(vals)
    buf = []
    for i, v in enumerate(vals):
        if v is not None:
            bisect.insort(buf, v)
        if len(buf) >= min_n:
            pos = q * (len(buf) - 1)
            lo = int(math.floor(pos))
            hi = min(lo + 1, len(buf) - 1)
            out[i] = buf[lo] + (buf[hi] - buf[lo]) * (pos - lo)
    return out


def sub_indicators(closes):
    rs = rsi(closes)
    m20 = ma(closes, 20)
    bias = [(closes[i] / m20[i] - 1) * 100 if (m20[i] and closes[i] is not None) else None
            for i in range(len(closes))]
    return rs, pct_rank_series(closes), pct_rank_series(bias)


# ==================== IC / 权重 ([H4][H5]) ====================
def ic_on_range(ind_meta, closes_all, t_lo, t_hi, step=FWD):
    """非重叠采样的截面 rank-IC. t 上界保证 t+FWD <= t_hi (无未来泄漏)"""
    ic = {"rsi": [], "pos": [], "bias": []}
    t = t_lo
    while t + FWD <= t_hi:
        cols = {"rsi": [], "pos": [], "bias": []}
        fr = []
        for k in range(len(ind_meta)):
            c = closes_all[k]
            if t + FWD >= len(c) or c[t] in (None, 0) or c[t + FWD] is None:
                continue
            r = ind_meta[k]["rs"][t]
            p = ind_meta[k]["pos"][t]
            b = ind_meta[k]["bias"][t]
            if None in (r, p, b):
                continue
            cols["rsi"].append(r)
            cols["pos"].append(p)
            cols["bias"].append(b)
            fr.append(c[t + FWD] / c[t] - 1)
        if len(fr) >= IC_MIN_IND:
            for key in cols:
                v = spearman(cols[key], fr)
                if v is not None:
                    ic[key].append(v)
        t += step
    stats = {}
    for key in ic:
        s = ic[key]
        if len(s) < 6:
            stats[key] = {"mean_ic": None, "ir": None, "n": len(s)}
            continue
        m = sum(s) / len(s)
        sd = (sum((x - m) ** 2 for x in s) / (len(s) - 1)) ** 0.5 or 1e-9
        stats[key] = {"mean_ic": round(m, 4), "ir": round(m / sd, 3), "n": len(s)}
    return stats


def weights_from_ic(stats, prior=None, t_full=T_FULL):
    """[H8] 按统计显著性收缩定权.

    修掉重叠采样偏差后, 三个子指标的 |IR| 仅 0.04~0.10, t = IR*sqrt(n) ≈ 0.4,
    与 0 无法区分. 直接用 |IR| 归一化 = 拟合噪声, 实测会造成权重窗口切换时
    分数跳变中位数 7 分/最大 37 分. 故:
      w = λ·w_IC + (1-λ)·w_prior,  λ = clamp(mean|t| / t_full, 0, 1)
    仅当某指标 |t| >= t_full 且 mean_ic > 0 时才允许反向(flip), 避免按噪声翻转语义.
    """
    prior = dict(DEFAULT_W) if prior is None else prior
    irs, ts, flip = {}, {}, {}
    for k, v in stats.items():
        ir = v.get("ir")
        n = v.get("n") or 0
        irs[k] = abs(ir) if ir is not None else 0.0
        ts[k] = abs(ir) * math.sqrt(n) if (ir is not None and n > 0) else 0.0
        flip[k] = bool(v.get("mean_ic") is not None and v["mean_ic"] > 0 and ts[k] >= t_full)
    for k in prior:
        irs.setdefault(k, 0.0)
        ts.setdefault(k, 0.0)
        flip.setdefault(k, False)
    tot = sum(irs.values())
    w_ic = {k: irs[k] / tot for k in irs} if tot > 0 else dict(prior)
    lam = min(1.0, (sum(ts.values()) / len(ts)) / t_full) if ts else 0.0
    w = {k: lam * w_ic.get(k, 0.0) + (1 - lam) * prior[k] for k in prior}
    s = sum(w.values())
    if s <= 0 or any(not math.isfinite(x) for x in w.values()):
        return dict(prior), {k: False for k in prior}, 0.0, {k: 0.0 for k in prior}
    w = {k: round(w[k] / s, 4) for k in w}
    return w, flip, round(lam, 3), {k: round(ts[k], 2) for k in ts}


def walkforward_weights(ind_meta, closes_all, n_t,
                        min_hist=IC_MIN_HIST, rebal=IC_REBAL):
    """返回 [{start,w,flip,stats,lam,t}, ...]; start 起生效, 仅用 <start 的数据校准"""
    segs = []
    r = min_hist
    while r < n_t:
        st = ic_on_range(ind_meta, closes_all, 60, r)
        w, fl, lam, ts = weights_from_ic(st)
        segs.append({"start": r, "w": w, "flip": fl, "stats": st, "lam": lam, "t": ts})
        r += rebal
    if not segs:
        segs = [{"start": 0, "w": dict(DEFAULT_W), "flip": {k: False for k in DEFAULT_W},
                 "stats": {}, "lam": 0.0, "t": {}}]
    return segs


def pit_weight_path(segs, n_t, span=W_SMOOTH):
    """[H9] 逐日权重路径 + 因果 EMA 平滑.

    权重每 IC_REBAL 天硬切换会让同一天的历史分数出现台阶(实测最大 20 分),
    使"某天 62 分"这种历史读数依赖切换点位置, 而非市场本身. 解决:
      1) 把分段权重展开成逐日序列;
      2) 对权重做 EMA(span) 平滑 —— 只用 <=t 的信息, 无前视;
      3) flip 用方向系数 s∈[-1,1] 表达 (v = 50 + s·(x-50)), 使反向也连续可插值.
    平滑仅改变权重的时间路径, 不改变任一时点可用的信息集.
    """
    keys = list(DEFAULT_W.keys())
    alpha = 2.0 / (span + 1.0)
    raw_w, raw_s = [], []
    si = 0
    for t in range(n_t):
        while si + 1 < len(segs) and segs[si + 1]["start"] <= t:
            si += 1
        if t < segs[0]["start"]:
            w, fl = DEFAULT_W, {k: False for k in keys}
        else:
            w, fl = segs[si]["w"], segs[si]["flip"]
        raw_w.append({k: float(w.get(k, DEFAULT_W[k])) for k in keys})
        raw_s.append({k: (-1.0 if fl.get(k) else 1.0) for k in keys})
    ew, es = [], []
    cw = dict(raw_w[0]) if raw_w else dict(DEFAULT_W)
    cs = dict(raw_s[0]) if raw_s else {k: 1.0 for k in keys}
    for t in range(n_t):
        for k in keys:
            cw[k] = alpha * raw_w[t][k] + (1 - alpha) * cw[k]
            cs[k] = alpha * raw_s[t][k] + (1 - alpha) * cs[k]
        tot = sum(cw.values()) or 1e-9
        ew.append({k: cw[k] / tot for k in keys})
        es.append({k: max(-1.0, min(1.0, cs[k])) for k in keys})
    return ew, es


def make_score_pit(rs, pos, bias, segs, n_t, smooth=True):
    """point-in-time 合成分: t 时刻只用 t 之前校准出的权重([H5]) + 权重路径平滑([H9])"""
    if smooth:
        ew, es = pit_weight_path(segs, n_t)
    else:
        ew, es = None, None
    score = [None] * n_t
    si = 0
    for t in range(n_t):
        while si + 1 < len(segs) and segs[si + 1]["start"] <= t:
            si += 1
        if smooth:
            cur_w, sg = ew[t], es[t]
        else:
            if t < segs[0]["start"]:
                w0, f0 = DEFAULT_W, {k: False for k in DEFAULT_W}
            else:
                w0, f0 = segs[si]["w"], segs[si]["flip"]
            cur_w = w0
            sg = {k: (-1.0 if f0.get(k) else 1.0) for k in DEFAULT_W}
        a, b, c = rs[t], pos[t], bias[t]
        if a is None or b is None or c is None:
            continue
        va = 50.0 + sg["rsi"] * (a - 50.0)
        vb = 50.0 + sg["pos"] * (b - 50.0)
        vc = 50.0 + sg["bias"] * (c - 50.0)
        score[t] = round(cur_w["rsi"] * va + cur_w["pos"] * vb + cur_w["bias"] * vc, 2)
    return score


# ==================== 交易日历 ([C1]) ====================
# 国务院办公厅《关于2026年部分节假日安排的通知》; A股调休周末不开市
HOLIDAYS_2026 = [
    "2026-01-01", "2026-01-02", "2026-01-03",
    "2026-02-15", "2026-02-16", "2026-02-17", "2026-02-18", "2026-02-19",
    "2026-02-20", "2026-02-21", "2026-02-22", "2026-02-23",
    "2026-04-04", "2026-04-05", "2026-04-06",
    "2026-05-01", "2026-05-02", "2026-05-03", "2026-05-04", "2026-05-05",
    "2026-06-19", "2026-06-20", "2026-06-21",
    "2026-09-25", "2026-09-26", "2026-09-27",
    "2026-10-01", "2026-10-02", "2026-10-03", "2026-10-04",
    "2026-10-05", "2026-10-06", "2026-10-07",
]
# 2027 安排未发布, 仅纳入法定固定段(元旦/劳动节/国庆), 覆盖度在 quality 中披露
HOLIDAYS_2027 = ["2027-01-01",
                 "2027-05-01", "2027-05-02", "2027-05-03", "2027-05-04", "2027-05-05",
                 "2027-10-01", "2027-10-02", "2027-10-03", "2027-10-04",
                 "2027-10-05", "2027-10-06", "2027-10-07"]
HOLIDAYS = set(HOLIDAYS_2026) | set(HOLIDAYS_2027)
CAL_FULL_UNTIL = "2026-12-31"


def _cal_cover_until():
    """日历「实际覆盖到」的年末，由 HOLIDAYS 推导。

    刻意不另行硬编码：否则新增 HOLIDAYS_20XX 后若忘记同步这里，两个常量会漂移，
    预警就会在错误的时间点触发（或永不触发）。
    超出该年份后 future_trade_dates 只会排除周末，真实节假日会被误当作交易日。
    """
    if not HOLIDAYS:
        return ""
    return "%d-12-31" % max(int(h[:4]) for h in HOLIDAYS)


CAL_COVER_UNTIL = _cal_cover_until()


def future_trade_dates(last_date, n):
    d = datetime.date.fromisoformat(last_date)
    out = []
    while len(out) < n:
        d += datetime.timedelta(days=1)
        if d.weekday() < 5 and d.isoformat() not in HOLIDAYS:
            out.append(d.isoformat())
    return out


# ==================== 类比库 / 预测器 ([F1][F2][F3][F4]) ====================
class AnalogLib:
    """跨行业类比片段库(numpy 向量化). 严格无泄漏: 候选片段的未来窗口必须完全早于查询时点"""

    def __init__(self, S, horizon=HORIZON, scales=FC_SCALES,
                 mkt_idx=None, idio_of=None, beta_map=None):
        self.S = S
        self.H = horizon
        self.scales = scales
        self.maxs = max(scales)
        self.mkt_idx = mkt_idx
        self.idio_of = idio_of
        self.beta_map = beta_map
        NK, T = S.shape
        ends = []
        for k in range(NK):
            fin = np.isfinite(S[k])
            for e in range(self.maxs - 1, T - horizon):
                if fin[e - self.maxs + 1:e + 1].all() and fin[e + 1:e + 1 + horizon].all():
                    ends.append((k, e))
        arr = np.array(ends, dtype=np.int64) if ends else np.zeros((0, 2), dtype=np.int64)
        self.ind = arr[:, 0] if len(arr) else arr
        self.end = arr[:, 1] if len(arr) else arr
        M = len(arr)
        self.M = M
        self.lvl = np.empty(M)
        self.fut = np.empty((M, horizon))
        self.anch = np.empty(M)
        self.vel = np.empty((M, 2))   # [C8] 每片段末端的(5日,20日)动量差, 用于匹配距离的速度维度
        self.stretch = np.empty(M)    # [C11] 每片段末端的"拉伸度"= (score - 自身后窥均值)/后窥std, 回复势能
        self.paths = {s: np.empty((M, s)) for s in scales}
        for m in range(M):
            k, e = int(self.ind[m]), int(self.end[m])
            self.lvl[m] = S[k, e - 9:e + 1].mean()
            for s in scales:
                seg = S[k, e - s + 1:e + 1]
                self.paths[s][m] = seg - seg.mean()
            self.vel[m, 0] = (S[k, e] - S[k, e - 5]) if (e - 5 >= 0 and np.isfinite(S[k, e - 5])) else 0.0
            self.vel[m, 1] = (S[k, e] - S[k, e - 20]) if (e - 20 >= 0 and np.isfinite(S[k, e - 20])) else 0.0
            # [C11] 拉伸度: 只用 e 及之前 STRETCH_L 日(严格 PIT)
            lo = max(0, e - STRETCH_L + 1)
            traw = S[k, lo:e + 1]
            tfin = traw[np.isfinite(traw)]
            if len(tfin) >= 30:
                mu = float(tfin.mean()); sd = float(tfin.std())
                self.stretch[m] = (S[k, e] - mu) / sd if sd > 1e-9 else 0.0
            else:
                self.stretch[m] = 0.0
            self.fut[m] = S[k, e + 1:e + 1 + horizon]
            self.anch[m] = S[k, e]
        # [A1] 距离归一化尺度必须 PIT: 原实现用全库(含查询时点之后的未来片段)算 MAD/RMS,
        # 等于让未来数据决定"多远算远", 是真实的前视偏差. 全库值仅作池过小时的回退.
        self.nrm_l = float(np.median(np.abs(self.lvl - np.median(self.lvl)))) or 1.0 if M else 1.0
        self.nrm_p = {s: (float(np.sqrt((self.paths[s] ** 2).mean(axis=1)).mean()) or 1.0) if M else 1.0
                      for s in scales}
        self.nrm_v0 = float(np.median(np.abs(self.vel[:, 0]))) or 1.0 if M else 1.0
        self.nrm_v1 = float(np.median(np.abs(self.vel[:, 1]))) or 1.0 if M else 1.0
        self.nrm_st = float(np.median(np.abs(self.stretch))) or 1.0 if M else 1.0
        self._nrm_cache = {}
        self._fc_cache = {}
        self.nrm_pit_used = 0
        self.nrm_fallback = 0
        # [C4] 市场因子分解钩子(由 compute.py 在构造时注入):
        #   mkt_idx  = 市场(沪深300)分数所在行
        #   idio_of  = lambda k -> 行业 k 的特质残差所在行
        #   beta_map = {k: (beta, C)} 由全样本 OLS 分解得到, 用于重构

    def _nrm_pit(self, t, idx):
        """[A1] 只用查询时点 t 之前已完结的片段计算归一化尺度. 按 t 缓存(同一天 31 行业共用)"""
        c = self._nrm_cache.get(t)
        if c is not None:
            return c
        if len(idx) < 200:
            self.nrm_fallback += 1
            c = (self.nrm_l, self.nrm_p, self.nrm_v0, self.nrm_v1, self.nrm_st)
        else:
            lv = self.lvl[idx]
            nl = float(np.median(np.abs(lv - np.median(lv)))) or 1.0
            npd = {}
            for s in self.scales:
                npd[s] = float(np.sqrt((self.paths[s][idx] ** 2).mean(axis=1)).mean()) or 1.0
            va = self.vel[idx, 0]; vb = self.vel[idx, 1]
            nv0 = float(np.median(np.abs(va))) or 1.0
            nv1 = float(np.median(np.abs(vb))) or 1.0
            nst = float(np.median(np.abs(self.stretch[idx]))) or 1.0
            self.nrm_pit_used += 1
            c = (nl, npd, nv0, nv1, nst)
        self._nrm_cache[t] = c
        return c

    @staticmethod
    def _wq(vals, w, q):
        o = np.argsort(vals)
        v = vals[o]
        ww = w[o]
        c = np.cumsum(ww) - 0.5 * ww
        c = c / ww.sum()
        return float(np.interp(q, c, v))

    def _dist(self, gidx, lvl0, p0, nl=None, npd=None, v0=None, nv0=None, nv1=None, st0=None, nst=None):
        nl = self.nrm_l if nl is None else nl
        npd = self.nrm_p if npd is None else npd
        D = np.abs(self.lvl[gidx] - lvl0) / nl
        wsc = 1.0 / len(self.scales)
        for s in self.scales:
            diff = self.paths[s][gidx] - p0[s]
            D = D + wsc * np.sqrt((diff ** 2).mean(axis=1)) / npd[s]
        # [C8] 速度(动量)维度: 优先匹配"同水平、同轨迹"的历史片段, 而非仅同形态.
        # 同形不同速(如已加速冲顶 vs 刚起步)对近 30 日推演含义截然不同.
        if FC_VEL_W and v0 is not None and nv0 and nv1:
            dv = (np.abs(self.vel[gidx, 0] - v0[0]) / nv0
                  + np.abs(self.vel[gidx, 1] - v0[1]) / nv1) * 0.5
            D = D + FC_VEL_W * dv
        # [C11] 拉伸度(回复压力)维度: 匹配"相对自身后窥均值偏离幅度相近"的历史片段.
        # 与速度维度正交——速度捕捉运动趋势(追势, 对均值回复有害), 拉伸捕捉回复势能(与均值回复一致).
        if FC_STRETCH_W and st0 is not None and nst:
            D = D + FC_STRETCH_W * np.abs(self.stretch[gidx] - st0) / nst
        return D

    def forecast(self, k0, t, K=FC_K, sep=FC_SEP, mpi=FC_MPI, mode=FC_MODE, cal=None, clip=True):
        """带缓存的对外接口. 校准在缓存之后应用, 使同一 (行业,时点) 只做一次近邻检索.
        cal 可为标量或长度 H 的逐步长系数([A7]).
        clip=False 用于特质残差(idio)序列——其值可负, 不应被截断到 [0,100]."""
        key = (k0, t, K, sep, mpi, mode, clip)
        r = self._fc_cache.get(key)
        if r is None:
            r = self._fc_raw(k0, t, K, sep, mpi, mode, clip)
            self._fc_cache[key] = r
        if r is None:
            return None
        med, q25, q75 = r["median"], r["p25"], r["p75"]
        if cal is not None:
            ca = np.asarray(cal, dtype=float)
            if np.any(ca != 1.0):
                q25 = med - (med - q25) * ca
                q75 = med + (q75 - med) * ca
        if clip:
            # [2026-09-05] 同 _fc_raw: 改为整体求值后绑定, 不再依赖上一行改写的 q25
            q25, q75 = (np.clip(np.minimum(q25, q75), 0, 100),
                        np.clip(np.maximum(q25, q75), 0, 100))
            med = np.clip(med, 0, 100)
        return {"median": med,
                "p25": q25, "p75": q75,
                "p_up": r["p_up"], "n_used": r["n_used"], "pool": r["pool"],
                "consensus": r.get("consensus", 0.5)}

    def _fc_raw(self, k0, t, K=FC_K, sep=FC_SEP, mpi=FC_MPI, mode=FC_MODE, clip=True):
        S, H = self.S, self.H
        if t < self.maxs - 1 or not np.isfinite(S[k0, t - self.maxs + 1:t + 1]).all():
            return None
        lvl0 = float(S[k0, t - 9:t + 1].mean())
        p0 = {}
        for s in self.scales:
            seg = S[k0, t - s + 1:t + 1]
            p0[s] = seg - seg.mean()
        v0 = np.array([
            (S[k0, t] - S[k0, t - 5]) if (t - 5 >= 0 and np.isfinite(S[k0, t - 5])) else 0.0,
            (S[k0, t] - S[k0, t - 20]) if (t - 20 >= 0 and np.isfinite(S[k0, t - 20])) else 0.0,
        ])
        # [C11] 查询点拉伸度(回复压力): 自身后窥 STRETCH_L 日窗口, 严格 PIT
        lo = max(0, t - STRETCH_L + 1)
        traw = S[k0, lo:t + 1]
        tfin = traw[np.isfinite(traw)]
        if len(tfin) >= 30:
            mu = float(tfin.mean()); sd = float(tfin.std())
            st0 = (S[k0, t] - mu) / sd if sd > 1e-9 else 0.0
        else:
            st0 = 0.0
        mask = self.end + H <= t
        idx = np.flatnonzero(mask)
        pool = int(len(idx))
        if pool < K * 3:
            return None
        nl, npd, nv0, nv1, nst = self._nrm_pit(t, idx)
        D = self._dist(idx, lvl0, p0, nl, npd, v0, nv0, nv1, st0, nst)
        order = np.argsort(D)
        sel = []
        cnt = {}
        ends = {}
        for oi in order:
            g = int(idx[oi])
            ki = int(self.ind[g])
            ei = int(self.end[g])
            if cnt.get(ki, 0) >= mpi:
                continue
            if any(abs(ei - pe) < sep for pe in ends.get(ki, ())):
                continue
            sel.append(g)
            cnt[ki] = cnt.get(ki, 0) + 1
            ends.setdefault(ki, []).append(ei)
            if len(sel) >= K:
                break
        if len(sel) < 8:
            return None
        sel = np.array(sel)
        Ds = self._dist(sel, lvl0, p0, nl, npd, v0, nv0, nv1, st0, nst)
        h = float(np.median(Ds)) or 1e-9
        W = np.maximum(np.exp(-0.5 * (Ds / h) ** 2), 1e-6)
        # [C1] 体制匹配重加权: 偏向与查询时点"波动+趋势体制"相同的历史片段
        # (同形不同体制的近邻会误导, 例如 2015 股灾同形 vs 2017 慢牛同形)
        rg = _regime_vec(S, k0, t)
        if rg is not None:
            ws = np.empty(len(sel))
            for ii, g in enumerate(sel):
                ki = int(self.ind[g]); ei = int(self.end[g])
                rc = _regime_vec(S, ki, ei)
                ws[ii] = 1.0 if rc is None else math.exp(
                    -0.5 * ((rg[0] - rc[0]) / REG_VOL_SCALE) ** 2
                    - 0.5 * ((rg[1] - rc[1]) / REG_TREND_SCALE) ** 2)
            W = W * ws
            # [2026-09-05] 体制极度不匹配时 exp(-0.5*(Δ/scale)^2) 会下溢到 0, 使 W 整体为 0。
            #   下游 p_up = (up*W).sum()/W.sum()、_wq() 里的 ww.sum() 都是同一个分母,
            #   归零即 0/0 = NaN —— 分位数与概率全变 NaN 且不报错, 一直传到前端数据里。
            #   当前输入量级(局部波动率差要上百才可能)实际不会触发, 但代价是静默崩坏,
            #   故把下界补回来, 与上面第一次 clamp 同一口径。
            W = np.maximum(W, 1e-6)
        fa = self.fut[sel]
        fd = S[k0, t] + (self.fut[sel] - self.anch[sel][:, None])
        P = fa if mode == "abs" else (fd if mode == "delta" else 0.5 * fa + 0.5 * fd)
        if clip:
            P = np.clip(P, 0.0, 100.0)
        med = np.array([self._wq(P[:, hh], W, 0.5) for hh in range(H)])
        q25 = np.array([self._wq(P[:, hh], W, 0.25) for hh in range(H)])
        q75 = np.array([self._wq(P[:, hh], W, 0.75) for hh in range(H)])
        up = (P[:, -1] > S[k0, t]).astype(float)
        p_up = float((up * W).sum() / W.sum())
        # [C2] 共识度: 选中近邻相对全候选池的距离比(干净匹配=高共识, 史无前例=低共识)
        Dpool_med = float(np.median(D)) if len(D) else 1.0
        Dsel_med = float(np.median(Ds)) if len(Ds) else 1.0
        ratio = min(max(Dpool_med / Dsel_med, 0.0), 4.0) if Dsel_med > 1e-9 else 1.0
        consensus = float(ratio / (1.0 + ratio))
        if clip:
            med = np.clip(med, 0, 100)
            # [2026-09-05] 下面两行原为顺序赋值: 第一行改完 q25, 第二行读到的已是新 q25。
            #   正常路径下 _wq 对同一组 (P,W) 按 q 单调递增, 必有 q25<=q75, 结果碰巧正确;
            #   但一旦某步出现 q25>q75(权重异常/校准系数为负), 不会交换而是双双压到小的那个,
            #   区间悄悄退化成一个点 —— 与代码意图("保证 lo<=hi")不符且更难察觉。
            #   改成右侧整体求值后再绑定, 语义就是真正的 min/max, 与执行顺序解耦。
            q25, q75 = (np.clip(np.minimum(q25, q75), 0, 100),
                        np.clip(np.maximum(q25, q75), 0, 100))
        return {"median": med, "p25": q25, "p75": q75,
                "p_up": p_up, "n_used": int(len(sel)), "pool": pool,
                "consensus": consensus}


# [C1] 体制向量: 查询点前 REG_WIN 日分数路径的(局部波动率, 局部趋势), PIT 安全(只用当日及之前)
REG_WIN = 20
REG_VOL_SCALE = 4.0
REG_TREND_SCALE = 6.0

def _regime_vec(S, k, e):
    if e < REG_WIN or not np.isfinite(S[k, e - REG_WIN + 1:e + 1]).all():
        return None
    seg = S[k, e - REG_WIN + 1:e + 1]
    d = np.diff(seg)
    return (float(np.std(d)), float((seg[-1] - seg[0]) / REG_WIN))


# ==================== 基线 ([B3]) ====================
def _sd_diff(S, k, t, win=WIN):
    hist = S[k, max(0, t - win):t + 1]
    hist = hist[np.isfinite(hist)]
    if len(hist) < 30:
        return None, None
    return hist, float(np.std(np.diff(hist)))


def base_persist(S, k, t, H=HORIZON):
    hist, sd = _sd_diff(S, k, t)
    if sd is None:
        return None
    m = np.full(H, S[k, t])
    w = 0.674 * sd * np.sqrt(np.arange(1, H + 1))
    return m, np.clip(m - w, 0, 100), np.clip(m + w, 0, 100)


def base_momentum(S, k, t, H=HORIZON):
    prev = S[k, max(0, t - 10)]
    hist, sd = _sd_diff(S, k, t)
    if sd is None or not np.isfinite(prev):
        return None
    slope = (S[k, t] - prev) / 10.0
    m = np.clip(S[k, t] + slope * np.arange(1, H + 1), 0, 100)
    w = 0.674 * sd * np.sqrt(np.arange(1, H + 1))
    return m, np.clip(m - w, 0, 100), np.clip(m + w, 0, 100)


def base_meanrev(S, k, t, H=HORIZON, tau=20.0):
    hist, sd = _sd_diff(S, k, t)
    if sd is None or len(hist) < 60:
        return None
    mu = float(hist.mean())
    m = mu + (S[k, t] - mu) * np.exp(-np.arange(1, H + 1) / tau)
    w = 0.674 * sd * np.sqrt(np.arange(1, H + 1))
    return np.clip(m, 0, 100), np.clip(m - w, 0, 100), np.clip(m + w, 0, 100)


COMBO_W = 0.5        # [A6] 类比池权重先验中枢(仅作 doc 参考; 实际权重见下方自适应)
COMBO_W_LO = 0.3      # [C2] 共识度最低时(当前状态史无前例) -> 0.3 类比 + 0.7 持平基线
COMBO_W_HI = 0.7      # [C2] 共识度最高时(历史有清晰同类) -> 0.7 类比 + 0.3 持平基线


def base_combo(S, lib, k, t, H=HORIZON):
    """[A6][C2] 预测组合: 类比池点位与"持平"基线按共识度自适应收缩.

    DM 检验显示类比池点位并不显著优于持平基线(p=0.59), 说明点位里噪声占比高.
    预测组合理论指向: 平均能在不牺牲信息的前提下降方差.
    固定 0.5 是稳妥先验, 但类比池的"共识度"是时变的: 历史上能找到清晰同类(近邻远小于全池)
    -> 该信类比; 当前状态史无前例(近邻都很远) -> 应更多收敛到稳健的持平基线.
    [C2] 故权重自适应 w = 0.3 + 0.4*consensus01; consensus 来自 _fc_raw,
    是查询时点的即时统计量(相对全候选池的距离比), 非样本内调参, 无前视。
    [2026-09-05] 口径校正: 此前注释写"区间 [0.3,0.7]", 但 _fc_raw 里
      ratio = clamp(Dpool_med/Dsel_med, 0, 4), consensus = ratio/(1+ratio),
    故 consensus 上限是 4/5 = 0.8 而非 1.0 —— w 的实际区间是 [0.3, 0.62], 到不了 0.7。
    实现本身是安全的(更保守), 错的是注释; 写 0.7 会让人以为类比权重能占七成。
    注释与实现不符时以实现为准, 这里据实更正, 留待日后若要放开再改 clamp 上界。
    关键性质: 收缩保号(w*(med-s_t) 不改变符号), 故方向准确率与纯类比池完全一致,
    只压缩点位的过度外推 —— 这正是"方向有效、点位弱"的对症下药.
    """
    r = lib.forecast(k, t)
    if r is None:
        return None
    b = base_persist(S, k, t, H)
    if b is None:
        return None
    c = float(r.get("consensus", 0.5))
    w = COMBO_W_LO + (COMBO_W_HI - COMBO_W_LO) * c
    med = w * r["median"] + (1 - w) * b[0]
    q25 = w * r["p25"] + (1 - w) * b[1]
    q75 = w * r["p75"] + (1 - w) * b[2]
    return (np.clip(med, 0, 100), np.clip(q25, 0, 100), np.clip(q75, 0, 100), r["p_up"])


def base_combo_mkt(S, lib, k, t, H=HORIZON):
    """[C4] 市场因子分解推演: 行业分 = β·市场(沪深300)分 + 截距 C + 特质残差.

    把预测拆成两路, 各自用最擅长的类比池:
      · 系统性部分 β·市场分  —— 用"市场自身"类比池推演(样本=全市场体制, 比单行业丰富得多,
        且市场极端行情的样本量远大于任一行业, 对系统性大幅波动的捕捉更稳)
      · 特质残差          —— 用"行业自身"类比池推演(形态更纯, 不受其它行业噪声干扰)
    两路分别推演后按 β/C 重构: final[h] = β·mkt_forecast[h] + C + idio_forecast[h].

    PIT 安全: mkt/idio 行均由截至 t 的已完结片段构造; β/C 用全样本 OLS(只用于"分解定义",
    不含未来); 推演本身只用 ≤t 信息. clip=False 应用到 idio 推演(残差可负, 不可截断到[0,100]).
    """
    if lib.mkt_idx is None or lib.idio_of is None or lib.beta_map is None:
        return None
    mi = lib.mkt_idx
    ii = lib.idio_of(k)
    rm = lib.forecast(mi, t)              # 市场(沪深300)分数路径推演
    ri = lib.forecast(ii, t, clip=False)   # 特质残差路径推演(可负, 不截断)
    if rm is None or ri is None:
        return None
    bc = lib.beta_map.get(k)
    if bc is None:
        return None
    beta, C = bc
    fm = np.asarray(rm["median"], dtype=float)
    fi = np.asarray(ri["median"], dtype=float)
    med = beta * fm + C + fi
    q25 = beta * np.asarray(rm["p25"], dtype=float) + C + np.asarray(ri["p25"], dtype=float)
    q75 = beta * np.asarray(rm["p75"], dtype=float) + C + np.asarray(ri["p75"], dtype=float)
    med = np.clip(med, 0, 100)
    # [2026-09-05] 同 _fc_raw: 改为整体求值后绑定。此处 beta 重构后顺序本就更可能颠倒
    #   (特质残差可负、beta*fm+C 可超出量程), 依赖"上一行改写的 q25"风险更大。
    q25, q75 = (np.clip(np.minimum(q25, q75), 0, 100),
                np.clip(np.maximum(q25, q75), 0, 100))
    # 方向概率: 系统性方向与特质方向等权结合(β>0 时两者同向)
    p_up = 0.5 * (rm.get("p_up") or 0.5) + 0.5 * (ri.get("p_up") or 0.5)
    return (med, q25, q75, float(p_up))


def base_randomwalk(S, k, t, H=HORIZON):
    hist = S[k, max(0, t - 20):t + 1]
    hist = hist[np.isfinite(hist)]
    if len(hist) < 10:
        return None
    dif = np.diff(hist)
    mu = float(dif.mean())
    sd = float(dif.std()) or 1e-6
    hh = np.arange(1, H + 1)
    m = S[k, t] + mu * hh
    w = 0.674 * sd * np.sqrt(hh)
    return np.clip(m, 0, 100), np.clip(m - w, 0, 100), np.clip(m + w, 0, 100)


# ==================== 回测 ([B1][B2][F3]) ====================
def calibration_factor(samples, horizon=None, smooth=5):
    """反解半宽系数: cal = median(|real-med| / 对应侧半宽) -> 使覆盖率恰好 50%.

    [A7] horizon 给定时返回长度 H 的逐步长系数. 单一全局系数只能保证"平均覆盖 50%",
    实测长步长(21-30日)覆盖率仅 46.4% 而短步长偏高 —— 平均对、局部错.
    不确定性随预测步长增长的速度本身需要被校准, 故按步长分别反解再轻度平滑(抑噪, 不强制单调).
    """
    if horizon is None:
        ratios = []
        for med, q25, q75, real in samples:
            for h in range(len(real)):
                hw = (q75[h] - med[h]) if real[h] >= med[h] else (med[h] - q25[h])
                if hw > 1e-9:
                    ratios.append(abs(real[h] - med[h]) / hw)
        if len(ratios) < 50:
            return 1.0
        return float(np.median(ratios))

    per_h = [[] for _ in range(horizon)]
    allr = []
    for med, q25, q75, real in samples:
        for h in range(min(len(real), horizon)):
            hw = (q75[h] - med[h]) if real[h] >= med[h] else (med[h] - q25[h])
            if hw > 1e-9:
                v = abs(real[h] - med[h]) / hw
                per_h[h].append(v)
                allr.append(v)
    if len(allr) < 50:
        return np.ones(horizon)
    gl = float(np.median(allr))
    arr = np.array([float(np.median(x)) if len(x) >= 50 else gl for x in per_h])
    k = max(1, int(smooth))
    if k > 1:
        pad = k // 2
        arr = np.convolve(np.pad(arr, (pad, pad), mode="edge"), np.ones(k) / k, mode="valid")[:horizon]
    return np.clip(arr, 0.2, 5.0)


def regime_of(s):
    """按预测时点已知的分数分区(PIT 安全, 不含任何未来信息), 用于条件校准 [A8]"""
    try:
        if s is None or not np.isfinite(s):
            return "mid"
    except TypeError:
        return "mid"
    return "cold" if s < 40 else ("hot" if s > 60 else "mid")


REGIME_CN = {"cold": "偏冷区 (<40)", "mid": "中性区 (40-60)", "hot": "偏热区 (>60)"}


def apply_pup_calib(p, calib):
    """[E2] 应用分箱保序校准映射. p: 原始概率(0-1), calib: run_backtest 输出的 p_up_calib dict."""
    if calib is None or p is None or not calib.get("map"):
        return p
    _nb = int(calib.get("nb", 10))
    _i = min(_nb - 1, int(min(max(float(p), 0.0), 0.999999) * _nb))
    return float(calib["map"][_i])


def median_bias_of(s, mb, shrink=0.5, thr=3.0):
    """[E3] 按预测时点分数查 median 偏差, 返回保守校正量 = shrink * bias (仅 |bias|>=thr 生效).
    正偏差=预测保守(实际更高) -> 校正量加在 median 上. mb: run_backtest 输出的 median_bias dict."""
    if mb is None or s is None:
        return 0.0
    try:
        sv = float(s)
    except (TypeError, ValueError):
        return 0.0
    for b in mb.get("bins", []):
        if b.get("lo", 0) <= sv < b.get("hi", 100):
            bias = float(b.get("bias", 0.0))
            return bias * shrink if abs(bias) >= thr else 0.0
    return 0.0


def calibration_regime(samples, metas, cal_h, min_n=300):
    """[A8] 在按步长校准([A7])之上, 再按预测时点所处区间做整体缩放.

    按步长校准后各步长覆盖率已贴近 50%, 但分区诊断显示中性区(40-60)仍只有 45.6% ——
    分数处于中枢时后续波动比模型预期更大(中枢是变盘的起点, 不是平静期).
    每个区间只用 1 个乘子(而非 30 个), 参数少不易过拟合;
    并按样本量加权归一化到均值 1, 保证整体覆盖率不被系统性放大或缩小.
    """
    acc = {}
    ch = np.asarray(cal_h, dtype=float)
    for (med, q25, q75, real), (_bi, s_t) in zip(samples, metas):
        g = regime_of(s_t)
        lo = med - (med - np.minimum(q25, q75)) * ch
        hi = med + (np.maximum(q25, q75) - med) * ch
        n = min(len(real), len(ch))
        for h in range(n):
            hw = (hi[h] - med[h]) if real[h] >= med[h] else (med[h] - lo[h])
            if hw > 1e-9:
                acc.setdefault(g, []).append(abs(real[h] - med[h]) / hw)
    if not acc:
        return {}
    out = {g: (float(np.median(v)) if len(v) >= min_n else 1.0) for g, v in acc.items()}
    tot = sum(len(v) for v in acc.values())
    wm = sum(out[g] * len(acc[g]) for g in acc) / tot if tot else 1.0
    if wm > 1e-9:
        out = {g: v / wm for g, v in out.items()}
    return {g: float(np.clip(v, 0.5, 2.0)) for g, v in out.items()}


def run_backtest(S, lib, lib_mkt=None, horizon=HORIZON, bt_back=BT_BACK, dates=None, industry_rows=None):
    """无重叠 walk-forward. 返回 (metrics_dict, cals) — cals 为各方法的逐步长校准系数
    [A2] 块级 Diebold-Mariano [A3] 条件覆盖诊断 [A4] 分年度稳定性 [A6] 组合 [A7] 按步长校准"""
    NK, T = S.shape
    t_end = T - horizon - 1
    t_start = max(320, t_end - bt_back)
    tpts = list(range(t_start, t_end + 1, horizon))
    NB = len(tpts)

    methods = {
        "knn": lambda k, t: (lambda r: None if r is None else (r["median"], r["p25"], r["p75"], r["p_up"]))(
            lib.forecast(k, t)),
        "combo": lambda k, t: base_combo(S, lib, k, t),
        "combo_mkt": lambda k, t: (base_combo_mkt(S, lib_mkt, k, t)
                                   if lib_mkt is not None else None),
        "persist": lambda k, t: (lambda r: None if r is None else (r[0], r[1], r[2], None))(base_persist(S, k, t)),
        "momentum": lambda k, t: (lambda r: None if r is None else (r[0], r[1], r[2], None))(base_momentum(S, k, t)),
        "meanrev": lambda k, t: (lambda r: None if r is None else (r[0], r[1], r[2], None))(base_meanrev(S, k, t)),
        "randomwalk": lambda k, t: (lambda r: None if r is None else (r[0], r[1], r[2], None))(base_randomwalk(S, k, t)),
    }
    raw = {m: [] for m in methods}          # (med,q25,q75,real)
    rmeta = {m: [] for m in methods}        # (block_index, s_t) 与 raw 平行, 供条件覆盖/年度分解
    dirs = {m: [] for m in methods}         # 每块正确率
    stats = {m: {"ae": 0.0, "se": 0.0, "n": 0, "cov_raw": 0, "cn": 0,
                 "nz": 0, "dn": 0} for m in methods}
    bloss = {m: [dict() for _ in range(NB)] for m in methods}   # [A2] block -> {k: (ae_end, se_path)}
    byear = []
    pu = []

    for bi, t in enumerate(tpts):
        byear.append(dates[t][:4] if (dates and t < len(dates)) else "?")
        blk = {m: [0, 0] for m in methods}
        for k in (industry_rows if industry_rows is not None else range(NK)):
            if not np.isfinite(S[k, t]):
                continue
            real = S[k, t + 1:t + 1 + horizon]
            if len(real) < horizon or not np.isfinite(real).all():
                continue
            s_t = float(S[k, t])
            for mname, fn in methods.items():
                r = fn(k, t)
                if r is None:
                    continue
                med, q25, q75, p_up = r
                st = stats[mname]
                ae = abs(real[-1] - med[-1])
                se = float(((real - med) ** 2).mean())
                st["n"] += 1
                st["ae"] += ae
                st["se"] += se
                lo = np.minimum(q25, q75)
                hi = np.maximum(q25, q75)
                st["cov_raw"] += int(((real >= lo) & (real <= hi)).sum())
                st["cn"] += horizon
                raw[mname].append((med, q25, q75, real))
                rmeta[mname].append((bi, s_t))
                bloss[mname][bi][k] = (ae, se)
                # [2026-09-05] p_up 的样本收集原先嵌在下面 |变动|>=1.0 的过滤里, 已移出。
                #   那个过滤是为"方向准确率"服务的: 只在变动足够大时才判方向, 免得被噪声淹没。
                #   但 p_up 的语义是 P(real[-1] > s_t), 标签与变动幅度无关 —— 按幅度过滤不是
                #   随机抽样, 会把小变动整体剔除, 让标签分布向大涨/大跌两端倾斜。
                #   实测被剔除样本占 3.9%(992 个中的 39 个), 其中涨跌 19:20 基本对称,
                #   对当前 AUC 影响很小; 但口径应当正确 —— 一旦 p_up 将来用于概率校准
                #   (Brier/保序映射), 这种条件过滤会引入真实的分布偏差。
                if mname == "knn" and p_up is not None:
                    pu.append((p_up, 1 if real[-1] > s_t else 0))
                if abs(real[-1] - s_t) >= 1.0:
                    blk[mname][1] += 1
                    st["dn"] += 1
                    if abs(med[-1] - s_t) > 1e-9:
                        st["nz"] += 1
                    if np.sign(med[-1] - s_t) == np.sign(real[-1] - s_t):
                        blk[mname][0] += 1
        for mname in methods:
            ok, nn = blk[mname]
            dirs[mname].append(ok / nn if nn else None)

    out = {}
    cals = {}
    for mname in methods:
        st = stats[mname]
        if st["n"] == 0:
            continue
        cal = calibration_factor(raw[mname], horizon=horizon)   # [A7] 逐步长系数
        creg = calibration_regime(raw[mname], rmeta[mname], cal)   # [A8] 分区乘子
        cals[mname] = {"h": cal, "reg": creg}
        cov_cal = 0
        cn = 0
        for (med, q25, q75, real), (_b, s_t) in zip(raw[mname], rmeta[mname]):
            c2 = np.asarray(cal, dtype=float) * creg.get(regime_of(s_t), 1.0)
            lo = med - (med - np.minimum(q25, q75)) * c2
            hi = med + (np.maximum(q25, q75) - med) * c2
            cov_cal += int(((real >= lo) & (real <= hi)).sum())
            cn += len(real)
        m, tstat, p, nb = block_ttest(dirs[mname])
        # 退化检测: 若绝大多数样本的预测变动为 0(如"持平"基线), 方向指标无意义 -> 置 None
        degen = st["dn"] > 0 and st["nz"] / st["dn"] < 0.5
        if degen:
            m = tstat = p = None
        out[mname] = {
            "dir_acc": round(m, 3) if m is not None else None,
            "block_t": round(tstat, 2) if tstat is not None else None,
            "block_p": round(p, 4) if p is not None else None,
            "no_direction": bool(degen),
            "n_blocks": nb,
            "mae_end": round(st["ae"] / st["n"], 2),
            "rmse_path": round(math.sqrt(st["se"] / st["n"]), 2),
            "coverage_raw": round(st["cov_raw"] / st["cn"], 3),
            "coverage_cal": round(cov_cal / cn, 3) if cn else None,
            "cal": round(float(np.mean(cal)), 3),
            "cal_h1": round(float(np.asarray(cal).ravel()[0]), 3),
            "cal_h30": round(float(np.asarray(cal).ravel()[-1]), 3),
            "cal_reg": {g: round(v, 3) for g, v in creg.items()},
            "n": st["n"],
        }
    if pu:
        ps = np.array([a for a, _ in pu])
        ys = np.array([b for _, b in pu])
        if 0 < ys.sum() < len(ys):
            rk = np.argsort(np.argsort(ps)) + 1
            n1 = float(ys.sum())
            n0 = float(len(ys) - n1)
            out["p_up_auc"] = round(float((rk[ys == 1].sum() - n1 * (n1 + 1) / 2) / (n1 * n0)), 3)

    # ---------- [A2] 块级 Diebold-Mariano ----------
    def _dm_pair(mA, mB):
        """gain 正值 = mA 比 mB 更准. 只用两者都出了预测的同 (时点,行业) 配对"""
        ge, gs = [], []
        for bi in range(NB):
            a, b = bloss[mA][bi], bloss[mB][bi]
            ks = set(a) & set(b)
            if not ks:
                ge.append(None)
                gs.append(None)
                continue
            ge.append(sum(b[k][0] - a[k][0] for k in ks) / len(ks))
            gs.append(sum(b[k][1] - a[k][1] for k in ks) / len(ks))
        me, te, pe, nb = block_dm(ge)
        ms, ts, ps_, _ = block_dm(gs)
        return {
            "gain_mae": round(me, 3) if me is not None else None,
            "dm_t": round(te, 2) if te is not None else None,
            "dm_p": round(pe, 4) if pe is not None else None,
            "gain_mse": round(ms, 3) if ms is not None else None,
            "dm_t_mse": round(ts, 2) if ts is not None else None,
            "dm_p_mse": round(ps_, 4) if ps_ is not None else None,
            "n_blocks": nb,
        }

    have = [m for m in methods if stats[m]["n"] > 0]
    out["dm_vs_baseline"] = {m: _dm_pair("knn", m) for m in have if m != "knn"}
    if "combo" in have and "knn" in have:
        out["dm_combo_vs_knn"] = _dm_pair("combo", "knn")
        out["dm_combo_vs_persist"] = _dm_pair("combo", "persist")
    if "combo_mkt" in have and "persist" in have:
        out["dm_combo_mkt_vs_persist"] = _dm_pair("combo_mkt", "persist")

    # ---------- 每块校准后覆盖率(主推演方法), 供 [A3][A4] 复用 ----------
    # [C4] 主推演优先 combo_mkt(市场因子分解): 仅当其 DM vs 持平显著(p<0.05)且增益优于 combo 时升级,
    # 否则退回 combo(仍显著) / knn, 保证交付的永远是统计上站得住的主推演.
    MAIN = None
    for _m in ("combo_mkt", "combo"):
        if _m not in have:
            continue
        _d = out.get("dm_%s_vs_persist" % _m)
        if _d and _d.get("dm_p") is not None and _d["dm_p"] < 0.05:
            if MAIN is None or (_d.get("gain_mae") or 0) > (out.get("dm_%s_vs_persist" % MAIN, {}).get("gain_mae") or 0):
                MAIN = _m
    if MAIN is None:
        MAIN = "combo" if "combo" in have else ("knn" if "knn" in have else (have[0] if have else "knn"))
    _cm = cals.get(MAIN, {})
    cal_h_main = np.asarray(_cm.get("h", 1.0), dtype=float)
    cal_reg_main = _cm.get("reg", {})
    out["diag_on"] = MAIN

    def _band(med, q25, q75, s_t):
        """诊断用的带宽必须与最终交付一致: 逐步长系数 x 分区乘子"""
        c2 = cal_h_main * cal_reg_main.get(regime_of(s_t), 1.0)
        lo = med - (med - np.minimum(q25, q75)) * c2
        hi = med + (np.maximum(q25, q75) - med) * c2
        return lo, hi

    bcov = [[0, 0] for _ in range(NB)]
    for (med, q25, q75, real), (bi, _s) in zip(raw[MAIN], rmeta[MAIN]):
        lo, hi = _band(med, q25, q75, _s)
        bcov[bi][0] += int(((real >= lo) & (real <= hi)).sum())
        bcov[bi][1] += len(real)

    # ---------- [A3] 条件覆盖诊断: 按预测步长 / 按当时所处区间 ----------
    acc = {}

    def _add(lab, bi, hit, n):
        arr = acc.setdefault(lab, [[0, 0] for _ in range(NB)])
        arr[bi][0] += hit
        arr[bi][1] += n

    hbins = [(1, 10), (11, 20), (21, 30)]
    for (med, q25, q75, real), (bi, s_t) in zip(raw[MAIN], rmeta[MAIN]):
        lo, hi = _band(med, q25, q75, s_t)
        inb = (real >= lo) & (real <= hi)
        for a, b in hbins:
            seg = inb[a - 1:b]
            if len(seg):
                _add("步长 %d-%d 日" % (a, b), bi, int(seg.sum()), int(len(seg)))
        _add(REGIME_CN[regime_of(s_t)], bi, int(inb.sum()), int(len(inb)))

    cov_diag = []
    for lab, arr in acc.items():
        rates = [h / n for h, n in arr if n > 0]
        if len(rates) < 3:
            continue
        mm = sum(rates) / len(rates)
        sd = (sum((x - mm) ** 2 for x in rates) / (len(rates) - 1)) ** 0.5
        th = sum(h for h, n in arr)
        tn = sum(n for h, n in arr)
        lr, plr = kupiec_lr(th, tn, 0.5)
        cov_diag.append({
            "bin": lab,
            "cov": round(mm, 3),
            "se": round(sd / math.sqrt(len(rates)), 3),
            "n": tn,
            "n_blocks": len(rates),
            "kupiec_lr": round(lr, 2) if lr is not None else None,
            "kupiec_p": round(plr, 4) if plr is not None else None,
            "off": round(abs(mm - 0.5), 3),
        })
    cov_diag.sort(key=lambda r: -r["off"])
    out["cov_diag"] = cov_diag

    # ---------- [A4] 分年度稳定性 ----------
    ybi = {}
    for bi, y in enumerate(byear):
        ybi.setdefault(y, []).append(bi)
    ystab = []
    for y in sorted(ybi):
        bis = ybi[y]
        dr = [dirs[MAIN][bi] for bi in bis if dirs[MAIN][bi] is not None]
        aes = [v[0] for bi in bis for v in bloss[MAIN][bi].values()]
        ch = sum(bcov[bi][0] for bi in bis)
        cn = sum(bcov[bi][1] for bi in bis)
        ystab.append({
            "year": y,
            "dir_acc": round(sum(dr) / len(dr), 3) if dr else None,
            "mae_end": round(sum(aes) / len(aes), 2) if aes else None,
            "cov_cal": round(ch / cn, 3) if cn else None,
            "n_blocks": len(bis),
            "n": len(aes),
        })
    out["year_stability"] = ystab
    # [2026-09-03] "最差年份"可比性门槛: 仅 2 块(60样本)的年份方向波动 ±10pp 是纯噪声,
    # 让它主导"最差年份"叙述会误导(实测 2022 仅 2 块). 要求 >=6 块(约半年)才进入比较.
    ok_y = [r for r in ystab if r["dir_acc"] is not None and r["n_blocks"] >= 6]
    if ok_y:
        w = min(ok_y, key=lambda r: r["dir_acc"])
        out["worst_year"] = {"year": w["year"], "dir_acc": w["dir_acc"], "n_blocks": w["n_blocks"]}
        out["year_win_rate"] = round(sum(1 for r in ok_y if r["dir_acc"] > 0.5) / len(ok_y), 3)

    out["nrm_pit"] = {"pit_days": int(getattr(lib, "nrm_pit_used", 0)),
                      "fallback_days": int(getattr(lib, "nrm_fallback", 0))}
    out["n_blocks_total"] = len(tpts)
    out["step"] = horizon

    # ---------- [E2] p_up 概率保序校准 (isotonic binning) ----------
    # 深度回测发现 knn 的 p_up 系统性失准: 低端低估(预测0.05实际0.22)、高端高估(0.95->0.83),
    # Brier 0.2266。用回测窗口(全部为历史)的无重叠样本拟合分箱保序映射, 单调化后应用,
    # 使概率读数可信。AUC 是排序指标, 单调校准不改变 AUC; 但 Brier 分数(概率校准质量)显著改善。
    # PIT 安全: 映射只用 <= 最新时点的历史样本, 是固定低维函数(10桶), 过拟合风险极小。
    # [2026-09-03 样本外否决] walk-forward 滚动验证(验证块12+ n=589, fit 最近5/8/13/全部块 x
    # shrink 0.5/1.0)显示: isotonic 映射应用后 Brier 无一配置改善(raw 0.2003 -> 0.2003~0.2274,
    # 最佳仅打平), 一次性前拟后验同样恶化 27.9% —— 概率-频率关系非平稳(随市场 regime 漂移),
    # 固定映射的样本内收益(Brier 0.202->0.199)是拟合窗口假象. 故 compute.py 已停用映射应用,
    # 交付原始 p_up(AUC 排序能力无损); 本表仅存档诊断供审计.
    _pup_iso = None
    if len(pu) >= 200:
        _pa = np.array([a for a, _ in pu])
        _py = np.array([b for _, b in pu])
        _nb = 10
        _edges = np.linspace(0.0, 1.0, _nb + 1)
        _freq, _cnt = [], []
        for _i in range(_nb):
            _m = (_pa >= _edges[_i]) & (_pa < _edges[_i + 1]) if _i < _nb - 1 \
                else (_pa >= _edges[_i]) & (_pa <= _edges[_i + 1])
            _cnt.append(int(_m.sum()))
            _freq.append(float(_py[_m].mean()) if _cnt[-1] else float("nan"))
        # PAV 单调化: 相邻违反桶按样本量加权合并(循环直到单调递增)
        _map = list(_freq)
        _changed = True
        while _changed:
            _changed = False
            for _i in range(_nb - 1):
                if _map[_i] > _map[_i + 1] and _cnt[_i] > 0 and _cnt[_i + 1] > 0:
                    _v = (_map[_i] * _cnt[_i] + _map[_i + 1] * _cnt[_i + 1]) / (_cnt[_i] + _cnt[_i + 1])
                    _map[_i] = _map[_i + 1] = _v
                    _changed = True
        # 空桶外插: 用最近非空桶值填充(保持单调)
        _last = None
        for _i in range(_nb):
            if _cnt[_i] > 0:
                _last = _map[_i]
            elif _last is not None:
                _map[_i] = _last
        _first = None
        for _i in range(_nb - 1, -1, -1):
            if _cnt[_i] > 0:
                _first = _map[_i]
            elif _first is not None:
                _map[_i] = _first
        _map = [float(np.clip(v, 0.0, 1.0)) for v in _map]
        _cal_p = np.array([_map[min(_nb - 1, int(min(max(p, 0.0), 0.999999) * _nb))] for p in _pa])
        _b_raw = float(np.mean((_pa - _py) ** 2))
        _b_cal = float(np.mean((_cal_p - _py) ** 2))
        _pup_iso = {"nb": _nb, "mid": [round(float((_edges[i] + _edges[i + 1]) / 2), 2) for i in range(_nb)],
                    "map": [round(v, 4) for v in _map], "n": _cnt,
                    "brier_raw": round(_b_raw, 4), "brier_cal": round(_b_cal, 4),
                    "n_samples": len(_pa),
                    "note": "诊断存档(compute.py 已停用应用, 见上注): 分箱保序映射样本内 Brier %.4f -> %.4f, "
                            "但 walk-forward 样本外无一配置改善(raw 0.2003 起), 概率-频率关系非平稳无法外推; "
                            "AUC(排序能力)不受单调映射影响" % (_b_raw, _b_cal)}
        out["p_up_calib"] = _pup_iso

    # ---------- [E3] median 分区偏差 (按预测时点分数区间) ----------
    # 深度回测发现 median 存在系统性偏差: 中枢区(40-60)预测 30 日末点偏低 ~7 分(熊市记忆),
    # 超卖区(<25)预测偏高 ~4 分。拟合各分区的平均偏差, 供主流程做保守(半量)校正。
    # [2026-09-03 样本外否决] walk-forward 验证(验证块12+): 前拟合半量/全量校正应用后 MAE
    # 无改善甚至恶化(raw 16.927 -> 半量 16.985 +0.34% / 全量 17.353 +2.52%; 样本内 15.423 的
    # "改善 8.9%"为拟合窗口假象). 偏差随 regime 漂移, 固定平移无法外推. 故 compute.py 已停用
    # median 平移, 交付原始 median; 本表仅存档诊断供审计.
    _mb = None
    if len(raw.get(MAIN, [])) >= 300:
        _bins = [(0, 25), (25, 40), (40, 50), (50, 60), (60, 75), (75, 101)]
        _acc = {b: [] for b in _bins}
        for (_med, _q25, _q75, _real), (_bi, _s) in zip(raw[MAIN], rmeta[MAIN]):
            _sd = _s if _s is not None else 50.0
            for _lo, _hi in _bins:
                if _lo <= _sd < _hi:
                    _acc[(_lo, _hi)].append(float(_real[-1] - _med[-1]))
                    break
        _mb = {"bins": [{"lo": lo, "hi": min(hi, 100), "bias": round(sum(v) / len(v), 3),
                         "n": len(v)} for (lo, hi), v in _acc.items() if len(v) >= 50],
               "note": "诊断存档(compute.py 已停用应用): 按预测时点分数分区统计 median 末点偏差 mean(real-med); "
                       "正=预测保守(实际更高). 样本外验证半量/全量平移均无改善(MAE +0.34%/+2.52%), 偏差随 regime "
                       "漂移, 固定平移无法外推, 故不再交付校正值"}
        out["median_bias"] = _mb
    return out, cals
