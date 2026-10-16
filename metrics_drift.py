# -*- coding: utf-8 -*-
"""模型指标漂移监控 —— 读 data/model_metrics_log.jsonl, 给出指标序列与漂移标记。

为什么需要读方：compute.py 每交易日往 model_metrics_log.jsonl 追加一行"模型当时长什么样"
（方向准确率/AUC/Brier/覆盖/MAE/市场状态）。只写不读就是死数据 —— 本仓库判别死数据的
办法就是全仓库 grep 只有写入点、没有读取点。"准确率是不是在缓慢退化"这个问题必须有人
定期回答，否则要等到看板明显不对劲才被发现，而那时已经积累了十几个交易日的无声漂移。

判定纪律（两类问题分开）：
  - **契约问题 -> exit 1，必须真红**：坏 JSON / 同一 asof 出现两行 / 关键字段缺失。
    重复行会让窗口均值把同一天多次计入，"漂移"其实是日志自身重复造成的假象；
    与 prediction_log 同理，幂等一旦静默失效就会污染序列，且事后极难察觉。
  - **指标漂移 -> 只打印，不判红**：样本以交易日计，20 日内均值波动本身含大量噪声，
    拿它当门禁会在正常波动下天天报警，最终被无视——比没有更糟。
    真要看趋势，看下面的表格与 z 值，由人判断。

用法：
    python metrics_drift.py            # 打印近期序列 + 漂移标记
    python metrics_drift.py --window 60
运行时会先做一次合成数据自检（不读真实日志）：自检失败直接 exit 1，
避免"统计写错了但每个月照样打印一张表"这种最坏的失效方式。
"""
import json
import os
import sys

BASE = os.path.dirname(os.path.abspath(__file__))
LOG = os.path.join(BASE, "data", "model_metrics_log.jsonl")

# 每行必须有的字段（值可以为 None，但键必须在）。缺键说明写入端改过 schema，
# 而读取端还按老假设算 —— 那会得到一整份"看着正常"的错报告。
REQUIRED = ["asof", "main", "dir_acc", "p_up_auc", "mae", "cov_cal", "mkt_state"]
# 参与漂移监控的数值字段
NUM_FIELDS = ["dir_acc", "p_up_auc", "brier", "mae", "rmse", "cov_cal", "cov_raw"]
# 各字段的"最小实际影响"：仅有统计显著性(z)不够，变化幅度本身也得够大。
# 否则参考窗口方差极小时，0.001 的波动也能凑出 z=3 —— 那是噪声被算法放大，不是漂移。
MIN_EFFECT = {"dir_acc": 0.02, "p_up_auc": 0.01, "brier": 0.01,
              "mae": 0.5, "rmse": 0.5, "cov_cal": 0.02, "cov_raw": 0.02}
# 窗口标准差的下限：完全平稳的窗口 sd=0，除零会让"明显下坠"变成"检测不出来"
# （这正是本脚本自检第一次运行就抓出来的洞）。给个极小的下限，让"稳定基线被打破"仍能报出。
SD_FLOOR = 1e-9
# 判定基线所需的最少「同一主推演方法」样本数（见 analyze 的第二处修正）。
MIN_REF = 8


def load(path):
    if not os.path.exists(path):
        return None
    rows = []
    seen = {}
    with open(path, encoding="utf-8") as f:
        for i, line in enumerate(f, 1):
            line = line.strip()
            if not line:
                continue
            try:
                r = json.loads(line)
            except ValueError as e:
                raise SystemExit("CONTRACT FAIL: 第 %d 行不是合法 JSON (%s)" % (i, e))
            miss = [k for k in REQUIRED if k not in r]
            if miss:
                raise SystemExit("CONTRACT FAIL: 第 %d 行缺字段 %s" % (i, ", ".join(miss)))
            a = r.get("asof")
            if a in seen:
                raise SystemExit("CONTRACT FAIL: asof=%s 出现两行(第 %d 行与第 %d 行) —— "
                                 "幂等失效会让同一天被重复计入均值" % (a, seen[a], i))
            seen[a] = i
            rows.append(r)
    rows.sort(key=lambda r: r.get("asof") or "")
    return rows


