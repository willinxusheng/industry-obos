# -*- coding: utf-8 -*-
"""拉取申万一级行业指数(31 个) 5 年日K -> data/industry_klines.json

数据源多源顺序回退（海外 runner 也能取数，对标斐波那契看板的 eastmoney/yahoo/stooq 加固）：
  ① 腾讯 proxy.finance.qq.com newfqkline  (中国本地主源，本机/腾讯 CDN 可达)
  ② 东方财富 push2his stock/kline         (secid=90.{sw}, 海外常被证实可达)
  ③ 新浪 money.finance.sina.com.cn sw{sw}  (海外常被证实可达)
任一源成功即用；单行业三源全失败计入 fail；>2 行业失败则整体 abort，避免部署部分残缺数据。

口径统一：所有源均取「不复权日线」(fq_key="day")，与 [A5] PIT 复现纪律一致
（腾讯 URL 末位复权参数显式传 day、东财 fqt=0、新浪 sw 均为不复权）。

[2026-09-05] 修正：腾讯的复权参数原先写的是 qfq，与本文件注释、fetch_benchmark.py
的基准口径、以及东财/新浪回退源的口径全部不一致。实测该接口对复权参数无响应
（传 qfq 仍返回 day 键），所以长期没出事；但这是"靠对方不理我"在兜底——一旦腾讯
哪天开始响应复权参数，行业数据会瞬间变成前复权，而基准仍是不复权，rs_pct 相对强度
跨基准偏估、历史指标不可复现（违背 PIT）。故显式改为 day，三源口径真正统一。
"""
import datetime
import json
import os
import ssl
import time
import urllib.request

BASE = os.path.dirname(os.path.abspath(__file__))
CTX = ssl.create_default_context()
CTX.check_hostname = False
CTX.verify_mode = ssl.CERT_NONE
HDRS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}

# 申万一级行业 31 个 (2021版): sw 6位代码
SW1 = [
    ("801010", "农林牧渔"), ("801030", "基础化工"), ("801040", "钢铁"),
    ("801050", "有色金属"), ("801080", "电子"), ("801880", "汽车"),
    ("801110", "家用电器"), ("801120", "食品饮料"), ("801130", "纺织服饰"),
    ("801140", "轻工制造"), ("801150", "医药生物"), ("801160", "公用事业"),
    ("801170", "交通运输"), ("801180", "房地产"), ("801200", "商贸零售"),
    ("801210", "社会服务"), ("801230", "综合"), ("801710", "建筑材料"),
    ("801720", "建筑装饰"), ("801730", "电力设备"), ("801740", "国防军工"),
    ("801750", "计算机"), ("801760", "传媒"), ("801770", "通信"),
    ("801780", "银行"), ("801790", "非银金融"), ("801950", "煤炭"),
    ("801960", "石油石化"), ("801970", "环保"), ("801980", "美容护理"),
    ("801890", "机械设备"),
]


def get_json(url, tries=5):
    last = None
    for i in range(tries):
        try:
            req = urllib.request.Request(url, headers=HDRS)
            raw = urllib.request.urlopen(req, timeout=25, context=CTX).read().decode("utf-8", "ignore")
            return json.loads(raw)
        except Exception as e:
            last = e
            time.sleep(1.2 * (i + 1))
    raise last


def _tencent_rows(code, host, path):
    """腾讯 K线公共解析. host/path 不同即为不同接入点, 数据口径完全一致."""
    url = "https://%s/%s?param=pt01%s,day,,,1300,day" % (host, path, code)
    d = get_json(url)
    node = (d.get("data", {}) or {}).get("pt01" + code) or {}
    key = "qfqday" if "qfqday" in node else "day"
    rows = []
    for r in (node.get(key) or []):
        rows.append([r[0], float(r[1]), float(r[2]), float(r[3]), float(r[4]), float(r[5])])
    return rows, key


def parse_tencent(code):
    """腾讯 newfqkline；返回 ([date,open,close,high,low,vol], 实际复权键)；不复权(day 键)。

    注意: 必须如实回传腾讯返回的复权键(qfqday 或 day), 由 main 据实标注 fq_key。
    若某日腾讯改为返回前复权(qfqday), fq_key 会被标为 "qfq", 触发 compute.quality_gate
    的 [A5] 复权断言 FAIL —— 绝不让"前复权数据被静默标成 day"绕过 PIT 历史可复现纪律。
    """
    return _tencent_rows(code, "proxy.finance.qq.com",
                         "ifzqgtimg/appstock/app/newfqkline/get")


