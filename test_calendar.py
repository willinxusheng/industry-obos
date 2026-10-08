# -*- coding: utf-8 -*-
"""门禁：交易日历唯一事实源 + 三处 CI 判据同源（[2026-10-08]）

背景（真事故，不是假想）：
  2026 国庆长假（10-01 ~ 10-07 休市）期间，CI 里三处「应到交易日」判据**各自内联了
  一份"纯周末算术"**（周六=今天-1 / 周日=今天-2 / 周一凌晨=今天-3 / 其余=昨天），
  一份都不含法定节假日，于是三处同时误判：
    · watchdog 把 10-01 当交易日 -> 判定"真漏更" -> 开 issue #5，且每 30 分钟复检
      都再次触发 daily.yml 补跑（issue 上刷了 28 条评论、长假跑了 45 次 daily）；
    · Freshness gate 同样误判 -> 长假 7 天每天强制全量重建（每天一条 daily refresh
      提交），并发重建还撞出一次 rebase 冲突失败；
    · auto-close 判据同源 -> 数据完全健康（休市本就无新数据）也关不掉告警，
      红标从 10-01 挂到 10-08。
  用户看到的就是"线上有更新失败的提示"，而链路其实一直是好的。

  根因不是某处写错，而是**同一口径被复制了三份**。修复 = 抽成 trading_calendar.py
  单一事实源，三处统一调用。本门禁把事故现场固化成断言。

本门禁的四类断言：
  [1] 日历函数契约：长假必须回退到节前最后一个交易日（事故时点逐个反演）；
     反例：交易日 16:00 之后必须是当天（防止有人"为避免误报"改成永远回退）。
  [2] 单一事实源：obos_core 不得再自带一份假日表，必须与 trading_calendar 同源。
  [3] 三处 CI 判据必须调用 trading_calendar.py，且"应到交易日"必须经 env 注入到
     github-script（JS 侧不许再自己算）。
  [4] 静态扫描：workflow 里不得再出现旧的纯周末算术字面量。
     ⚠️ 扫描器自带**正控**：把旧代码样本喂给它必须命中，否则"扫不出来"会被当成"没问题"。
"""
import io
import os
import re
import subprocess
import sys
import datetime

HERE = os.path.dirname(os.path.abspath(__file__))
os.chdir(HERE)
sys.path.insert(0, HERE)

fail = 0
n_pass = 0


def bad(msg):
    global fail
    fail += 1
    print('  FAIL  ' + msg)


def ok(msg):
    global n_pass
    n_pass += 1
    print('  ok    ' + msg)


def read(p):
    return io.open(os.path.join(HERE, p), encoding='utf-8').read()


# ══════════════════════════════════════════════════════════════════════════
print('== [1] 日历函数契约 ==')
import trading_calendar as tc  # noqa: E402


def E(s):
    d = datetime.datetime.fromisoformat(s)
    if d.tzinfo is None:
        d = d.replace(tzinfo=tc.BJ_TZ)
    return tc.expected_asof(d).isoformat()


# 事故现场：长假期间每一条 watchdog 评论都在说"应到交易日: 2026-10-01/02/…"，
# 而线上 asof 合理停在 09-30（休市）。这些时点全部必须回退到 09-30。
INCIDENT_CASES = [
    ('2026-10-01T23:19', '2026-09-30'),   # issue #5 的开启时点（北京 10-01 23:19）
    ('2026-10-02T23:35', '2026-09-30'),   # cron-job.org 深夜班（长假中）
    ('2026-10-02T08:39', '2026-09-30'),   # 凌晨班（长假中）
    ('2026-10-05T23:35', '2026-09-30'),   # 长假中的周一深夜班
    ('2026-10-07T16:00', '2026-09-30'),   # 长假最后一天 16:00
    ('2026-10-07T23:59', '2026-09-30'),   # 长假最后时刻
    ('2026-10-08T08:00', '2026-09-30'),   # 节后首日开盘前（当日未落定）
    ('2026-10-08T16:00', '2026-10-08'),   # 节后首日 16:00（当日落定）
    ('2026-10-08T19:00', '2026-10-08'),
]
for at, want in INCIDENT_CASES:
    got = E(at)
    if got == want:
        ok('长假反演 %s -> %s' % (at, got))
    else:
        bad('长假反演 %s 期望 %s 实得 %s（旧 bug 回归：休市日被当成交易日）' % (at, want, got))

# 反例（防止"为躲误报而永远回退"）：交易日 16:00 之后必须是当天
NEG_CASES = [
    ('2026-10-08T16:00', '2026-10-08'),
    ('2026-10-09T22:00', '2026-10-09'),
    ('2026-09-30T23:30', '2026-09-30'),
]
for at, want in NEG_CASES:
    got = E(at)
    if got == want:
        ok('反例（交易日已落定必须取当天）%s -> %s' % (at, got))
    else:
        bad('反例 %s 期望 %s 实得 %s（若改成"永远回退上一交易日"，真漏更将永不告警）'
            % (at, want, got))

