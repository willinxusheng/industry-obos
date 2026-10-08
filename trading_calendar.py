# -*- coding: utf-8 -*-
"""交易日历 —— 全项目唯一事实源（Python 计算层 与 CI 判据层共用一张表）

为什么单独成文件（而不是留在 obos_core 里）：
    CI 里有三处「应到最新完整交易日」判据 —— daily.yml 的 Freshness gate、
    daily.yml 的 auto-close、watchdog.yml 的看门狗。它们此前**各自**写了一份
    "纯周末算术"（周六=今天-1、周一凌晨=今天-3 …），**全部不含法定节假日**。

    2026 年国庆长假（10-01 ~ 10-07 休市）实测后果，三处同时爆雷：
      · watchdog：把 10-01 当成交易日 -> 判定"真漏更" -> 开 issue #5，
        且每 30 分钟复检都再次触发 daily.yml 补跑（issue 上刷了 28 条评论）；
      · Freshness gate：同样误判 -> 长假 7 天每天强制全量重建（每天一条
        daily refresh 提交），并发重建还撞出一次 rebase 冲突失败；
      · auto-close：判据同源 -> 明明数据完全健康（休市无新数据）也关不掉告警，
        红标从 10-01 挂到 10-08，用户看到的就是"线上有更新失败的提示"。

    根因不是某处写错，而是**同一口径被复制了三份**。故抽出本模块作为唯一事实源：
    假日表只有这一份，判据只有一个函数，三处 CI 统一调用（见各 workflow 的
    "计算应到交易日" 步骤）。obos_core 反向 import 本模块，保证计算层与判据层
    用的是同一张表 —— 新增 HOLIDAYS_20XX 后三处自动同时生效，不存在同步遗漏。

CLI（供 CI 的 shell / github-script 消费）：
    python3 trading_calendar.py                      # 此刻(北京时间)的应到交易日
    python3 trading_calendar.py --at 2026-10-01T23:19  # 指定北京时间（受控反演用）
    python3 trading_calendar.py --json               # 附判定细节的 JSON
    python3 trading_calendar.py --selftest           # 内置受控断言（0=通过）
"""
import datetime
import json
import sys

# ==================== 交易日历 ([C1]) ====================
# 国务院办公厅《关于2026年部分节假日安排的通知》; A股调休周末不开市
HOLIDAYS_2026 = [
    "2026-01-01", "2026-01-02", "2026-01-03",
    "2026-02-15", "2026-02-16", "2026-02-17", "2026-02-18", "2026-02-19",
    "2026-02-20", "2026-02-21", "2026-02-22", "2026-02-23",
    "2026-04-04", "2026-04-05", "2026-04-06",
    "2026-05-01", "2026-05-02", "2026-05-03", "2026-05-04", "2026-05-05",
    "2026-06-19", "2026-06-20", "2026-06-21",
    "2026-09-25", "2026-09-26", "2026-09-27",
    "2026-10-01", "2026-10-02", "2026-10-03", "2026-10-04",
    "2026-10-05", "2026-10-06", "2026-10-07",
]
# 2027 安排未发布, 仅纳入法定固定段(元旦/劳动节/国庆), 覆盖度在 quality 中披露
HOLIDAYS_2027 = ["2027-01-01",
                 "2027-05-01", "2027-05-02", "2027-05-03", "2027-05-04", "2027-05-05",
                 "2027-10-01", "2027-10-02", "2027-10-03", "2027-10-04",
                 "2027-10-05", "2027-10-06", "2027-10-07"]
HOLIDAYS = set(HOLIDAYS_2026) | set(HOLIDAYS_2027)
CAL_FULL_UNTIL = "2026-12-31"


def _cal_cover_until():
    """日历「实际覆盖到」的年末，由 HOLIDAYS 推导。

    刻意不另行硬编码：否则新增 HOLIDAYS_20XX 后若忘记同步这里，两个常量会漂移，
    预警就会在错误的时间点触发（或永不触发）。
    超出该年份后 future_trade_dates 只会排除周末，真实节假日会被误当作交易日。
    """
    if not HOLIDAYS:
        return ""
    return "%d-12-31" % max(int(h[:4]) for h in HOLIDAYS)


CAL_COVER_UNTIL = _cal_cover_until()

# 北京时间：CI runner 是 UTC，一切"今天"都必须按 UTC+8 取，否则北京 00:00-08:00 的
# 运行会把"昨天/今天"算错一天（历史上 compute.py 的 lag 判定踩过同一个坑）。
BJ_TZ = datetime.timezone(datetime.timedelta(hours=8))

