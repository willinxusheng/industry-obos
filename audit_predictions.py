# -*- coding: utf-8 -*-
"""预测日志复核(审计资产, 2026-09-04; 2026-09-05 修正执行时机与统计口径).

读取 data/prediction_log.jsonl(compute.py 每次主模式计算追加的预测快照), 对已成熟
(预测日 asof 之后第 30 个交易日的实际分数已可观测) 的行, 用"当时的预测快照" vs "后来
实际发生的分数"做无泄漏复核:
  · 方向命中:  实际终点 vs 预测中位终点 与 实际变动方向是否一致
  · p_up Brier: 预测升温概率 vs 实际是否升温(30 日终点高于起点) 的均方误差
  · 区间覆盖:  实际终点是否落入 [lo, hi](校准后交付区间)

⚠️ 执行时机(2026-09-05 修正, 曾是本脚本最致命的问题):
  本脚本读 data/industry_obos.json 取"后来实际发生的分数", 但 daily.yml 的提交清单
  只有 index.html / data.js / sub*.js / series.js / prediction_log.jsonl —— **从不提交
  data/industry_obos.json**(仓库里那份是永久陈旧的历史快照)。于是:
    · 脚本此前从未被 CI 调用过, 而单独 checkout 后在任何机器上跑, 日志都是新的、
      数据都是旧的 —— 永远落在"日志比数据新"的分支里, 审计结论恒为空;
    · 预测日志却每天照常累积, 等 2026-10 中首批样本成熟时才发现无人复核。
  正解: 必须紧跟在 compute.py 之后执行 —— 那个时刻 data/ 是本次刚算出来的最新结果,
  且 compute.py 已把当日快照追加进日志, 两者同日, 复核才成立。已接入 daily.yml。

诚实边界:
  1) 实际分数取 data/industry_obos.json 的最新重算序列 —— 分数经 walk-forward 权重/平滑
     修订, 重算有轻微非 PIT 修订; 方向级复核稳健, 点位级含小噪声(与 run_backtest 同源).
  2) 首批预测需 30 交易日后才可复核(系统自 2026-09-04 起建档), 成熟样本不足时输出提示
     并说明还差多少个交易日, 引导使用官方 walk-forward 口径(run_backtest)作为替代基准.

用法:
  python audit_predictions.py            # 复核真实预测日志(只读 data/, 不写盘)
退出码:
  0 = 正常(含"尚无成熟样本"这种预期内的早期状态)
  1 = 数据契约破损(文件缺失/坏 JSON/序列长度与日历不一致) —— 拒绝按错位下标出报告
"""
import json
import os
import sys

BASE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE)

# 预测期限与模型同源: 直接用 obos_core 的 HORIZON, 避免"日志按 30 天记、复核按 25 天读"
# 这类两边各自硬编码、悄悄对不上的情况(此处不设 fallback —— obos_core 若导入失败,
# 说明 compute.py 同样跑不起来, 安静降级只会让复核在错误期限上继续出数)。
from obos_core import HORIZON

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


def load_data():
    """读复核所需的两个数据文件. 缺失/坏 JSON 时给可执行指引, 而不是抛一屏栈。

    仓库里 data/*.json 是历史快照这一事实, 是"文件在但内容对不上"的根源,
    所以缺文件时的提示必须直指执行时机, 而不是笼统说"请 fetch"。
    """
    missing = [p for p in (OBOS, BMK) if not os.path.exists(p)]
    if missing:
        print("::error::缺少复核所需数据: %s" % ", ".join(missing))
        print("  本脚本必须紧跟 compute.py 之后执行(此时 data/ 才是本次最新结果)。")
        print("  仓库里的 data/*.json 是历史快照、CI 从不回写, 单独 checkout 后跑必然对不上。")
        return None, None
    try:
        with open(OBOS, encoding="utf-8") as f:
            ob = json.load(f)
        with open(BMK, encoding="utf-8") as f:
            bmk = json.load(f)
    except ValueError as e:
        print("::error::data/*.json 解析失败(文件可能被截断): %s" % e)
        return None, None
    return ob, bmk