def analyze(rows, window=20):
    """返回 (stats, flags, meta)。stats: 字段 -> (mean, std, n)，取"除最后一行外"的尾部 window 行。

    基线必须**排除最后一行**：否则最新值被算进均值与标准差里，越极端越把自己的基线拉过去，
    漂移反而更难被检出（同型错误：用含异常值的窗口去判断该异常值）。

    [2026-10-16] 基线还必须**限定在同一主推演方法内**（本函数第二处修正）。原因：
      compute.py 的 main 由 DM 门控自动选择（obos_core [C4]），线上实测 2026-10-09 从
      combo_mkt 切成 combo。两种方法的指标**天生差一截**（实测窗口内 combo_mkt 方向
      约 0.694 / combo 约 0.665，Brier 0.199 vs 0.216）。把它们混进同一条基线会同时造成
      两类错误：
        · 标准差被方法间差异撑大 -> **真漂移被稀释**，该报的报不出来；
        · 切换日那一跳被当成漂移 -> **假警报**，读的人分不清"模型退化"还是"换了方法"。
      故按 main 分组；**同方法样本不足时不判漂移**（宁可不说），而不是退回混池 ——
      退回混池等于把上面两个错误原样带回来。
    """
    latest = rows[-1] if rows else None
    cur = (latest or {}).get("main")
    pool = rows[:-1][-window:] if len(rows) > 1 else []
    same = [r for r in pool if r.get("main") == cur]
    meta = {"main": cur, "n_pool": len(pool), "n_same": len(same), "window": window}
    if len(same) < MIN_REF:
        # 主推演刚切换（或日志太短）—— 同方法可比样本不够，明确说"不可判断"。
        meta["scope"] = "insufficient_same_method"
        return {}, [], meta
    ref = same
    meta["scope"] = "same_method"
    stats, flags = {}, []
    for k in NUM_FIELDS:
        vs = [r.get(k) for r in ref]
        vs = [float(v) for v in vs if isinstance(v, (int, float))]
        if len(vs) < MIN_REF:
            continue
        m = sum(vs) / len(vs)
        var = sum((v - m) ** 2 for v in vs) / (len(vs) - 1)
        sd = max(var ** 0.5, SD_FLOOR)
        stats[k] = (m, sd, len(vs))
        lv = latest.get(k) if latest else None
        if isinstance(lv, (int, float)):
            z = (float(lv) - m) / sd
            # 两个条件同时满足才算：① 幅度够大(MIN_EFFECT) ② 相对基线够远(2σ)
            if abs(float(lv) - m) >= MIN_EFFECT.get(k, 0.0) and abs(z) >= 2.0:
                flags.append((k, float(lv), m, z))
    return stats, flags, meta


def selfcheck():
    """合成数据自检：三类形态的期望输出必须都对，否则 exit 1。

    这是本脚本唯一的防假绿手段 —— 漂移监控天然"总是打印出一张表"，
    统计写错了也不会报错，只会安静地给出错误结论。
    """
    def mk(n, dir_acc=0.70, auc=0.76, mae=18.0, cov=0.50, main="combo_mkt"):
        return [{"asof": "2026-01-%02d" % (i + 1), "main": main,
                 "dir_acc": dir_acc, "p_up_auc": auc, "mae": mae, "cov_cal": cov,
                 "mkt_state": "偏冷"} for i in range(n)]

    # ① 平稳序列 -> 不得报漂移
    s, fl, _ = analyze(mk(30))
    if fl:
        raise SystemExit("SELFCHECK FAIL: 平稳序列被误报漂移 %s" % fl)
    if abs(s["dir_acc"][0] - 0.70) > 1e-9:
        raise SystemExit("SELFCHECK FAIL: 基线均值算错 %s" % (s["dir_acc"],))
    # ② 末行显著偏离（+/- 远超窗口标准差）-> 必须报出来
    rows = mk(30)
    rows.append(dict(rows[-1], asof="2026-02-01", dir_acc=0.10))
    _, fl, _ = analyze(rows)
    if not any(f[0] == "dir_acc" for f in fl):
        raise SystemExit("SELFCHECK FAIL: 明显下坠未被检出 %s" % (fl,))
    # ③ 样本不足 -> 不得报漂移（宁可不说，不给噪声贴标签）
    _, fl3, _ = analyze(mk(6))
    if fl3:
        raise SystemExit("SELFCHECK FAIL: 样本不足仍报漂移 %s" % (fl3,))
    # ④ [2026-10-16] 主推演切换造成的**固有水平差**不得被当成漂移。
    #    线上实测：2026-10-09 由 combo_mkt 切成 combo，而 combo 的方向准确率天然低约
    #    3pp（0.665 vs 0.694）。旧实现（不分方法、混池取基线）在切换当天必然报 z<-2。
    rows = mk(30, main="combo_mkt", dir_acc=0.694)
    rows.append(dict(mk(1, main="combo", dir_acc=0.665)[0]))
    _, fl4, m4 = analyze(rows)
    if fl4:
        raise SystemExit("SELFCHECK FAIL: 主推演切换的固有水平差被误报漂移 %s" % (fl4,))
    if m4["scope"] != "insufficient_same_method":
        raise SystemExit("SELFCHECK FAIL: 切换后同方法样本不足，本应明确'不判'，实得 scope=%s"
                         % m4["scope"])
    # 非空跑证明：同一份数据若按**旧口径**（混池）算，必须真的会误报 —— 否则本用例
    # 起不到回归作用，"新增的修复"可能只是碰巧通过。
    _mix = [r["dir_acc"] for r in rows[:-1][-20:]]
    _mm = sum(_mix) / len(_mix)
    _sd = max((sum((v - _mm) ** 2 for v in _mix) / (len(_mix) - 1)) ** 0.5, SD_FLOOR)
    if abs((0.665 - _mm) / _sd) < 2.0:
        raise SystemExit("SELFCHECK 无效: 该用例在旧口径下也不会误报，起不到回归作用")
    # ⑤ 反向用例：同方法内的真下坠**仍然必须报** —— 防止"为躲误报而把敏感度砍没"
    rows = mk(29, main="combo", dir_acc=0.70)
    rows.append(dict(rows[-1], asof="2026-02-01", dir_acc=0.10))
    _, fl5, m5 = analyze(rows)
    if m5["scope"] != "same_method":
        raise SystemExit("SELFCHECK FAIL: 同方法样本充足却未判，scope=%s" % m5["scope"])
    if not any(f[0] == "dir_acc" for f in fl5):
        raise SystemExit("SELFCHECK FAIL: 同方法内下坠未被检出 %s" % (fl5,))
    print("selfcheck OK: 平稳不报 / 下坠必报 / 样本不足不报 / "
          "方法切换不误报(且旧口径确会误报) / 同方法内下坠仍必报")