# 长假逐时点遍历 + 全年"返回值自身必须是交易日"探针
holiday_bad = []
d = datetime.date(2026, 10, 1)
while d <= datetime.date(2026, 10, 7):
    for hh, mm in ((0, 0), (9, 29), (9, 30), (16, 0), (23, 59)):
        if E('%sT%02d:%02d' % (d.isoformat(), hh, mm)) != '2026-09-30':
            holiday_bad.append('%s %02d:%02d' % (d.isoformat(), hh, mm))
    d += datetime.timedelta(days=1)
if holiday_bad:
    bad('长假遍历未回退到节前：%s' % ', '.join(holiday_bad[:5]))
else:
    ok('长假 7 天 × 5 时点 = 35 个时点全部回退到节前')

probe_bad = []
p = datetime.date(2026, 1, 1)
while p <= datetime.date(2026, 12, 31):
    for hh, mm in ((8, 0), (16, 30)):
        e = tc.expected_asof(datetime.datetime(p.year, p.month, p.day, hh, mm, tzinfo=tc.BJ_TZ))
        if not tc.is_trade_day(e):
            probe_bad.append(p.isoformat())
    p += datetime.timedelta(days=1)
if probe_bad:
    bad('2026 年有 %d 天给出了非交易日的"应到交易日"：%s'
        % (len(probe_bad), ', '.join(probe_bad[:5])))
else:
    ok('2026 全年 730 个探针：返回的应到值恒为交易日（周末+节假日全部跳过）')

# 日历内置自检（含跨节日：春节/清明/劳动/端午/中秋/国庆）
r = subprocess.run([sys.executable, 'trading_calendar.py', '--selftest'],
                   capture_output=True, text=True)
if r.returncode == 0:
    ok('trading_calendar.py --selftest 通过')
else:
    bad('trading_calendar.py --selftest 失败：%s' % (r.stdout + r.stderr)[:300])

# CLI 契约：CI 直接消费 stdout，必须是一行 ISO 日期
r = subprocess.run([sys.executable, 'trading_calendar.py', '--at', '2026-10-01T23:19'],
                   capture_output=True, text=True)
if r.returncode == 0 and r.stdout.strip() == '2026-09-30':
    ok('CLI --at 反演 => %s' % r.stdout.strip())
else:
    bad('CLI --at 2026-10-01T23:19 期望 2026-09-30，实得 %r (rc=%d)'
        % (r.stdout.strip(), r.returncode))
r = subprocess.run([sys.executable, 'trading_calendar.py'], capture_output=True, text=True)
if r.returncode == 0 and re.fullmatch(r'\d{4}-\d{2}-\d{2}\n?', r.stdout):
    ok('CLI 默认输出为单行 ISO 日期：%s' % r.stdout.strip())
else:
    bad('CLI 默认输出格式不符（CI 会把它当应到交易日用）：%r' % r.stdout[:80])

# 缺失交易日口径：compute.py 的"数据滞后"故障判据（绝不能用自然日差，长假必然误报）
MTD_CASES = [
    ('2026-09-30', '2026-10-01T23:19', 0, '长假首日（issue #5 的现场时点）：休市没有交易日可缺'),
    ('2026-09-30', '2026-10-06T22:00', 0, '长假中：自然日差 6 天，缺失交易日数必须是 0'),
    ('2026-09-30', '2026-10-08T16:30', 1, '节后首日 16:30：当天数据可能尚未落定（阈值 2 故不告警）'),
    ('2026-09-30', '2026-10-09T16:30', 2, '连续缺失 2 个交易日 -> 应告警'),
    ('2026-09-28', '2026-10-08T16:30', 3, '真漏更 3 个交易日（跨长假仍要正确计数）'),
    ('2026-10-08', '2026-10-08T22:00', 0, '正常当日'),
]
for last, at, want, why in MTD_CASES:
    got = tc.missing_trade_days(last, datetime.datetime.fromisoformat(at).replace(tzinfo=tc.BJ_TZ))
    if got == want:
        ok('缺失交易日 %s/@%s = %d（%s）' % (last, at, got, why))
    else:
        bad('缺失交易日 %s/@%s 期望 %d 实得 %d（%s）' % (last, at, want, got, why))

# ══════════════════════════════════════════════════════════════════════════
print('== [2] 单一事实源：obos_core 不得再自带假日表 ==')
import obos_core as oc  # noqa: E402

if oc.HOLIDAYS is tc.HOLIDAYS or set(oc.HOLIDAYS) == set(tc.HOLIDAYS):
    ok('obos_core.HOLIDAYS 与 trading_calendar.HOLIDAYS 一致（%d 个假日）' % len(tc.HOLIDAYS))
else:
    bad('两份假日表不一致：obos_core 多 %s / 少 %s'
        % (sorted(set(oc.HOLIDAYS) - set(tc.HOLIDAYS))[:3],
           sorted(set(tc.HOLIDAYS) - set(oc.HOLIDAYS))[:3]))
if oc.future_trade_dates is tc.future_trade_dates:
    ok('future_trade_dates 为同一函数对象（未各自复制实现）')
else:
    bad('obos_core.future_trade_dates 不是 trading_calendar 的同一个函数 —— 又出现了第二份实现')