# 当日收盘数据视为「已落定」的北京时间分钟数（16:00）。
# 与 daily.yml 的盘中封锁窗口上界、fetch_common.drop_unsettled_tail 同一口径：
# 16:00 之前拉到的当日 bar 一律不可信，故"应到交易日"也不得推进到今天。
SETTLE_MIN = 16 * 60


def _as_date(d):
    if isinstance(d, datetime.datetime):
        return d.date()
    if isinstance(d, datetime.date):
        return d
    return datetime.date.fromisoformat(str(d))


def is_trade_day(d):
    """d: date / datetime / 'YYYY-MM-DD'。周末与法定节假日均非交易日。"""
    d = _as_date(d)
    return d.weekday() < 5 and d.isoformat() not in HOLIDAYS


def prev_trade_day(d):
    """d 之前(不含 d)最近的一个交易日。"""
    d = _as_date(d) - datetime.timedelta(days=1)
    while not is_trade_day(d):
        d -= datetime.timedelta(days=1)
    return d


def next_trade_day(d):
    """d 之后(不含 d)最近的一个交易日。"""
    d = _as_date(d) + datetime.timedelta(days=1)
    while not is_trade_day(d):
        d += datetime.timedelta(days=1)
    return d


def beijing_now():
    return datetime.datetime.now(BJ_TZ)


def expected_asof(now=None):
    """此刻「应到的最新完整交易日」—— 全项目唯一判据。

    规则（一句话）：今天是交易日且已过 16:00(北京) 则应为今天，否则为上一个交易日。

    对照旧的三份周末算术，差异只在节假日：
      · 长假中任意时刻 -> 回退到节前最后一个交易日（旧算术会给出一个不存在的交易日）
      · 周末 -> 周五（与旧算术一致）
      · 周一凌晨 -> 上周五（与旧算术一致）
      · 交易日 16:00 后 -> 当天（与旧算术一致）

    now: 北京时间 datetime（naive 视为北京时间）；默认取当前时刻。
    """
    if now is None:
        now = beijing_now()
    elif now.tzinfo is not None:
        now = now.astimezone(BJ_TZ)
    today = now.date()
    tmin = now.hour * 60 + now.minute
    if is_trade_day(today) and tmin >= SETTLE_MIN:
        return today
    return prev_trade_day(today)


def future_trade_dates(last_date, n):
    """last_date 之后(不含)的 n 个交易日。"""
    d = _as_date(last_date)
    out = []
    while len(out) < n:
        d += datetime.timedelta(days=1)
        if is_trade_day(d):
            out.append(d.isoformat())
    return out


def missing_trade_days(last_date, now=None):
    """last_date 之后、应到交易日(含)之前，**本该有数据却缺失的交易日个数**。

    [2026-10-08] 为什么不能用自然日差：长假休市时数据合法地停在节前最后一个交易日，
    自然日差却天天增长 —— 2026 国庆休市 7 天，asof=09-30 到了 10-06 就是"滞后 6 天"，
    质量块会持续报"数据滞后 N 天"（前端 qNote 显示"发现 1 项问题"），而真实缺失的
    交易日数是 0（休市本就没有交易日数据可缺）。故障判据必须用交易日口径。

    语义：
      长假中            -> 0（没有交易日可缺）
      交易日 16:00 后当天数据未到 -> 1（可能只是源还没发布，故阈值定 2）
      真漏更 3 个交易日 -> 3
    """
    exp = expected_asof(now)
    d = _as_date(last_date)
    n = 0
    d = next_trade_day(d)
    while d <= exp:
        n += 1
        d = next_trade_day(d)
    return n


