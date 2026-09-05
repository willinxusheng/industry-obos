# -*- coding: utf-8 -*-
"""取数据源健康自检 (2026-09-05 建立).

背景
----
代码里写着"三源回退", 但回退源在主源正常时【永不执行】—— 这是最容易腐烂的路径。
2026-09-05 实测发现所谓的三源冗余并不真实:
  · 新浪 K线接口【不支持申万行业代码】: sw801780 返回 HTTP 200 + JSON null,
    试过 sw/sh/sz/b_/hb 等 8 种 symbol 格式全灭; 而对个股 sh600000、指数 sh000300
    正常返回。属接口能力缺失, 与运行环境无关、全球一致 —— 行业链路已移除该源。
  · 东财 push2his.eastmoney.com 在本机网络不可达(4 次重试 + curl 直连均失败),
    但同域的 push2(实时行情) 正常。CI(海外)是否可达未知, 需本脚本在对应环境验证。

更要命的是这类失效是【静默】的: 新浪返回 HTTP 200 + null 而不是报错, 若不实测,
看板上永远显示"取数成功", 谁也不会知道冗余已经没了。

用途
----
把"冗余是否真实存在"变成可验证的事实, 而不是靠 SOURCES 列表里的假设。
每个源只发 1 次请求, 成本可忽略。

用法
----
  python check_sources.py          # 探测全部源并输出报告
  python check_sources.py --quiet  # 只输出结论行(适合放进 CI 日志)

退出码
------
  0 = 行业链路 >= 2 个可用源(存在真实冗余)
  1 = 行业链路只剩 1 个可用源(单点依赖, 需要处理)
  2 = 行业链路 0 个可用源(取数已彻底中断)
"""
import io
import os
import sys
import time

BASE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE)

import fetch_benchmark as B          # noqa: E402
import fetch_data as F               # noqa: E402

# 行业链路在用的源 + 已移除的新浪(作为对照一起探测, 便于日后发现它恢复支持)
IND_PROBE = [("tencent", lambda: F.parse_tencent("801780")),
             ("tencent_bak", lambda: F.parse_tencent_bak("801780")),
             ("eastmoney", lambda: F.parse_eastmoney("801780")),
             ("sina(已移除)", lambda: F.parse_sina("801780"))]
IND_IN_USE = ("tencent", "tencent_bak", "eastmoney")

BMK_PROBE = [("tencent", B.parse_tencent),
             ("tencent_bak", B.parse_tencent_bak),
             ("eastmoney", B.parse_eastmoney),
             ("sina", B.parse_sina)]


def probe(fn):
    """返回 (ok, rows_or_None, err)"""
    t0 = time.time()
    try:
        rows, fq = fn()
        return (True, rows, fq, time.time() - t0, None)
    except Exception as e:
        return (False, None, None, time.time() - t0, "%s: %s" % (type(e).__name__, str(e)[:70]))


def field_check(rows):
    """字段顺序自检: open/close 必须落在 [low, high] 内, 否则是顺序错配。"""
    bad = 0
    for r in (rows or [])[-200:]:
        _d, o, c, h, l, _v = r
        if not (l - 1e-9 <= o <= h + 1e-9 and l - 1e-9 <= c <= h + 1e-9):
            bad += 1
    return bad


def report(title, probe_list, in_use, sample_len, out):
    out.write("\n===== %s =====\n" % title)
    ok_map = {}
    for name, fn in probe_list:
        ok, rows, fq, dt, err = probe(fn)
        if ok and len(rows) >= 1000:
            bad = field_check(rows)
            ok_map[name] = rows
            out.write("  %-16s ✅ rows=%-5d fq=%-5s 末日=%s 末收=%.4f (%.1fs) %s\n"
                      % (name, len(rows), fq, rows[-1][0], rows[-1][2], dt,
                         "字段顺序 ✅" if bad == 0 else "字段顺序 ❌ 越界%d" % bad))
        elif ok:
            out.write("  %-16s ❌ 仅 %d 行(不足1000) —— 接口存活但不返回有效数据\n"
                      % (name, len(rows)))
        else:
            out.write("  %-16s ❌ %s (%.1fs)\n" % (name, err, dt))

    avail = [n for n in in_use if n in ok_map]
    # 一致性对比
    if len(ok_map) >= 2:
        out.write("  --- 收盘价逐日一致性(以第一个可用源为基准) ---\n")
        ref_name = avail[0] if avail else list(ok_map)[0]
        ref = {r[0]: r[2] for r in ok_map[ref_name]}
        for name, rows in ok_map.items():
            if name == ref_name:
                continue
            m = {r[0]: r[2] for r in rows}
            common = set(ref) & set(m)
            if not common:
                out.write("    %-16s 与 %s 无共同日期\n" % (name, ref_name))
                continue
            diffs = [abs(ref[d] - m[d]) for d in common]
            out.write("    %-16s 共同日=%-5d 最大绝对差=%.6f  %s\n"
                      % (name, len(common), max(diffs),
                         "✅ 一致" if max(diffs) < 1e-6 else "⚠️ 有差异"))
    out.write("  => 在用源可用数: %d / %d  %s\n"
              % (len(avail), len(in_use),
                 "✅ 存在真实冗余" if len(avail) >= 2 else
                 ("❌ 单点依赖(无冗余)" if len(avail) == 1 else "❌ 全部不可用")))
    return len(avail)


def main():
    quiet = "--quiet" in sys.argv
    buf = io.StringIO()
    buf.write("取数据源健康自检  %s\n" % time.strftime("%Y-%m-%d %H:%M:%S"))
    buf.write("探测对象: 申万一级行业 801780(银行) / 沪深300 sh000300；每源 1 次请求\n")

    n_ind = report("行业链路(申万 sw 代码)", IND_PROBE, IND_IN_USE, 1300, buf)
    n_bmk = report("基准链路(沪深300 sh000300)", BMK_PROBE,
                   [n for n, _ in BMK_PROBE], 1300, buf)

    verdict = ("行业可用源 %d 个 → %s"
               % (n_ind, "真实冗余" if n_ind >= 2 else
                  ("单点依赖" if n_ind == 1 else "全部中断")))
    buf.write("\n【结论】%s；基准可用源 %d 个\n" % (verdict, n_bmk))
    buf.write("提示: 东财 push2his 的可达性与网络环境相关，本机结论不可直接代表 CI(海外)。\n")

    sys.stdout.write(buf.getvalue())
    return 0 if n_ind >= 2 else (1 if n_ind == 1 else 2)


if __name__ == "__main__":
    sys.exit(main())
