# -*- coding: utf-8 -*-
"""取数据源健康自检 (2026-09-05 建立).

背景
----
代码里写着"三源回退", 但回退源在主源正常时【永不执行】—— 这是最容易腐烂的路径。
2026-09-05 实测发现所谓的三源冗余并不真实:
  · 新浪 K线接口【不支持申万行业代码】: sw801780 返回 HTTP 200 + JSON null,
    试过 sw/sh/sz/b_/hb 等 8 种 symbol 格式全灭; 而对个股 sh600000、指数 sh000300
    正常返回。属接口能力缺失, 与运行环境无关、全球一致 —— 行业链路已移除该源。
  · 东财 push2his.eastmoney.com 在 2026-09-05 曾于本机不可达(4 次重试 + curl 直连均失败),
    但同域的 push2(实时行情) 正常。
    [2026-10-16 实测] 存在【两个叠加的问题】, 而且当初的归因只抓到一半:
      ① 可达性本身波动 —— 同一台机器、相隔约 20 分钟两次探测: 一次基准/行业全通
         (基准 1300 条, 与腾讯最大绝对差 0.01), 一次全断(RemoteDisconnected 19.7s)。
         所以"不可达"与"可达"都能观测到, 谁都不能当稳定事实。
      ② 即使处在可达窗口内, 它对申万行业代码的支持也是【残缺】的 —— 31 个一级行业
         只有 16 个能返回、其中仅 5 个是完整 1300 条, 其余短至 68 条(计算机)、
         88 条(建筑材料)、1076 条(电子)。
    旧版本本脚本只探 801780(银行) —— 恰好是 EMPTY 的那 15 个之一, 于是天天输出
    "东财 ❌ 不可用", 与"网络不可达"互相印证, 把 ② 一藏就是 40 天。
    教训: 【单行业探针会给出错误的整体结论】, 故改为 6 个行业抽样 + 完整性报告。

更要命的是这类失效是【静默】的: 新浪返回 HTTP 200 + null 而不是报错, 若不实测,
看板上永远显示"取数成功", 谁也不会知道冗余已经没了。

用途
----
把"冗余是否真实存在"变成可验证的事实, 而不是靠 SOURCES 列表里的假设。
每个探测走各 fetch 脚本自己的 get_json(tries=5, 25s 超时)，死源单次约 19s，
全部 8 个探测最坏情况约几分钟——continue-on-error 下可接受，但别指望"秒回"。

冗余的口径（2026-09-30 修正）
----
tencent 与 tencent_bak 是同一份内容的两个接入域名（实测逐日最大差 0.0），
只能防域名级故障（限流/劫持），防不了腾讯侧的数据内容损坏或口径变更。
所以退出码按【独立口径源】计：tencent 系(主源+备用接入点)只算 1 个，
需要 tencent 系之外还有至少 1 个可用源(东财/新浪)才算"真实冗余"。

用法
----
  python check_sources.py          # 探测全部源并输出报告
  python check_sources.py --quiet  # 只输出结论行(适合放进 CI 日志)

退出码
------
  0 = 行业链路独立口径源 >= 2 个(存在真实冗余)
  1 = 行业链路只剩 1 个独立口径源(单点依赖, 需要处理)
  2 = 行业链路 0 个可用源(取数已彻底中断)
"""
import io
import os
import sys
import time
from collections import Counter

BASE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE)

import fetch_benchmark as B          # noqa: E402
import fetch_data as F               # noqa: E402
from fetch_common import bj_now      # noqa: E402

# [2026-10-16] 行业链路探针必须【多行业抽样】。
#   原实现只探 801780(银行) 一个行业 —— 而 801780 恰恰是东财返回 EMPTY 的那 15 个之一，
#   于是本脚本天天输出"东财 ❌ 不可用"，与 2026-09-05 注释里写的"本机网络不可达"互相
#   印证，真实原因(接口对申万代码支持残缺)被彻底掩盖。实测 31 个一级行业里东财只有
#   16 个能返回，其中仅 5 个是完整 1300 条，其余短至 68 条 —— 单行业探针无论抽中哪
#   一个都会给出错误的整体结论。故改为固定抽样，并同时报告"成功数"与"完整数"。
IND_SAMPLE = [("801780", "银行"), ("801950", "煤炭"), ("801010", "农林牧渔"),
              ("801030", "基础化工"), ("801150", "医药生物"), ("801750", "计算机")]

