# -*- coding: utf-8 -*-
"""预测日志复核(审计资产, 2026-09-04).

读取 data/prediction_log.jsonl(compute.py 每次主模式计算追加的预测快照), 对已成熟
(预测日 asof 之后第 30 个交易日的实际分数已可观测) 的行, 用"当时的预测快照" vs "后来
实际发生的分数"做无泄漏复核:
  · 方向命中:  实际终点 vs 预测中位终点 与 实际变动方向是否一致(仅 |实际变动|>=1 的样本)
  · p_up Brier: 预测升温概率 vs 实际是否升温(30 日终点高于起点) 的均方误差
  · 区间覆盖:  实际终点是否落入 [lo, hi](校准后交付区间)

诚实边界:
  1) 实际分数取 data/industry_obos.json 的最新重算序列 —— 分数经 walk-forward 权重/平滑
     修订, 重算有轻微非 PIT 修订; 方向级复核稳健, 点位级含小噪声(与 run_backtest 同源).
  2) 首批预测需 30 交易日后才可复核(系统自 2026-09-04 起建档), 成熟样本不足时输出提示,
     并引导使用官方 walk-forward 口径(run_backtest)作为替代基准.

用法:
  python audit_predictions.py            # 复核真实预测日志(只读 data/, 不写盘)
"""
import json
import os
import sys

BASE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE)

LOG = os.path.join(BASE, "data", "prediction_log.jsonl")
OBOS = os.path.join(BASE, "data", "industry_obos.json")
BMK = os.path.join(BASE, "data", "benchmark.json")


def load_rows():
    if not os.path.exists(LOG):
        return []
    rows = []
    with open(LOG, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except ValueError:
                continue
    return rows


def main():
    rows = load_rows()
    if not rows:
        print("prediction_log.jsonl 为空或不存在 —— 自 compute.py 建档起, 每次主模式计算会追加快照.")
        print("首个成熟样本需等预测日之后 30 个交易日, 在此之前请用官方 walk-forward 口径评估:")
        print("  (本地) python backtest_deep.py   或   复现 compute.py 阶段5 的 run_backtest 诊断.")
        return
    with open(OBOS, encoding="utf-8") as f:
        ob = json.load(f)
    with open(BMK, encoding="utf-8") as f:
        bmk = json.load(f)
    dates = bmk["dates"]
    dpos = {d: i for i, d in enumerate(dates)}
    score_by_code = {}
    for ind in ob["industries"]:
        score_by_code[ind["code"]] = ind["score"]
    asofs = sorted({r["asof"] for r in rows})
    print("预测日志: %d 行业天 @ %d 个 asof (%s → %s) | 数据截至 %s"
          % (len(rows), len(asofs), asofs[0], asofs[-1], ob["asof"]))
    # 成熟样本 = asof 对应日期存在且其后第 30 个交易日仍在数据内
    mature = []
    for r in rows:
        code, asof = r.get("code"), r.get("asof")
        sc = score_by_code.get(code)
        if sc is None or asof not in dpos:
            continue
        t = dpos[asof]
        if t + 30 >= len(sc):
            continue
        real30 = sc[t + 30]
        if real30 is None:
            continue
        mature.append((r, real30))
    if not mature:
        # [2026-09-04] 修复: 原实现 asof 不在本地 dates 时兜底输出 "?"——日志(CI 每日)
        # 几乎总是比本地 data/ 快照新, 该分支必命中, 导致永远无法给出成熟日期。
        # 现在区分两种未成熟原因并给出可执行指引。
        if asofs[0] in dpos:
            ti = dpos[asofs[0]]
            need = dates[ti + 30] if ti + 30 < len(dates) else "本地数据末端"
            print("尚无成熟样本(最早预测 %s, 数据截至 %s → 需数据覆盖到 %s 之后). 请等待积累或用 walk-forward 口径."
                  % (asofs[0], ob["asof"], need))
        else:
            print("尚无成熟样本: 预测日志最新 %s 早于本地数据 %s —— 请先 fetch 追平本地数据"
                  "(python fetch_data.py + fetch_benchmark.py + compute.py) 再复核;"
                  "或用官方 walk-forward 口径(backtest_deep.py / run_backtest)."
                  % (asofs[-1], ob["asof"]))
        return
    dir_ok = dir_n = 0
    brier = 0.0
    cov_hit = cov_n = 0
    by_regime = {}
    for r, real30 in mature:
        s = r.get("s")
        if s is None or real30 is None:
            continue
        med, lo, hi = r.get("med"), r.get("lo"), r.get("hi")
        pup = r.get("p_up")
        if abs(real30 - s) >= 1.0:
            dir_n += 1
            if med is not None and abs(med - s) > 1e-9 and \
               (med - s) * (real30 - s) > 0:
                dir_ok += 1
        if pup is not None:
            y = 1 if real30 > s else 0
            brier += (pup - y) ** 2
        if lo is not None and hi is not None:
            cov_n += 1
            cov_hit += 1 if (lo <= real30 <= hi) else 0
        g = "cold" if s < 40 else ("hot" if s > 60 else "mid")
        st = by_regime.setdefault(g, [0, 0, 0.0])   # dir_ok, dir_n, brier
        if abs(real30 - s) >= 1.0 and med is not None and abs(med - s) > 1e-9:
            st[1] += 1
            st[0] += 1 if (med - s) * (real30 - s) > 0 else 0
        if pup is not None:
            st[2] += (pup - (1 if real30 > s else 0)) ** 2
    print("\n===== 预测快照复核(成熟 %d 行) =====" % len(mature))
    if dir_n:
        print("方向命中(30d): %.1f%% (%d/%d)" % (100.0 * dir_ok / dir_n, dir_ok, dir_n))
    if mature and any(r.get("p_up") is not None for r, _ in mature):
        n_p = sum(1 for r, _ in mature if r.get("p_up") is not None)
        print("p_up Brier  : %.4f (n=%d)" % (brier / n_p, n_p))
    if cov_n:
        print("区间覆盖    : %.1f%% (%d/%d)  [目标 50%%]" % (100.0 * cov_hit / cov_n, cov_hit, cov_n))
    print("\n分区间:")
    for g in ("cold", "mid", "hot"):
        if g not in by_regime or by_regime[g][1] == 0:
            continue
        ok, n, br = by_regime[g]
        print("  %-4s: 方向 %.1f%% (%d/%d)%s" % (g, 100.0 * ok / n, ok, n,
              "" if n == 0 else ""))


if __name__ == "__main__":
    main()