def main():
    rows = load_rows()
    if not rows:
        print("prediction_log.jsonl 为空或不存在 —— 自 compute.py 建档起, 每次主模式计算会追加快照.")
        print("首个成熟样本需等预测日之后 %d 个交易日, 在此之前请用官方 walk-forward 口径评估:" % HORIZON)
        print("  (本地) python backtest_deep.py   或   复现 compute.py 阶段5 的 run_backtest 诊断.")
        return 0
    ob, bmk = load_data()
    if ob is None or bmk is None:
        return 1
    dates = bmk["dates"]
    dpos = {d: i for i, d in enumerate(dates)}
    n_dt = len(dates)
    # [2026-09-05] 长度契约断言 —— 此前完全没有, 是本脚本最隐蔽的风险:
    #   复核用"基准日历下标 t"去索引行业分数序列 sc[t+HORIZON], 这要求两者严格等长。
    #   一旦取数层某源回退导致行业比基准少一天, 全部 real 会整体错位且不报任何错,
    #   产出的还是一份"看起来很正常"的错误报告 —— 比报错危险得多。宁可判红中断。
    bad = [(x.get("code"), len(x.get("score") or [])) for x in ob["industries"]
           if len(x.get("score") or []) != n_dt]
    if bad:
        print("::error::%d/%d 个行业的分数序列长度与基准日历不一致(如 %s: %d vs 日历 %d), "
              "拒绝按错位下标出报告" % (len(bad), len(ob["industries"]), bad[0][0], bad[0][1], n_dt))
        return 1
    score_by_code = {x["code"]: x["score"] for x in ob["industries"]}
    asofs = sorted({r["asof"] for r in rows if r.get("asof")})
    print("预测日志: %d 行业天 @ %d 个 asof (%s → %s) | 数据截至 %s"
          % (len(rows), len(asofs), asofs[0], asofs[-1], ob["asof"]))
    # 成熟样本 = asof 对应日期存在且其后第 HORIZON 个交易日的实际分数已可观测
    mature = []
    n_no_asof = n_no_future = n_none = 0
    for r in rows:
        code, asof = r.get("code"), r.get("asof")
        sc = score_by_code.get(code)
        if sc is None:
            continue
        if asof not in dpos:
            n_no_asof += 1
            continue
        t = dpos[asof]
        if t + HORIZON >= len(sc):
            n_no_future += 1
            continue
        real = sc[t + HORIZON]
        if real is None:
            n_none += 1
            continue
        mature.append((r, real))
    if not mature:
        # [2026-09-04 首修 / 2026-09-05 再修] 原实现在 asof 不在本地 dates 时兜底输出 "?",
        #   而日志(CI 每日)几乎总比本地快照新, 该分支必命中, 导致永远给不出成熟日期。
        #   第二次修正补上两点: ① 因果方向别写反(日志比数据"新", 不是"早于");
        #   ② 拆开三种未成熟原因分别计数, 才能分辨"还没到时候"与"数据对不上"。
        known = [a for a in asofs if a in dpos]
        print("\n尚无成熟样本(%d 行预测, 需其后第 %d 个交易日的实际值)" % (len(rows), HORIZON))
        if not known:
            print("  预测日志覆盖 %s ~ %s, 本地数据日历末端仅到 %s —— 日志比本地数据新。"
                  % (asofs[0], asofs[-1], dates[-1] if dates else "空"))
            print("  这通常不是取数问题, 而是执行时机: 仓库里 data/*.json 是历史快照(CI 从不回写),")
            print("  必须与 compute.py 紧邻执行才能拿到当日数据(见 daily.yml 的 Audit 步骤)。")
        else:
            t0 = dpos[known[0]]
            gap = HORIZON - (n_dt - 1 - t0)
            print("  最早可定位的预测日 %s, 其后第 %d 个交易日尚未走完 —— 还差 %d 个交易日。"
                  % (known[0], HORIZON, gap))
        print("  未成熟明细: 预测日不在本地日历 %d 行 / 未来不足 %d 个交易日 %d 行 / 实际值为空 %d 行"
              % (n_no_asof, HORIZON, n_no_future, n_none))
        print("  请等待积累, 或用官方 walk-forward 口径(backtest_deep.py / run_backtest)先行评估。")
        return 0
    dir_ok = dir_n = 0                  # 全样本口径(不过滤变动幅度)
    f_ok = f_n = 0                      # 过滤口径(|实际变动|>=1, 与旧版一致, 便于对照)
    brier = 0.0
    brier_n = 0
    cov_hit = cov_n = 0
    # [2026-09-05] 分区间此前累加了 Brier 却从不输出(死代码), 且没有自己的分母 ——
    #   若按方向样本数 n 去除, 分母口径与累加口径不同(方向要 |变动|>=1, Brier 不要),
    #   算出来是错的。故分区间改为分别计数: [dir_ok, dir_n, brier_sum, brier_n]。
    by_regime = {}
    for r, real in mature:
        s = r.get("s")
        if s is None or real is None:
            continue
        med, lo, hi = r.get("med"), r.get("lo"), r.get("hi")
        pup = r.get("p_up")
        # p_up 语义 = P(终点 > 起点), 与 obos_core 回测里的标签定义完全一致
        if pup is not None:
            y = 1 if real > s else 0
            brier += (pup - y) ** 2
            brier_n += 1
        if lo is not None and hi is not None:
            cov_n += 1
            cov_hit += 1 if (lo <= real <= hi) else 0
        # 方向只统计"预测确实给出了方向"的样本(med 与起点相同 = 预测持平, 无法判定)
        directed = med is not None and abs(med - s) > 1e-9
        if directed:
            hit = 1 if (med - s) * (real - s) > 0 else 0
            dir_n += 1
            dir_ok += hit
            if abs(real - s) >= 1.0:
                f_n += 1
                f_ok += hit
        g = "cold" if s < 40 else ("hot" if s > 60 else "mid")
        st = by_regime.setdefault(g, [0, 0, 0.0, 0])
        if directed:
            st[1] += 1
            st[0] += 1 if (med - s) * (real - s) > 0 else 0
        if pup is not None:
            st[2] += (pup - (1 if real > s else 0)) ** 2
            st[3] += 1
    print("\n===== 预测快照复核(成熟 %d 行, 期限 %d 交易日) =====" % (len(mature), HORIZON))
    if dir_n:
        print("方向命中(%dd): %.1f%% (%d/%d)  [全样本口径]"
              % (HORIZON, 100.0 * dir_ok / dir_n, dir_ok, dir_n))
    if f_n:
        # 过滤口径以"实际变动幅度"为条件挑选样本 —— 条件于结果, 存在选择性偏差,
        # 不能当作真实命中率; 保留它只为与历史数字对照, 故一并说明。
        print("方向命中(%dd): %.1f%% (%d/%d)  [|实际变动|>=1 过滤口径, 条件于结果, 有偏, 仅供对照]"
              % (HORIZON, 100.0 * f_ok / f_n, f_ok, f_n))
    if brier_n:
        print("p_up Brier  : %.4f (n=%d)  [越小越好; 0.25 = 瞎猜]" % (brier / brier_n, brier_n))
    if cov_n:
        print("区间覆盖    : %.1f%% (%d/%d)  [目标 50%%]" % (100.0 * cov_hit / cov_n, cov_hit, cov_n))
    print("\n分区间(按预测起点分数):")
    for g in ("cold", "mid", "hot"):
        if g not in by_regime:
            continue
        ok, n, brs, bn = by_regime[g]
        if n == 0 and bn == 0:
            continue
        msg = "  %-4s: " % g
        if n:
            msg += "方向 %.1f%% (%d/%d)" % (100.0 * ok / n, ok, n)
        if bn:
            msg += (" | " if n else "") + "Brier %.4f (n=%d)" % (brs / bn, bn)
        print(msg)
    return 0


if __name__ == "__main__":
    sys.exit(main())