if oc.CAL_FULL_UNTIL == tc.CAL_FULL_UNTIL and oc.CAL_COVER_UNTIL == tc.CAL_COVER_UNTIL:
    ok('CAL_FULL_UNTIL=%s / CAL_COVER_UNTIL=%s 同源' % (oc.CAL_FULL_UNTIL, oc.CAL_COVER_UNTIL))
else:
    bad('日历覆盖常量不一致：%s vs %s' % (oc.CAL_FULL_UNTIL, tc.CAL_FULL_UNTIL))

src_core = read('obos_core.py')
if 'HOLIDAYS_2026 = [' in src_core:
    bad('obos_core.py 里又出现了 HOLIDAYS_2026 表字面量（应只在 trading_calendar.py 定义）')
else:
    ok('obos_core.py 内不再硬编码假日表字面量')

# ══════════════════════════════════════════════════════════════════════════
print('== [3] 三处 CI 判据必须接到事实源 ==')
daily = read('.github/workflows/daily.yml')
watch = read('.github/workflows/watchdog.yml')

for name, txt in (('daily.yml', daily), ('watchdog.yml', watch)):
    if 'python3 trading_calendar.py' in txt:
        ok('%s 调用 trading_calendar.py' % name)
    else:
        bad('%s 未调用 trading_calendar.py —— 判据又回到自算' % name)
    if 'EXPECTED_ASOF' in txt:
        ok('%s 通过 env 把应到交易日注入 github-script' % name)
    else:
        bad('%s 未把应到交易日经 env 注入 JS 侧（JS 里可能又在自己算）' % name)

# JS 侧不得再出现内联的日期回退实现
js_bad = []
for name, txt in (('daily.yml', daily), ('watchdog.yml', watch)):
    m = re.search(r'dayBack\s*=', txt)
    if m:
        js_bad.append('%s 出现 dayBack(' % name)
    if re.search(r'const\s+dayBack', txt):
        js_bad.append('%s 出现 const dayBack' % name)
if js_bad:
    bad('JS 侧仍在自算日期回退：%s' % ', '.join(js_bad))
else:
    ok('两个 workflow 的 JS 侧均无自算日期回退实现')

# 源码清单：改日历必须触发重建（daily.yml 的 grep 与 test_freshness_gate.SRC_FILES 逐字一致）
if re.search(r'obos_core\\\.py\|trading_calendar\\\.py', daily):
    ok('daily.yml 的 SRC 清单含 trading_calendar.py')
else:
    bad('daily.yml 的 Freshness gate 源码清单漏了 trading_calendar.py —— 改日历不会触发重建')
if "'trading_calendar.py'" in read('test_freshness_gate.py'):
    ok('test_freshness_gate.py 的 SRC_FILES 含 trading_calendar.py')
else:
    bad('test_freshness_gate.py 的 SRC_FILES 漏了 trading_calendar.py（两处必须逐字一致）')

# ══════════════════════════════════════════════════════════════════════════
print('== [4] 静态扫描：旧"纯周末算术"不得复活 ==')
LEGACY = [
    (r'\$\(\(DOW\s*-\s*5\)\)', '旧的"周六=DOW-5 天前"算术'),
    (r'EXPECTED=["\']?\$TODAY', '旧的"16:00 后=今天(不看节假日)"算术'),
    (r'date -u -d "\+8 hours -3 days"', '旧的"周一凌晨=3 天前"算术'),
    (r'date -u -d "\+8 hours -1 day"', '旧的"其余=1 天前"算术'),
]


def scan(text):
    hits = []
    for pat, why in LEGACY:
        if re.search(pat, text):
            hits.append(why)
    return hits


# 正控：把旧代码样本喂给扫描器，必须命中 —— 否则"扫不出来"会被误当"没问题"
NEG_SAMPLE = (
    'EXPECTED=$(date -u -d "+8 hours -$((DOW - 5)) days" +%Y-%m-%d)\n'
    'EXPECTED="$TODAY"\n'
    'EXPECTED=$(date -u -d "+8 hours -3 days" +%Y-%m-%d)\n'
    'EXPECTED=$(date -u -d "+8 hours -1 day" +%Y-%m-%d)\n'
)
hits = scan(NEG_SAMPLE)
if len(hits) == 4:
    ok('扫描器正控：旧代码样本 4/4 全部命中')
else:
    bad('扫描器正控失败：旧样本只命中 %d/4 %s —— 扫描器失效，本门禁的 [4] 等于没跑'
        % (len(hits), hits))

for name, txt in (('daily.yml', daily), ('watchdog.yml', watch)):
    hits = scan(txt)
    if hits:
        bad('%s 又出现旧的纯周末算术：%s' % (name, '、'.join(hits)))
    else:
        ok('%s 无旧算术残留' % name)

# ══════════════════════════════════════════════════════════════════════════
print()
if fail:
    print('❌ 交易日历门禁未通过：%d 项失败 / %d 项通过' % (fail, n_pass))
    sys.exit(1)
print('✅ 交易日历门禁通过：%d 项断言全绿' % n_pass)