IND_PROBE = [("tencent", F.parse_tencent),
             ("tencent_bak", F.parse_tencent_bak),
             ("eastmoney", F.parse_eastmoney),
             ("sina(已移除)", F.parse_sina)]
IND_IN_USE = ("tencent", "tencent_bak", "eastmoney")

BMK_PROBE = [("tencent", B.parse_tencent),
             ("tencent_bak", B.parse_tencent_bak),
             ("eastmoney", B.parse_eastmoney),
             ("sina", B.parse_sina)]

# 独立口径分组: 同组 = 同一份内容的多个接入点, 冗余只防域名级故障。
# (2026-09-30 修正: 原实现把 tencent_bak 计为独立源, 在东财不可达的环境里
#  会输出"✅ 存在真实冗余"+退出码 0 —— 但两个"源"会一模一样地坏。)
SOURCE_GROUP = {"tencent": "tencent", "tencent_bak": "tencent",
                "eastmoney": "eastmoney", "sina": "sina",
                "sina(已移除)": "sina"}


def probe(fn):
    """返回 (ok, rows_or_None, fq, 耗时s, err_or_None) 五元组。"""
    t0 = time.time()
    try:
        rows, fq = fn()
        return (True, rows, fq, time.time() - t0, None)
    except Exception as e:
        return (False, None, None, time.time() - t0, "%s: %s" % (type(e).__name__, str(e)[:70]))


def probe1(fn, code):
    """同上, 但按行业代码调用(签名 (code) -> (rows, fq))。"""
    t0 = time.time()
    try:
        rows, fq = fn(code)
        return (True, rows, fq, time.time() - t0, None)
    except Exception as e:
        return (False, None, None, time.time() - t0, "%s: %s" % (type(e).__name__, str(e)[:70]))


def report_industry(probe_list, in_use, out):
    """行业链路的可用性判定: 只看"有返回值"不够, 还要看"是否每个抽样行业都拿全"。

    一个源只能覆盖部分行业时, 它在主源故障时接管会产出【混合口径】的数据集
    (部分行业来自完整历史、部分来自被截短的历史), 而且 compute.quality_gate 在
    二级模式下还可能把被截短的序列误认成合法的"指数口径真空期"而放行 ——
    这正是 2026-10-16 修掉的那个漏洞。故本函数把"完整性"和"可用性"分开报告,
    且只有【全部抽样行业都拿全】的源才算真实冗余。
    """
    out.write("\n===== 行业链路(申万 sw 代码) =====\n")
    out.write("  抽样 %d 个行业: %s\n"
              % (len(IND_SAMPLE), "、".join("%s(%s)" % (n, s) for s, n in IND_SAMPLE)))
    stat = {}
    for name, fn in probe_list:
        lens, errs = [], []
        for sw, cname in IND_SAMPLE:
            _ok, rows, _fq, _dt, err = probe1(fn, sw)
            if rows and len(rows) >= 1000:
                lens.append(len(rows))
            else:
                errs.append("%s%s" % (cname, "(空)" if not err else "(ERR)"))
        stat[name] = lens
        if lens:
            full = sum(1 for x in lens if x == max(lens))
            perfect = (len(lens) == len(IND_SAMPLE) and full == len(lens))
            out.write("  %-16s 成功 %d/%d  条数 %d~%d  完整 %d/%d  %s\n"
                      % (name, len(lens), len(IND_SAMPLE), min(lens), max(lens),
                         full, len(lens),
                         "✅ 全抽样完整" if perfect else "⚠️ 支持残缺, 不能当冗余"))
            if errs:
                out.write("  %-16s   未取到: %s\n" % ("", "、".join(errs)))
        else:
            out.write("  %-16s 成功 0/%d  ❌ %s\n"
                      % (name, len(IND_SAMPLE), "、".join(errs[:4])))
    # 只有"全抽样完整"的源才算真实冗余
    rel = [n for n in in_use
           if stat.get(n) and len(stat[n]) == len(IND_SAMPLE)
           and all(x == max(stat[n]) for x in stat[n])]
    n_group = len({SOURCE_GROUP.get(n, n) for n in rel})
    out.write("  => 可靠冗余源: %s (独立口径 %d 个)  %s\n"
              % ("、".join(rel) if rel else "无", n_group,
                 "✅ 存在真实冗余" if n_group >= 2 else
                 ("❌ 单点依赖(无独立冗余)" if n_group == 1 else "❌ 全部不可用")))
    return n_group