# ==================== CLI ====================
def _selftest():
    """受控断言：正例(节假日/周末/凌晨) + 反例(交易日 16:00 后必须落在当天)。

    这些断言就是 CI 判据的契约 —— 若哪天有人把 expected_asof 改回纯周末算术，
    长假用例会立刻变红。
    """
    def E(s):
        return expected_asof(datetime.datetime.fromisoformat(s)).isoformat()

    cases = [
        # (北京时间, 期望应到交易日, 说明)
        ("2026-10-01T23:19", "2026-09-30", "国庆首日深夜：休市，应到仍是节前最后交易日"),
        ("2026-10-01T09:00", "2026-09-30", "国庆首日凌晨"),
        ("2026-10-07T22:00", "2026-09-30", "长假最后一天深夜"),
        ("2026-10-08T08:00", "2026-09-30", "节后首个交易日开盘前：当日尚未落定"),
        ("2026-10-08T16:00", "2026-10-08", "节后首个交易日 16:00：当日已落定"),
        ("2026-10-08T23:59", "2026-10-08", "节后首个交易日深夜"),
        ("2026-10-09T03:00", "2026-10-08", "周五凌晨：上一个交易日是周四"),
        ("2026-10-10T12:00", "2026-10-09", "周六：回退到周五"),
        ("2026-10-11T23:00", "2026-10-09", "周日深夜：回退到周五"),
        ("2026-10-12T01:00", "2026-10-09", "周一凌晨：回退到上周五"),
        ("2026-10-12T16:30", "2026-10-12", "周一 16:30：当日已落定"),
        ("2026-09-30T16:00", "2026-09-30", "节前最后交易日 16:00"),
        ("2026-09-30T15:59", "2026-09-29", "节前最后交易日 15:59：当日未落定"),
        ("2026-02-13T22:00", "2026-02-13", "春节前最后交易日(周五)深夜"),
        ("2026-02-17T22:00", "2026-02-13", "春节假期中(周二)：回退到节前"),
        ("2026-02-24T22:00", "2026-02-24", "春节后首个交易日(周二)深夜"),
        ("2026-05-06T23:30", "2026-05-06", "劳动节后首个交易日(周三)深夜"),
        ("2026-05-04T23:30", "2026-04-30", "劳动节假期中(周一)：回退到节前周四"),
        ("2026-06-18T22:00", "2026-06-18", "端午前最后交易日(周四)深夜"),
        ("2026-06-23T22:00", "2026-06-23", "端午后首个交易日(周二)深夜"),
    ]
    bad = []
    for at, want, why in cases:
        got = E(at)
        if got != want:
            bad.append("  %s 期望 %s 实得 %s  (%s)" % (at, want, got, why))
    # 反例：把长假中的每一分钟都过一遍，绝不允许出现"不存在的交易日"
    d = datetime.date(2026, 10, 1)
    while d <= datetime.date(2026, 10, 7):
        for hm in ((0, 0), (9, 30), (12, 0), (16, 0), (23, 59)):
            at = datetime.datetime(d.year, d.month, d.day, hm[0], hm[1])
            if expected_asof(at).isoformat() != "2026-09-30":
                bad.append("  长假遍历 %s 未回退到节前" % at.isoformat())
        d += datetime.timedelta(days=1)
    # 反向：任何返回的"应到交易日"自身必须是交易日
    probe = datetime.date(2026, 1, 1)
    while probe <= datetime.date(2026, 12, 31):
        for hm in ((8, 0), (16, 30)):
            e = expected_asof(datetime.datetime(probe.year, probe.month, probe.day, hm[0], hm[1]))
            if not is_trade_day(e):
                bad.append("  2026-%s 给出的应到交易日 %s 自身不是交易日" % (probe.isoformat(), e.isoformat()))
        probe += datetime.timedelta(days=1)
    if bad:
        print("日历自检失败：")
        print("\n".join(bad[:20]))
        return 1
    print("日历自检通过：%d 个定点用例 + 长假遍历 + 全年 730 个" % len(cases) +
          "应到值合法性探针（共覆盖 %d 个时点）" % (len(cases) + 35 + 730))
    return 0


def main(argv):
    if "--selftest" in argv:
        return _selftest()
    at = None
    if "--at" in argv:
        i = argv.index("--at")
        if i + 1 < len(argv):
            at = argv[i + 1]
    now = None
    if at:
        now = datetime.datetime.fromisoformat(at)
        if now.tzinfo is None:
            now = now.replace(tzinfo=BJ_TZ)
    e = expected_asof(now)
    if "--json" in argv:
        n = now or beijing_now()
        print(json.dumps({
            "expected": e.isoformat(),
            "bj_now": n.strftime("%Y-%m-%dT%H:%M"),
            "is_trade_day_today": is_trade_day(n.date()),
            "settled": is_trade_day(n.date()) and (n.hour * 60 + n.minute) >= SETTLE_MIN,
            "calendar_official_until": CAL_FULL_UNTIL,
            "calendar_cover_until": CAL_COVER_UNTIL,
        }, ensure_ascii=False))
    else:
        print(e.isoformat())
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
