#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""预测审计链路的受控反演测试(2026-09-05 新增).

为什么需要它:
  audit_predictions.py 的输出是给人做判断的结论(方向命中/Brier/区间覆盖)。这类代码最
  危险的失效方式不是崩, 而是"算完了、格式正常、数字是错的" —— 与 backtest_deep.py 里
  那处 pooled/截面 IC 差 27 倍的 bug 同一性质: 不报错, 但结论反向。而本脚本要到
  2026-10 中才有首批成熟样本, 届时再发现口径错, 前面攒的日志全白记。

方法(受控反演, 不读真实数据):
  自己造"答案已知"的合成数据 —— 分数序列、预测快照都由本脚本设定, 因此每个统计量的
  正确值是可推算的。跑完断言输出等于那个已知答案。若有人改坏了口径, 这里立刻红。

覆盖的失效模式:
  A 全对 -> 命中/Brier/覆盖应为 100%/0/100%
  B 全错 -> 应为 0%/1.0/0%
  C 分数序列长度与日历不一致 -> 必须拒绝出报告(返回非 0), 否则是错位下标下的假数
  D 预测日之后不足 30 个交易日 -> 属预期内的"未成熟", 应返回 0 而非报错
  E 预测持平(med==起点) -> 不计入方向样本, 但不该污染 Brier/覆盖
  F 全样本口径 vs |变动|>=1 过滤口径必须分离 —— 过滤口径条件于结果, 会掩盖小幅错误
  G compute.append_prediction_log 幂等不能依赖 JSON 键顺序(旧实现按行首前缀匹配)