def parse_tencent_bak(code):
    """腾讯备用接入点 ifzq.gtimg.cn.

    [2026-09-05 实测] 与主源 1300 个交易日逐日收盘价完全一致(最大绝对差 0.0),
    口径天然同源, 不会像第三方源那样引入跨源偏估。价值在于: 主源域名被限流/故障/劫持时
    可无缝顶上, 是真正可用的冗余。
    """
    return _tencent_rows(code, "ifzq.gtimg.cn", "appstock/app/newfqkline/get")


def parse_eastmoney(code):
    """东方财富 push2his；secid=90.{sw}（申万行业）；fqt=0 不复权；klines 字符串顺序 date,open,close,high,low,vol。"""
    url = ("https://push2his.eastmoney.com/api/qt/stock/kline/get"
           "?secid=90.%s&fields1=f1,f2,f3,f4,f5,f6&fields2=f51,f52,f53,f54,f55,f56"
           "&klt=101&fqt=0&end=20500101&lmt=1300") % code
    d = get_json(url)
    node = (d.get("data") or {})
    kls = node.get("klines") or []
    rows = []
    for s in kls:
        p = s.split(",")
        if len(p) < 6:
            continue
        rows.append([p[0], float(p[1]), float(p[2]), float(p[3]), float(p[4]), float(p[5])])
    return rows, "day"


def parse_sina(code):
    """新浪 getKLineData；symbol=sw{sw}；scale=240 日线；不复权；字段 day/open/high/low/close/volume。

    [2026-09-05 实测] 该接口【不支持申万行业代码】：对 sw801780 返回 HTTP 200 + JSON null，
    试过 sw/sh/sz/b_/hb 等 8 种 symbol 格式全部为 null；而对个股 sh600000、指数 sh000300
    均正常返回。这是接口能力问题(不是网络或我们写错), 与运行环境无关、全球一致。
    故行业链路已不再使用它；但基准链路(沪深300 = sh000300)仍有效，那边保留。
    """
    url = ("https://money.finance.sina.com.cn/quotes_service/api/json_v2.php/"
           "CN_MarketData.getKLineData?symbol=sw%s&scale=240&ma=no&datalen=1300") % code
    d = get_json(url)
    rows = []
    for r in (d or []):
        # 新浪字段顺序 day/open/high/low/close/volume，用命名键避免顺序错配
        rows.append([r["day"], float(r["open"]), float(r["close"]),
                     float(r["high"]), float(r["low"]), float(r["volume"])])
    return rows, "day"


# 优先级：腾讯(主) → 腾讯备用接入点(同口径镜像) → 东财(第三方, 海外可达回退)
# [2026-09-05] 新浪已从行业链路移除(不支持申万行业代码, 见 parse_sina 注释)。
# 注意别连基准链路的新浪也一起删 —— 那边取的是 sh000300 指数代码, 实测仍有效。
# 冗余是否真实可用, 请用 `python check_sources.py` 定期自检, 别只靠这里的假设。
SOURCES = [
    ("tencent", parse_tencent),
    ("tencent_bak", parse_tencent_bak),
    ("eastmoney", parse_eastmoney),
]


def fetch_rows(sw):
    """按 SOURCES 顺序尝试，返回 (rows, src_name)；全失败抛 RuntimeError。"""
    errs = []
    for name, fn in SOURCES:
        try:
            rows, fq = fn(sw)
            if len(rows) < 1000:
                errs.append("%s:rows=%d" % (name, len(rows)))
                continue
            ds = [r[0] for r in rows]
            if ds != sorted(ds):
                errs.append("%s:unsorted" % name)
                continue
            return rows, name, fq
        except Exception as e:
            errs.append("%s:%s" % (name, str(e)[:60]))
    raise RuntimeError("; ".join(errs))


def main():
    out, fail = {}, []
    today = datetime.date.today()
    for i, (sw, name) in enumerate(SW1):
        try:
            rows, src, fq = fetch_rows(sw)
            out["pt01" + sw] = {"name": name, "sw": sw, "fq_key": ("qfq" if fq == "qfqday" else "day"), "src": src, "rows": rows}
            print("%d/31 pt01%s %s src=%s rows=%d" % (i + 1, sw, name, src, len(rows)), flush=True)
        except Exception as e:
            fail.append(("pt01" + sw, name, str(e)[:120]))
            print("%d/31 pt01%s %s FAILED %s" % (i + 1, sw, name, str(e)[:120]), flush=True)
        time.sleep(0.3)

    # [A5] 防部分部署：>2 行业取数全失败则整体中止，绝不用残缺数据覆盖线上
    if len(out) < 29:
        raise SystemExit("FAILED: only %d/31 industries fetched -> abort (avoid partial deploy)" % len(out))

    path = os.path.join(BASE, "data", "industry_klines.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False)
    print("saved:", len(out), "->", path, flush=True)
    srcset = sorted({v.get("src") for v in out.values()})
    print("sources used:", srcset, flush=True)
    if fail:
        print("FAILED:", json.dumps(fail, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
