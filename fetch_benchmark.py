# -*- coding: utf-8 -*-
"""拉取沪深300指数(sh000300)日K作为相对强度/市场宽度基准 -> data/benchmark.json

数据源多源顺序回退（海外 runner 可达，对标斐波那契看板加固）：
  ① 腾讯 proxy.finance.qq.com newfqkline (sh000300)
  ② 东方财富 push2his stock/kline        (secid=1.000300, 海外常被证实可达)
  ③ 新浪 money.finance.sina.com.cn        (sh000300)
口径统一不复权日线，与行业取数一致。
"""
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


def parse_tencent():
    url = ("https://proxy.finance.qq.com/ifzqgtimg/appstock/app/newfqkline/get"
           "?param=sh000300,day,,,1300,qfq")
    d = get_json(url)
    node = (d.get("data", {}) or {}).get("sh000300") or {}
    key = "qfqday" if "qfqday" in node else "day"
    return [[r[0], float(r[1]), float(r[2]), float(r[3]), float(r[4]), float(r[5])]
            for r in (node.get(key) or [])]


def parse_eastmoney():
    url = ("https://push2his.eastmoney.com/api/qt/stock/kline/get"
           "?secid=1.000300&fields1=f1,f2,f3,f4,f5,f6&fields2=f51,f52,f53,f54,f55,f56"
           "&klt=101&fqt=0&end=20500101&lmt=1300")
    d = get_json(url)
    node = (d.get("data") or {})
    kls = node.get("klines") or []
    rows = []
    for s in kls:
        p = s.split(",")
        if len(p) < 6:
            continue
        rows.append([p[0], float(p[1]), float(p[2]), float(p[3]), float(p[4]), float(p[5])])
    return rows


def parse_sina():
    url = ("https://money.finance.sina.com.cn/quotes_service/api/json_v2.php/"
           "CN_MarketData.getKLineData?symbol=sh000300&scale=240&ma=no&datalen=1300")
    d = get_json(url)
    rows = []
    for r in (d or []):
        rows.append([r["day"], float(r["open"]), float(r["close"]),
                     float(r["high"]), float(r["low"]), float(r["volume"])])
    return rows


SOURCES = [
    ("tencent", parse_tencent),
    ("eastmoney", parse_eastmoney),
    ("sina", parse_sina),
]


def fetch_rows():
    errs = []
    for name, fn in SOURCES:
        try:
            rows = fn()
            if len(rows) < 1000:
                errs.append("%s:rows=%d" % (name, len(rows)))
                continue
            ds = [r[0] for r in rows]
            if ds != sorted(ds):
                errs.append("%s:unsorted" % name)
                continue
            return rows, name
        except Exception as e:
            errs.append("%s:%s" % (name, str(e)[:60]))
    raise RuntimeError("; ".join(errs))


def main():
    rows, src = fetch_rows()
    out = {"code": "sh000300", "name": "沪深300", "src": src,
           "dates": [r[0] for r in rows], "close": [r[2] for r in rows]}
    path = os.path.join(BASE, "data", "benchmark.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False)
    print("saved benchmark src=%s rows=%d last=%s" % (src, len(rows), rows[-1][0]))


if __name__ == "__main__":
    main()
