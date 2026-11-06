#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""R9 探针: 尾部增长不变性。CI 每交易日尾部多 1 根 K 线, 这是常态路径。
日历锚定后, 历史网格点不变、分数只看过去(PIT), 因此尾部 +1 根时
回测指标必须逐位不变(t_end 只移动 1 天, 通常不会多出一个 30 日网格点)。
若变 -> 说明某处仍把网格/权重挂在了"序列长度"上, 就是 R8 同类 bug 的尾部版本。
"""
import copy
import datetime
import io
import json
import os
import subprocess
import sys

BASE = os.path.dirname(os.path.abspath(__file__))


def snapshot_bt():
    d = json.load(open(os.path.join(BASE, "data", "industry_obos.json"), encoding="utf-8"))
    bt = d["backtest"]
    return {m: (bt.get(m, {}) or {}).get("dir_acc") for m in ("knn", "combo", "meanrev")}


def main():
    bak = {}
    # [2026-11-06] 教训: 日志文件也必须备份 —— 合成日 asof 会被 append_prediction_log
    # 当成"新的一天"写进日志(实测污染了 31 行 2026-11-09 假快照)。
    for fn in ("data/industry_klines.json", "data/benchmark.json",
               "data/prediction_log.jsonl", "data/sub_prediction_log.jsonl",
               "data/model_metrics_log.jsonl"):
        p = os.path.join(BASE, fn)
        bak[fn] = io.open(p, encoding="utf-8").read() if os.path.exists(p) else None

    try:
        # 基线
        subprocess.run([sys.executable, "compute.py"], cwd=BASE, check=True,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        base = snapshot_bt()

        # 尾部加 1 根合成 K 线: 复制末日, 日期 = 下一个交易日(简单 +1 自然日跳过周末)
        klines = json.loads(bak["data/industry_klines.json"])
        bench = json.loads(bak["data/benchmark.json"])
        last_d = bench["dates"][-1]
        nd = datetime.date.fromisoformat(last_d) + datetime.timedelta(days=1)
        while nd.weekday() >= 5:
            nd += datetime.timedelta(days=1)
        nd = nd.isoformat()
        bench["dates"].append(nd)
        bench["close"].append(bench["close"][-1])
        for code, v in klines.items():
            v["rows"].append(list(v["rows"][-1]))
            v["rows"][-1][0] = nd
        io.open(os.path.join(BASE, "data", "industry_klines.json"), "w",
                encoding="utf-8").write(json.dumps(klines, ensure_ascii=False))
        io.open(os.path.join(BASE, "data", "benchmark.json"), "w",
                encoding="utf-8").write(json.dumps(bench, ensure_ascii=False))

        subprocess.run([sys.executable, "compute.py"], cwd=BASE, check=True,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        grown = snapshot_bt()
    finally:
        for fn, txt in bak.items():
            p = os.path.join(BASE, fn)
            if txt is None:
                try:
                    os.remove(p)
                except OSError:
                    pass
            else:
                io.open(p, "w", encoding="utf-8").write(txt)
        # 还原数据后重算一次, 让 data/industry_obos.json 回到真实基线
        subprocess.run([sys.executable, "compute.py"], cwd=BASE, check=True,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    print("尾部 +1 根合成K线 (%s):" % nd)
    ok = True
    for m in base:
        same = base[m] == grown[m]
        ok = ok and same
        print("  %-8s base=%s grown=%s  %s" % (m, base[m], grown[m], "一致" if same else "变了 ❌"))
    print("判定:", "尾部增长不变 ✅" if ok else "尾部增长会换指标 ❌(R8 同类 bug 的尾部版)")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
