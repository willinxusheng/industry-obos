#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""R9 探针: p_up 后验映射在真样本外到底能不能改善 Brier?

板上原话:「概率未做后验映射，样本外验证无改善」。该结论在日志只有零星成熟时得出。
现在成熟样本 306 行(10 asof × 31 行业)，用**时间块对半交叉**重新检验:
  - 折 A: 前 5 个 asof 拟合映射 -> 后 5 个 asof 评估
  - 折 B: 后 5 个拟合 -> 前 5 个评估
两种映射: ① Platt logistic  ② 十分位分箱均值(与板上 p_up_calib 同款口径)
评估: 样本外 Brier / AUC, 与未映射基线对比。
"""
import json
import math
import os

BASE = os.path.dirname(os.path.abspath(__file__))


def load_rows():
    seen = {}
    with open(os.path.join(BASE, "data", "prediction_log.jsonl"), encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            r = json.loads(line)
            if r.get("asof") and r.get("code"):
                seen[(r["asof"], r["code"])] = r   # 同日同代码幂等去重
    return seen


def mature_rows(seen, dates, scores, horizon=30):
    idx = {d: i for i, d in enumerate(dates)}
    last = len(dates) - 1
    rows = []
    for (a, c), r in sorted(seen.items()):
        i = idx.get(a)
        if i is None or i + horizon > last:
            continue
        sc = scores.get(c)
        if not sc:
            continue
        act = sc[i + horizon]
        if act is None or r.get("s") is None or r.get("p_up") is None:
            continue
        d_act = act - r["s"]
        if d_act == 0:
            continue
        rows.append({"asof": a, "p": float(r["p_up"]), "y": 1.0 if d_act > 0 else 0.0})
    return rows


def brier(rows, pf):
    return sum((pf(r) - r["y"]) ** 2 for r in rows) / len(rows)


def auc(rows, pf):
    pos = [pf(r) for r in rows if r["y"] == 1]
    neg = [pf(r) for r in rows if r["y"] == 0]
    if not pos or not neg:
        return None
    win = sum(1 for a in pos for b in neg if a > b) + 0.5 * sum(1 for a in pos for b in neg if a == b)
    return win / (len(pos) * len(neg))


def fit_platt(fit):
    """Newton 解二元 logistic: y ~ sigmoid(a*p + b)。"""
    xs = [r["p"] for r in fit]
    ys = [r["y"] for r in fit]
    a, b = 0.0, 0.0
    for _ in range(200):
        g1 = g2 = h11 = h12 = h22 = 0.0
        for x, y in zip(xs, ys):
            z = a * x + b
            z = max(-30.0, min(30.0, z))
            q = 1.0 / (1.0 + math.exp(-z))
            w = max(q * (1 - q), 1e-9)
            g1 += (y - q) * x
            g2 += (y - q)
            h11 += w * x * x
            h12 += w * x
            h22 += w
        det = h11 * h22 - h12 * h12
        if abs(det) < 1e-12:
            break
        da = (g1 * h22 - g2 * h12) / det
        db = (h11 * g2 - h12 * g1) / det
        a += da
        b += db
        if abs(da) + abs(db) < 1e-10:
            break
    return lambda r: 1.0 / (1.0 + math.exp(-max(-30.0, min(30.0, a * r["p"] + b))))


def fit_decile(fit):
    """十分位分箱: 箱内实际命中率的均值映射, 测试侧按箱子查表。"""
    bins = {}
    for r in fit:
        k = min(9, int(r["p"] * 10))
        bins.setdefault(k, []).append(r["y"])
    mu = {k: sum(v) / len(v) for k, v in bins.items()}
    base = sum(r["y"] for r in fit) / len(fit)

    def f(r):
        k = min(9, int(r["p"] * 10))
        return mu.get(k, base)
    return f


def main():
    bench = json.load(open(os.path.join(BASE, "data", "benchmark.json"), encoding="utf-8"))
    ob = json.load(open(os.path.join(BASE, "data", "industry_obos.json"), encoding="utf-8"))
    scores = {x["code"]: x["score"] for x in ob["industries"]}
    rows = mature_rows(load_rows(), bench["dates"], scores)
    asofs = sorted({r["asof"] for r in rows})
    print("成熟样本: %d 行 / %d 个 asof (%s ~ %s)" % (len(rows), len(asofs), asofs[0], asofs[-1]))

    def p_raw(r):
        return r["p"]

    # 全样本基线(仅作参照, 不作结论)
    print("\n[全样本参照] raw Brier %.4f | AUC %.4f" % (brier(rows, p_raw), auc(rows, p_raw)))

    half = len(asofs) // 2
    splits = [("前→后", asofs[:half], asofs[half:]), ("后→前", asofs[half:], asofs[:half])]
    res = {"raw": [], "platt": [], "decile": [], "raw_auc": [], "platt_auc": [], "decile_auc": []}
    for tag, fit_as, ev_as in splits:
        fit = [r for r in rows if r["asof"] in fit_as]
        ev = [r for r in rows if r["asof"] in ev_as]
        if len(fit) < 30 or len(ev) < 30:
            print("[%s] 样本不足 fit=%d ev=%d, 跳过" % (tag, len(fit), len(ev)))
            continue
        platt = fit_platt(fit)
        dec = fit_decile(fit)
        print("\n[%s] fit=%d 行(%s~%s) -> 样本外 ev=%d 行(%s~%s)"
              % (tag, len(fit), fit_as[0], fit_as[-1], len(ev), ev_as[0], ev_as[-1]))
        for name, pf in (("raw", p_raw), ("platt", platt), ("decile", dec)):
            b = brier(ev, pf)
            a_ = auc(ev, pf)
            res[name].append(b)
            res[name + "_auc"].append(a_)
            print("  %-6s Brier %.4f | AUC %.4f" % (name, b, a_))
    if res["raw"]:
        print("\n=== 两折均值 ===")
        for name in ("raw", "platt", "decile"):
            bs = res[name]
            as_ = [x for x in res[name + "_auc"] if x is not None]
            print("%-6s Brier %.4f | AUC %.4f" % (name, sum(bs) / len(bs), sum(as_) / len(as_)))


if __name__ == "__main__":
    main()
