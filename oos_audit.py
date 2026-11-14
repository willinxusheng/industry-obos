# -*- coding: utf-8 -*-
"""真样本外复核(可复用模块, 2026-11-06)。

为什么单独成文件: 看板上的"方向准确率"来自 run_backtest 的 walk-forward —— 它用
**现在这套模型**回放 5 年历史, 网格虽无前后泄漏, 但它测的是"这套管线在历史样本上
的表现", 不是"当时写下的预测后来对没对"。唯一能回答后者的是 prediction_log.jsonl:
每天把预测快照落盘, 30 个交易日后拿实际值对账, 事后无法修改。

audit_predictions.py 是给人看的报告版(带口径说明与分区间明细), 本模块是给 compute.py
调用的数值版 —— 两者必须口径一致: 同一份日志、同一个 HORIZON、同一套
[方向: |实际变动|>=1.0 过滤] 与 [覆盖: 原始区间 lo..hi]。改这里必须同步改那边,
否则看板上的"样本外"与 CI 审计报告的"样本外"会各说各话。

口径争议留痕: 方向命中同时给两个数 ——
  · dir_all  : 全样本(|实际变动| 不论大小), 无偏, 但小变动近似噪声, 数字偏低;
  · dir_big  : |实际变动|>=1.0, 与看板 walk-forward 同口径, 但属于"条件于结果"的过滤,
               有偏(它剔掉的是振幅小的样本, 与预测质量无关)。两者都披露, 不挑好看的那个。
"""
import json
import math


def _mature_index(dates, asof, horizon):
    """asof 之后第 horizon 个交易日的下标; 无成熟样本返回 None。"""
    try:
        i = dates.index(asof)
    except ValueError:
        return None
    j = i + horizon
    return j if j < len(dates) else None


def audit_rows(rows, dates, scores, horizon=30, min_n=30):
    """rows: prediction_log 解析后的 dict 列表; dates: 基准交易日历;
    scores: {code: [score...]} 与 dates 对齐。返回 dict(样本不足时 matured=0)。"""
    idx = {d: i for i, d in enumerate(dates)}
    last = len(dates) - 1
    seen = {}
    for r in rows:
        if r.get("asof") and r.get("code"):
            seen[(r["asof"], r["code"])] = r      # 同日多次运行以最后一次为准
    hit_all = n_all = hit_big = n_big = 0
    n_mature = 0
    cov = n_cov = 0
    br = []
    by_reg = {"cold": [0, 0, 0, 0], "mid": [0, 0, 0, 0], "hot": [0, 0, 0, 0]}
    #                                  ^hit ^n  ^cov_hit ^cov_n
    asofs = set()
    for (a, code), r in seen.items():
        i = idx.get(a)
        if i is None or i + horizon > last:
            continue
        sc = scores.get(code)
        if not sc or i + horizon >= len(sc):
            continue
        act, s_t, med = sc[i + horizon], r.get("s"), r.get("med")
        if act is None or s_t is None or med is None:
            continue
        asofs.add(a)
        n_mature += 1
        d_act, d_fc = act - s_t, med - s_t
        # 方向只在【预测确实给了方向】时统计: med == s_t 表示"预测持平", 无从判定对错。
        #   与 audit_predictions.py 的 directed 判据一致(实测 310 行里 4 行预测持平,
        #   剔后 306 行, 与报告版的 57.2% 逐位相同)。覆盖/Brier 不受此过滤影响。
        g = "cold" if s_t < 40 else ("hot" if s_t > 60 else "mid")
        if abs(d_fc) > 1e-9:
            n_all += 1
            ok = 1 if d_act * d_fc > 0 else 0
            hit_all += ok
            if abs(d_act) >= 1.0:
                n_big += 1
                hit_big += ok
            by_reg[g][1] += 1
            by_reg[g][0] += ok
        lo, hi = r.get("lo"), r.get("hi")
        if lo is not None and hi is not None:
            n_cov += 1
            okc = 1 if lo <= act <= hi else 0
            cov += okc
            # [2026-11-14] 分 regime 覆盖 —— R10 实证：总体 46.9% 掩盖了
            #   中区 29.4% / 冷区 64.1% 的强分化，必须拆开披露。
            by_reg[g][3] += 1
            by_reg[g][2] += okc
        if r.get("p_up") is not None:
            br.append((r["p_up"] - (1.0 if d_act > 0 else 0.0)) ** 2)
    if n_all < min_n:
        return {"matured": 0, "n_asof": len(asofs), "need": "至少 %d 行成熟样本" % min_n}
    return {
        "matured": n_all,
        "n_mature_rows": n_mature,
        "n_asof": len(asofs),
        "span": [min(asofs), max(asofs)] if asofs else None,
        "dir_all": round(hit_all / n_all, 3),
        # 分母为 0 时给 None 而不是 0 —— 否则前端会显示"方向 0%", 看着像模型崩了
        "dir_big": round(hit_big / n_big, 3) if n_big else None,
        "n_big": n_big,
        "coverage": round(cov / n_cov, 3) if n_cov else None,
        "brier": round(sum(br) / len(br), 4) if br else None,
        "by_regime": {g: {"dir": round(v[0] / v[1], 3) if v[1] else None,
                          "n": v[1],
                          "cov": round(v[2] / v[3], 3) if v[3] else None,
                          "cov_n": v[3]}
                      for g, v in by_reg.items()},
    }


def load_log(path):
    rows = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    rows.append(json.loads(line))
                except ValueError:
                    continue
    return rows