def main():
    selfcheck()
    window = 20
    if "--window" in sys.argv:
        try:
            window = int(sys.argv[sys.argv.index("--window") + 1])
        except (IndexError, ValueError):
            raise SystemExit("--window 需要一个整数")
    rows = load(LOG)
    if rows is None:
        print("model_metrics_log.jsonl 尚不存在（首个交易日计算后自动生成）—— 无事可做")
        return
    print("模型指标日志: %d 个交易日 (%s ~ %s)" % (len(rows), rows[0]["asof"], rows[-1]["asof"]))
    tail = rows[-min(10, len(rows)):]
    print("\n近期序列（每交易日一行）：")
    print("  %-12s %-10s %7s %7s %7s %7s %7s  %s"
          % ("asof", "main", "dir", "auc", "brier", "mae", "cov", "市场"))
    for r in tail:
        def f(k, d=3):
            v = r.get(k)
            return ("%." + str(d) + "f") % v if isinstance(v, (int, float)) else "-"
        print("  %-12s %-10s %7s %7s %7s %7s %7s  %s"
              % (r["asof"], str(r.get("main"))[:10], f("dir_acc"), f("p_up_auc"),
                 f("brier"), f("mae", 2), f("cov_cal"), r.get("mkt_state") or "-"))
    stats, flags, meta = analyze(rows, window)
    # 主推演切换点必须显式打出来：切换本身会让指标整体位移，读的人得知道"这一跳不是退化"
    sw = [(rows[i]["asof"], rows[i - 1].get("main"), rows[i].get("main"))
          for i in range(1, len(rows)) if rows[i].get("main") != rows[i - 1].get("main")]
    if sw:
        print("\n主推演切换记录（跨切换比较指标无意义）：")
        for a, b, c in sw[-3:]:
            print("  %s  %s -> %s" % (a, b, c))
    if meta["scope"] == "insufficient_same_method":
        print("\n漂移监控: 当前主推演(%s)的同方法可比样本仅 %d 个交易日（需 >=%d），**本轮不判漂移**。"
              % (meta["main"] or "-", meta["n_same"], MIN_REF))
        print("  刻意不退回混池基线：把 combo 与 combo_mkt 混在一起算均值/标准差，"
              "既会稀释真漂移（方法间固有差把 sd 撑大），又会让切换日本身变成假警报。")
    elif not stats:
        print("\n漂移监控: 参考窗口不足 %d 个交易日，暂不给基线（避免拿噪声当结论）" % MIN_REF)
    else:
        print("\n漂移基线（最近 %d 行中除末行外、且**同为 %s**，n=%d）："
              % (window, meta["main"], max(s[2] for s in stats.values())))
        for k in NUM_FIELDS:
            if k in stats:
                m, sd, n = stats[k]
                print("  %-9s 均值 %8.4f  标准差 %7.4f" % (k, m, sd))
        if flags:
            print("\n⚠️ 偏离基线超过 2 个标准差（仅提示，不判红）：")
            for k, lv, m, z in flags:
                print("  %-9s 最新 %8.4f  vs 基线 %8.4f  z=%+.2f" % (k, lv, m, z))
        else:
            print("\n✅ 最新一行各项均在基线 2 个标准差内")
    cnt = {}
    for r in rows:
        key = r.get("mkt_state") or "-"
        cnt[key] = cnt.get(key, 0) + 1
    print("\n市场状态分布（全日志）: " + "  ".join("%s %d" % (k, v) for k, v in
                                                sorted(cnt.items(), key=lambda x: -x[1])))
    print("\n说明：漂移标记只作提示。窗口以交易日计，样本少时波动天然大；"
          "真正要防的是「连续多日单向偏移」，请结合上表逐行判断。")


if __name__ == "__main__":
    main()