用法: python test_audit_logic.py   (零第三方依赖, 不写真实 data/, 不联网)
"""
import contextlib
import datetime
import io
import json
import os
import sys
import tempfile

BASE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE)

import audit_predictions
import compute

N = 60
D0 = datetime.date(2026, 1, 1)
DATES = [(D0 + datetime.timedelta(days=i)).strftime("%Y-%m-%d") for i in range(N)]

fail = 0


def bad(msg):
    global fail
    fail += 1
    print("  FAIL  " + msg)


def ok(msg):
    print("  PASS  " + msg)


@contextlib.contextmanager
def synthetic(tmp, obos, rows):
    """把 audit_predictions 的三个数据路径临时指向合成文件(用完自动还原)。"""
    p_log = os.path.join(tmp, "prediction_log.jsonl")
    p_obos = os.path.join(tmp, "industry_obos.json")
    p_bmk = os.path.join(tmp, "benchmark.json")
    with io.open(p_bmk, "w", encoding="utf-8") as f:
        json.dump({"dates": DATES, "close": [1.0] * N}, f)
    with io.open(p_obos, "w", encoding="utf-8") as f:
        json.dump({"asof": DATES[-1], "industries": obos}, f)
    with io.open(p_log, "w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    old = (audit_predictions.LOG, audit_predictions.OBOS, audit_predictions.BMK)
    audit_predictions.LOG, audit_predictions.OBOS, audit_predictions.BMK = p_log, p_obos, p_bmk
    try:
        yield p_log
    finally:
        audit_predictions.LOG, audit_predictions.OBOS, audit_predictions.BMK = old


def run(obos, rows):
    """跑一次 audit main(), 返回 (退出码, 标准输出)。"""
    tmp = tempfile.mkdtemp(prefix="audit_t_")
    try:
        with synthetic(tmp, obos, rows):
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                rc = audit_predictions.main()
            return rc, buf.getvalue()
    finally:
        for fn in os.listdir(tmp):
            os.unlink(os.path.join(tmp, fn))
        os.rmdir(tmp)


def industry(code, score, s):
    return {"code": code, "name": code, "score": score, "cur_score": s}


def snap(asof, code, s, med, lo, hi, p_up):
    return {"asof": asof, "code": code, "name": code, "s": s,
            "med": med, "lo": lo, "hi": hi, "p_up": p_up}


T_ASOF = 10                       # 预测日下标 -> 复核点在 t+30=40
ASOF = DATES[T_ASOF]
REAL_UP = 60.0                    # 实际终点(起点 50, 上升 10)

print("===== A 全对: 命中/Brier/覆盖 应为 100% / 0.0 / 100% =====")
obos = [industry("c%d" % i, [50.0] * N, 50.0) for i in range(3)]
for x in obos:
    x["score"][T_ASOF + 30] = REAL_UP
rows = [snap(ASOF, x["code"], 50.0, med=60.0, lo=55.0, hi=65.0, p_up=1.0) for x in obos]
rc, out = run(obos, rows)
if rc != 0:
    bad("正常数据却返回 %s" % rc)
if "方向命中(30d): 100.0% (3/3)" in out and "[全样本口径]" in out:
    ok("方向命中 100%% (3/3)")
else:
    bad("方向命中不是 100%% (3/3):\n%s" % out)
if "p_up Brier  : 0.0000 (n=3)" in out:
    ok("Brier 0.0000 (n=3)")
else:
    bad("Brier 不是 0.0000 (n=3):\n%s" % out)
if "区间覆盖    : 100.0%" in out:
    ok("区间覆盖 100%")
else:
    bad("区间覆盖不是 100%:\n%s" % out)

print("\n===== B 全错: 命中/Brier/覆盖 应为 0% / 1.0 / 0% =====")
rows = [snap(ASOF, x["code"], 50.0, med=40.0, lo=30.0, hi=40.0, p_up=0.0) for x in obos]
rc, out = run(obos, rows)
if "方向命中(30d): 0.0% (0/3)" in out:
    ok("方向命中 0% (0/3)")
else:
    bad("方向命中不是 0%% (0/3):\n%s" % out)
if "p_up Brier  : 1.0000 (n=3)" in out:
    ok("Brier 1.0000 (n=3)")
else:
    bad("Brier 不是 1.0000 (n=3):\n%s" % out)
if "区间覆盖    : 0.0%" in out:
    ok("区间覆盖 0%")
else:
    bad("区间覆盖不是 0%:\n%s" % out)

print("\n===== C 长度契约破损: 必须拒绝出报告 =====")
broke = [industry("c0", [50.0] * N, 50.0), industry("c1", [50.0] * (N - 1), 50.0)]
rc, out = run(broke, [snap(ASOF, "c0", 50.0, 60.0, 55.0, 65.0, 1.0)])
if rc != 0:
    ok("长度不一致时判红(rc=%s)" % rc)
else:
    bad("长度不一致仍未判红 —— 会按错位下标产出假报告")
if "不一致" in out:
    ok("给出了不一致的诊断")
else:
    bad("未说明不一致:\n%s" % out)

print("\n===== D 未成熟(预测日距末端不足 30 交易日): 应返回 0 且说明 =====")
rc, out = run(obos, [snap(DATES[N - 5], "c0", 50.0, 60.0, 55.0, 65.0, 1.0)])
if rc == 0:
    ok("未成熟不算错误(rc=0)")
else:
    bad("未成熟却判红 —— 正常早期状态会让 CI 天天告警")
if "尚无成熟样本" in out:
    ok("说明了未成熟")
else:
    bad("未说明未成熟:\n%s" % out)

print("\n===== E 预测持平(med==起点): 不计方向, 但 Brier/覆盖照常 =====")
rows = [snap(ASOF, "c0", 50.0, med=50.0, lo=55.0, hi=65.0, p_up=1.0),   # 持平, 无方向
        snap(ASOF, "c1", 50.0, med=60.0, lo=55.0, hi=65.0, p_up=1.0)]   # 上升, 命中
rc, out = run(obos, rows)
if "方向命中(30d): 100.0% (1/1)" in out:
    ok("持平样本被排除在方向统计外(1/1)")
else:
    bad("方向样本数不对(应 1/1):\n%s" % out)
if "p_up Brier  : 0.0000 (n=2)" in out:
    ok("Brier 仍统计全部 2 行(n=2)")
else:
    bad("Brier 分母被方向过滤株连:\n%s" % out)

print("\n===== F 两种口径必须分离(过滤口径条件于结果, 会掩盖小幅错误) =====")
# c0 大幅上升且预测对; c1 小幅上升但预测错(实际 +0.5, 预测 -1)
obos_f = [industry("c0", [50.0] * N, 50.0), industry("c1", [50.0] * N, 50.0)]
obos_f[0]["score"][T_ASOF + 30] = 60.0     # +10
obos_f[1]["score"][T_ASOF + 30] = 50.5     # +0.5 (<1, 会被过滤口径剔除)
rows = [snap(ASOF, "c0", 50.0, med=60.0, lo=55.0, hi=65.0, p_up=1.0),
        snap(ASOF, "c1", 50.0, med=49.0, lo=55.0, hi=65.0, p_up=1.0)]
rc, out = run(obos_f, rows)
if "方向命中(30d): 50.0% (1/2)" in out:
    ok("全样本口径 50%% (1/2) —— 真实命中率")
else:
    bad("全样本口径不是 50%% (1/2):\n%s" % out)
if "100.0% (1/1)" in out and "过滤口径" in out and "有偏" in out:
    ok("过滤口径单独标注为有偏(100% 1/1), 不会冒充真实命中率")
else:
    bad("过滤口径未与全样本分离或未标注有偏:\n%s" % out)

print("\n===== G append_prediction_log 幂等不得依赖 JSON 键顺序 =====")
# 旧实现按行首前缀 '{"asof":"..."}' 匹配; 这里故意把 asof 放在第二个键,
# 模拟"将来给快照加了个字段且排在 asof 前面"这种最普通的改动。
tmp = tempfile.mkdtemp(prefix="audit_g_")
try:
    p = os.path.join(tmp, "prediction_log.jsonl")
    with io.open(p, "w", encoding="utf-8") as f:
        f.write(json.dumps({"code": "c0", "asof": "2026-01-05", "name": "x"},
                           ensure_ascii=False) + "\n")
    old = compute.PRED_LOG
    compute.PRED_LOG = p
    try:
        inds = [{"code": "c0", "name": "x", "cur_score": 50.0, "state": "中性",
                 "forecast": {"median": [1.0] * 30, "p25": [1.0] * 30,
                              "p75": [2.0] * 30, "p_up": 0.5}}]
        with contextlib.redirect_stdout(io.StringIO()):
            compute.append_prediction_log("2026-01-05", inds)
        n = sum(1 for _ in io.open(p, encoding="utf-8"))
        if n == 1:
            ok("键顺序变了仍能识别当日已记录(未重复追加)")
        else:
            bad("键顺序变了导致重复追加(行数 %d) —— 会污染 Brier 与命中率" % n)
        # 换个日期必须照常写入, 确认上面的"未追加"不是因为写入路径坏了
        with contextlib.redirect_stdout(io.StringIO()):
            compute.append_prediction_log("2026-01-06", inds)
        n2 = sum(1 for _ in io.open(p, encoding="utf-8"))
        if n2 == 2:
            ok("新日期照常追加(写入路径未坏)")
        else:
            bad("新日期未追加(行数 %d) —— 幂等改动误伤了正常写入" % n2)
    finally:
        compute.PRED_LOG = old
finally:
    for fn in os.listdir(tmp):
        os.unlink(os.path.join(tmp, fn))
    os.rmdir(tmp)

print()
if fail == 0:
    print("AUDIT LOGIC TEST PASSED：审计统计口径与日志幂等在受控反演下均正确")
else:
    print("AUDIT LOGIC TEST FAILED：%d 项" % fail)
sys.exit(1 if fail else 0)