def field_check(rows):
    """字段顺序自检: open/close 必须落在 [low, high] 内, 否则是顺序错配。"""
    bad = 0
    for r in (rows or [])[-200:]:
        _d, o, c, h, l, _v = r
        if not (l - 1e-9 <= o <= h + 1e-9 and l - 1e-9 <= c <= h + 1e-9):
            bad += 1
    return bad


def report(title, probe_list, in_use, out):
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
        # [2026-09-30] 盘中拉取时, 各源对"今天"这根未落定 bar 的时戳不同, 绝对差是
        #   时戳差不是口径差 —— 比对前剔除未落定的当天 bar(北京时间 16:00 前拉取时)。
        now = bj_now()
        drop_today = now.strftime("%Y-%m-%d") if now.hour * 60 + now.minute < 960 else None
        ref_name = avail[0] if avail else list(ok_map)[0]
        ref = {r[0]: r[2] for r in ok_map[ref_name]}
        if drop_today:
            ref.pop(drop_today, None)
        for name, rows in ok_map.items():
            if name == ref_name:
                continue
            m = {r[0]: r[2] for r in rows}
            if drop_today:
                m.pop(drop_today, None)
            common = set(ref) & set(m)
            if not common:
                out.write("    %-16s 与 %s 无共同日期\n" % (name, ref_name))
                continue
            diffs = [abs(ref[d] - m[d]) for d in common]
            max_abs = max(diffs)
            # [2026-09-30] 相对容差 1e-3: 新浪对指数有 ~0.005 的四舍五入精度差,
            #   旧阈值 1e-6 让所有非镜像源永远 ⚠️ —— 天天告警等于没有告警。
            #   真正的口径分歧(复权/错码)差 1% 以上, 1e-3 足以区分。
            max_rel = max(abs(ref[d] - m[d]) / max(abs(ref[d]), 1e-9) for d in common)
            out.write("    %-16s 共同日=%-5d 最大绝对差=%.4f(相对%.1e)  %s\n"
                      % (name, len(common), max_abs, max_rel,
                         "✅ 一致" if max_rel < 1e-3 else "⚠️ 有差异"))
    n_avail = len(avail)
    n_group = len({SOURCE_GROUP.get(n, n) for n in avail})
    out.write("  => 在用源可用数: %d / %d (独立口径源 %d 个)  %s\n"
              % (n_avail, len(in_use), n_group,
                 "✅ 存在真实冗余" if n_group >= 2 else
                 ("❌ 单点依赖(无独立冗余)" if n_group == 1 else "❌ 全部不可用")))
    return n_group


def main():
    quiet = "--quiet" in sys.argv
    buf = io.StringIO()
    buf.write("取数据源健康自检  %s\n" % time.strftime("%Y-%m-%d %H:%M:%S"))
    buf.write("探测对象: 申万一级行业抽样 %d 个 / 沪深300 sh000300\n" % len(IND_SAMPLE))

    n_ind = report_industry(IND_PROBE, IND_IN_USE, buf)
    n_bmk = report("基准链路(沪深300 sh000300)", BMK_PROBE,
                   [n for n, _ in BMK_PROBE], buf)

    verdict = ("行业【可靠】独立口径源 %d 个 → %s"
               % (n_ind, "真实冗余" if n_ind >= 2 else
                  ("单点依赖" if n_ind == 1 else "全部中断")))
    buf.write("\n【结论】%s；基准独立口径源 %d 个\n" % (verdict, n_bmk))
    buf.write("说明: 行业源须【全部抽样行业都拿全】才算可靠冗余 —— 只覆盖部分行业的源\n")
    buf.write("      在主源故障时接管, 会产出完整/截短混杂的数据集。\n")
    buf.write("  · 东财 push2his 两个叠加问题: ① 可达性波动(同机相隔 20 分钟一次全通\n")
    buf.write("    一次全断); ② 即便可达, 对申万代码支持残缺 —— 31 个一级行业仅 16 个\n")
    buf.write("    能返回、其中 5 个完整, 最短 68 条。故不计入可靠冗余。\n")
    buf.write("  · 新浪对申万代码返回 JSON null(接口能力缺失, 与网络无关)。\n")
    buf.write("  => 行业链路实际仍是腾讯单点; 腾讯一旦故障没有等效替代。\n")

    sys.stdout.write(buf.getvalue())
    return 0 if n_ind >= 2 else (1 if n_ind == 1 else 2)


if __name__ == "__main__":
    sys.exit(main())
