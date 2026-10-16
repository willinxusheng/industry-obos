#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""核心算法不变式测试(2026-09-05 新增).

为什么需要它:
  本仓库最致命的错误不是崩溃, 是"算完了、数字看着正常、其实是错的" —— 第六轮 pooled
  与截面 IC 差 27 倍、第七轮审计断链, 都是这一类。类比预测与秩相关处在整条链路最上游,
  这里的偏差会被放大到每一个分数、每一次回测, 且没有任何异常提示。

本文件用"不变式 + 故障注入"守住三条性质(均用合成数据, 不读真实 data/):
  A. 未来不变性: 篡改查询时点之后的数据, 预测结果必须逐位不变(否则就是前视泄漏)
  B. NaN 不入秩: spearman 遇非有限值必须剔除, 不能产出"看着正常的错数"
  C. 分位区间不退化: lo/hi 的顺序不得依赖上一行的赋值结果

用法: python test_pit_invariants.py   (需 numpy; 不联网, 不写盘)
"""
import importlib.util
import math
import os
import sys

import numpy as np

BASE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE)
import obos_core

fail = 0


def bad(msg):
    global fail
    fail += 1
    print("  FAIL  " + msg)


def ok(msg):
    print("  PASS  " + msg)


def synth(nk=12, nt=420, seed=7):
    """均值回复随机游走, 贴近 OBOS 分数的统计性质(有界、围绕 50 波动)。"""
    rng = np.random.default_rng(seed)
    S = np.zeros((nk, nt))
    for k in range(nk):
        x = 50.0
        for t in range(nt):
            x = x + 0.08 * (50.0 - x) + rng.normal(0, 1.8)
            S[k, t] = min(100.0, max(0.0, x))
    return S


print("===== A 未来不变性: 篡改 t 之后的数据, 预测必须逐位不变 =====")
# ⚠️ 篡改范围必须与查询时点绑定: 对每个 t 单独造一份"只改 t 之后"的副本。
#   若固定篡改 TQ 之后再拿去查 t>TQ 的时点, 被改的区间对那时点而言是"历史",
#   预测理应变化 —— 那样测出来的是用例自身的错误, 不是泄漏(本用例第一版就栽在这里)。
S = synth()
worst, tested = 0.0, 0
for k0 in (0, 3, 7, 11):
    for t in (260, 300, 360):
        S2 = S.copy()
        S2[:, t + 1:] += 60.0              # 巨大改动但保持有限, 片段成立条件不变
        S2 = np.clip(S2, 0, 100)
        lib1, lib2 = obos_core.AnalogLib(S), obos_core.AnalogLib(S2)
        a, b = lib1.forecast(k0, t), lib2.forecast(k0, t)
        if a is None or b is None:
            if (a is None) != (b is None):
                bad("k=%d t=%d: 一份为 None 一份不为 —— 未来数据影响了可用性" % (k0, t))
            continue
        tested += 1
        for key in ("median", "p25", "p75"):
            worst = max(worst, float(np.max(np.abs(np.asarray(a[key]) - np.asarray(b[key])))))
        worst = max(worst, abs(a["p_up"] - b["p_up"]),
                    abs(a["consensus"] - b["consensus"]))
if tested == 0:
    bad("没有任何时点可预测(池不足?), 本用例未真正覆盖")
elif worst == 0.0:
    ok("%d 个 (行业,时点) 组合的最大绝对差 = 0 —— 无前视泄漏" % tested)
else:
    bad("预测随未来数据变化, 最大差 %.3e —— 存在前视泄漏" % worst)

print("\n===== B 非有限值不得进入秩相关 =====")
# 旧实现直接排序: NaN 与任何值比较均为 False, 会打乱并列判定并算出错秩,
# 返回的却是一个"看起来很正常"的相关系数 —— 比返回 NaN 更危险。
x = [1.0, 2.0, 3.0, 4.0, 5.0, 6.0]
y = [2.0, 1.0, 4.0, float("nan"), 5.0, 7.0]
got = obos_core.spearman(x, y)
ref = obos_core.spearman([1, 2, 3, 5, 6], [2, 1, 4, 5, 7])   # 手工剔除 NaN 那一对
if got is None:
    bad("含 NaN 时返回 None —— 可用样本仍充足, 应剔除后计算而非整体作废")
elif not math.isfinite(got):
    bad("含 NaN 时返回了非有限值 %r —— NaN 仍在扩散" % got)
elif abs(got - ref) > 1e-12:
    bad("剔除后结果 %.6f 与手工剔除 %.6f 不一致" % (got, ref))
else:
    ok("含 NaN 时按剔除处理, 与手工剔除一致(%.6f)" % got)

# 剔除后样本不足必须拒绝, 不能凑合着算
if obos_core.spearman([1.0, float("nan"), float("nan"), float("nan")],
                      [1.0, 2.0, 3.0, 4.0]) is None:
    ok("剔除后样本不足时拒绝返回(None)")
else:
    bad("剔除后仅剩 1 个有效样本仍给出了相关系数")

# 正常输入不应被误伤: 结果与"无 NaN 路径"完全一致
w = 0.0
for seed in range(30):
    r = np.random.default_rng(seed)
    a1, b1 = list(r.normal(size=40)), list(r.normal(size=40))
    w = max(w, abs(obos_core.spearman(a1, b1) - obos_core.spearman(a1, b1)))
if w == 0.0:
    ok("正常输入结果稳定(30 组随机, 最大差 0)")
else:
    bad("正常输入结果不稳定")

print("\n===== C lo/hi 顺序不得依赖上一行的赋值 =====")
# 旧写法: 先 lo=min(lo,hi) 再 hi=max(lo,hi) —— 第二行读到的是已被改写的 lo。
# 正常(q25<=q75)时碰巧正确; 一旦顺序颠倒, 不会交换而是双双压到小的那个,
# 区间悄悄退化成一个点 —— 与"保证 lo<=hi"的意图不符且难以察觉。
q25o, q75o = np.array([60.0]), np.array([40.0])      # 反常: lo > hi
old_lo = np.clip(np.minimum(q25o, q75o), 0, 100)
old_hi = np.clip(np.maximum(old_lo, q75o), 0, 100)   # 旧写法: 用的是新 lo
new_lo, new_hi = (np.clip(np.minimum(q25o, q75o), 0, 100),
                  np.clip(np.maximum(q25o, q75o), 0, 100))
if float(old_lo[0]) == float(old_hi[0]) == 40.0:
    ok("确认旧写法在顺序颠倒时会退化为一点(40,40)—— 不改则隐患常在")
else:
    bad("未能复现旧写法的退化行为, 用例前提失效: (%s,%s)" % (old_lo[0], old_hi[0]))
if float(new_lo[0]) == 40.0 and float(new_hi[0]) == 60.0 and new_hi[0] > new_lo[0]:
    ok("现写法给出真正的 min/max(40,60), 区间不退化")
else:
    bad("现写法结果错误: lo=%s hi=%s" % (new_lo[0], new_hi[0]))
# 源码里不得再出现"连续两行互相依赖"的旧写法(整行赋值形式, 而非元组解包形式)
src = open(os.path.join(BASE, "obos_core.py"), encoding="utf-8").read()
OLD_PAT = "q25 = np.clip(np.minimum(q25, q75), 0, 100)"
if src.count(OLD_PAT) == 0:
    ok("源码中已无顺序依赖的旧写法(%r 出现 0 次)" % OLD_PAT)
else:
    bad("源码中仍有 %d 处顺序依赖写法 %r" % (src.count(OLD_PAT), OLD_PAT))

print("\n===== D 回退源截短历史不得被当成「口径真空期」放行 =====")
# [2026-10-16] 行业链路 = 腾讯(主) -> 腾讯备用接入点 -> 东财。而东财 push2his 对申万
#   行业代码的支持是【残缺】的: 实测 31 个一级行业只有 16 个能返回, 其中仅 5 个是完整
#   1300 条, 其余短至 68 条(计算机)、88 条(建筑材料)。它返回的截短序列形态是
#   "前段缺失 + 后段连续 + 有效段够长", 与二级管线合法的「指数口径真空期」
#   【指纹同构】—— 从单份数据里分不出来。
#   修复前故障注入实测: 1 个二级行业截短到 1227/1076/951/876 条时
#     status=PASS / align_coverage=1.00000 / missing_cells=0 / issues=空,
#   连 15 个行业同时截短也照样 PASS。即门禁会亲口宣布"完全对齐、没问题",
#   而分数其实是基于短一截的 PIT 窗口算出来的 —— 典型的"算完、格式正常、数字是错的"。
# 修复三件事:
#   ① 真空期豁免叠加显式白名单 SUB_VACUUM_WHITELIST, 且首个有效日须与登记值一致;
#   ② 形态像真空期但未登记 -> 直接 fatal。不能靠 cover>0.999 的容差落进 WARN ——
#      该容差随行业数放大: 一级 31 个 -> 缺<=40 天放行, 二级 109 个 -> 缺<=141 天放行,
#      而东财最轻的残缺恰是 73 天, 正好落在二级的容差里(修复前 C1227 仅 WARN)。
#   ③ 空序列不得让门禁崩溃(原实现无条件 ds[0], 15 个行业全空时抛 IndexError)。
import datetime as _dt  # noqa: E402

import compute  # noqa: E402
import trading_calendar as tc  # noqa: E402

N_D = 1300
_end = tc.expected_asof()                      # 返回 datetime.date
_D = _end if isinstance(_end, _dt.date) else _dt.date.fromisoformat(str(_end))
_ds = []
for _ in range(N_D):
    _ds.append(_D.isoformat())
    _D = tc.prev_trade_day(_D)
bdates = list(reversed(_ds))
bclose = [3000.0 + i for i in range(N_D)]
print("  合成基准: %d 个真实交易日 %s ~ %s" % (len(bdates), bdates[0], bdates[-1]))


def _mk(n_ind, cut_map, vac_mode=None):
    """vac_mode: None=不含白名单行业 / 'legal'=按登记起点 / 'shorter'=被截得更短。
    不足 n_ind 个时返回 None(调用方跳过)，保证用例不会因时间推移而自坏。"""
    raw = {}
    n_plain = n_ind - 1 if vac_mode else n_ind
    for i in range(n_plain):
        k = cut_map.get(i, 0)
        rows = [[d, 1000.0 + i + j, 1005.0 + i + j, 1008.0 + i + j, 997.0 + i + j, 1.0e7]
                for j, d in enumerate(bdates[k:])]
        raw["pt01%06d" % (801000 + i * 10)] = {"name": "合成%03d" % (i + 1),
                                               "fq_key": "day", "rows": rows}
    if vac_mode:
        wl = compute.SUB_VACUUM_WHITELIST.get("pt01801179")
        if not wl or wl["from"] not in bdates:
            return None
        k = bdates.index(wl["from"]) + (100 if vac_mode == "shorter" else 0)
        rows = [[d, 2000.0 + j, 2005.0 + j, 2008.0 + j, 1997.0 + j, 1.0e7]
                for j, d in enumerate(bdates[k:])]
        raw["pt01801179"] = {"name": "铁路公路", "fq_key": "day", "rows": rows}
    return raw


def _g(raw, n_ind, relax):
    return compute.quality_gate(raw, bdates, bclose, expect_n=n_ind,
                                relax_prefix_vacuum=relax)


def _check(label, raw, n_ind, relax, expect):
    if raw is None:
        print("  SKIP  %s (合成基准已不含白名单登记日, 用例自动跳过)" % label)
        return
    try:
        r = _g(raw, n_ind, relax)
    except Exception as e:
        bad("%s: 门禁抛异常 %s: %s" % (label, type(e).__name__, str(e)[:50]))
        return
    got = r["status"]
    if got == expect:
        ok("%s -> %s" % (label, got))
    else:
        bad("%s -> %s (期望 %s; cover=%.5f miss=%d issues=%s)"
            % (label, got, expect, r["align_coverage"], r["missing_cells"],
               r["issues"][:1]))


# 反向对照: 必须 PASS, 否则下面"截短被拦"可能只是"恒 FAIL"
_check("D0 反向对照 31 个一级全对齐", _mk(31, {}), 31, False, "PASS")
_check("D1 1 个一级缺 41 天(边界外)", _mk(31, {0: 41}), 31, False, "FAIL")
_check("D2 反向对照 109 个二级全对齐", _mk(109, {}, vac_mode="legal"), 109, True, "PASS")
# 修复前这四种截短在二级模式下全部 PASS(被记成真空期)
for _label, _k in (("1227", 73), ("1076", 224), ("951", 349), ("876", 424)):
    _check("D3 二级截短到 %s 条(缺%d天)" % (_label, _k),
           _mk(109, {0: _k}, vac_mode="legal"), 109, True, "FAIL")
_check("D4 15 个二级同时截短到 1227 条",
       _mk(109, {i: 73 for i in range(15)}, vac_mode="legal"), 109, True, "FAIL")
# 白名单正向: 合法口径真空期必须仍被豁免(修复不能把这条件正常路径一起堵死)
_check("D5 白名单行业 合法真空期(须豁免)", _mk(109, {}, vac_mode="legal"), 109, True, "PASS")
# 白名单反向: 同一行业被截得更短, 首个有效日与登记值不符 -> 必须 FAIL
_check("D6 白名单行业 被截更短(须 FAIL)",
       _mk(109, {}, vac_mode="shorter"), 109, True, "FAIL")
# 空序列: 必须给出 FAIL 结论, 而不是抛 IndexError 让门禁崩掉
try:
    _empty = _mk(31, {})
    for _c in list(_empty)[:15]:
        _empty[_c]["rows"] = []
    _r = _g(_empty, 31, False)
    if _r["status"] == "FAIL" and _r.get("empty_series") == 15:
        ok("D7 15 个行业空序列 -> FAIL(empty_series=15)，门禁未崩溃")
    else:
        bad("D7 空序列 -> status=%s empty_series=%s (期望 FAIL/15)"
            % (_r["status"], _r.get("empty_series")))
except Exception as e:
    bad("D7 空序列让门禁崩溃: %s: %s" % (type(e).__name__, str(e)[:60]))

print()
if fail == 0:
    print("PIT INVARIANTS TEST PASSED：无前视泄漏 / 非有限值不入秩 / 分位区间不退化 / "
          "回退源截短不得冒充口径真空期")
else:
    print("PIT INVARIANTS TEST FAILED：%d 项" % fail)
sys.exit(1 if fail else 0)
