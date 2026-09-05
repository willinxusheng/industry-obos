# -*- coding: utf-8 -*-
"""拉取申万二级行业指数(109 个) 5 年日K -> data/sub_klines.json

与 fetch_data.py(申万一级 31 个)同构：数据源多源顺序回退，口径统一「不复权日线」。
  ① 腾讯 proxy.finance.qq.com newfqkline  (主源，2026-09-03 实测 109/109 可拉)
  ② 东方财富 push2his stock/kline         (secid=90.{sw})
  ③ 新浪 money.finance.sina.com.cn sw{sw}
任一源成功即用；单行业三源全失败计入 fail；>5 行业失败则整体 abort（109 个基数大，
容忍度按 ~5% 设，比一级的 >2/31 略宽，绝不用残缺数据覆盖线上）。

清单口径（2026-09-03 定版，勿凭记忆改代码——申万二级代码非连号）：
  来源：乐咕乐股申万行业总览(2021版) 131 个二级行业，剔除成分股 <10 的 22 个微型行业
  （林业Ⅱ/农业综合Ⅱ/其他家电Ⅱ/旅游零售Ⅱ/体育Ⅱ/油气开采Ⅱ/医疗美容/渔业/动物保健Ⅱ/
   非金属材料Ⅱ/汽车服务/乘用车/黑色家电/厨卫电器/专业连锁Ⅱ/酒店餐饮/国有大型银行Ⅱ/
   股份制银行Ⅱ/保险Ⅱ/房屋建设Ⅱ/航天装备Ⅱ/焦炭Ⅱ 等）——成分过少信号噪声大，
  且腾讯行情不收录其中 7 个（实测无返回）。剩余 109 个逐一经腾讯行情接口交叉验证
  代码-名称零不一致。成分数随申万定期调样变化，此处为定版快照。
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

# 申万二级行业 109 个 (2021版, 成分>=10): (sw 6位代码, 名称, 所属一级, 成分数)
SW2 = [
    ("801016", "种植业", "农林牧渔", 20),
    ("801014", "饲料", "农林牧渔", 18),
    ("801012", "农产品加工", "农林牧渔", 23),
    ("801017", "养殖业", "农林牧渔", 22),
    ("801033", "化学原料", "基础化工", 57),
    ("801034", "化学制品", "基础化工", 172),
    ("801032", "化学纤维", "基础化工", 26),
    ("801036", "塑料", "基础化工", 72),
    ("801037", "橡胶", "基础化工", 17),
    ("801038", "农化制品", "基础化工", 59),
    ("801043", "冶钢原料", "钢铁", 10),
    ("801044", "普钢", "钢铁", 22),
    ("801045", "特钢Ⅱ", "钢铁", 12),
    ("801051", "金属新材料", "有色金属", 32),
    ("801055", "工业金属", "有色金属", 59),
    ("801053", "贵金属", "有色金属", 11),
    ("801054", "小金属", "有色金属", 27),
    ("801056", "能源金属", "有色金属", 11),
    ("801081", "半导体", "电子", 181),
    ("801083", "元件", "电子", 61),
    ("801084", "光学光电子", "电子", 93),
    ("801082", "其他电子Ⅱ", "电子", 33),
    ("801085", "消费电子", "电子", 91),
    ("801086", "电子化学品Ⅱ", "电子", 35),
    ("801093", "汽车零部件", "汽车", 244),
    ("801881", "摩托车及其他", "汽车", 17),
    ("801096", "商用车", "汽车", 13),
    ("801111", "白色家电", "家用电器", 11),
    ("801113", "小家电", "家用电器", 20),
    ("801115", "照明设备Ⅱ", "家用电器", 12),
    ("801116", "家电零部件Ⅱ", "家用电器", 29),
    ("801124", "食品加工", "食品饮料", 25),
    ("801125", "白酒Ⅱ", "食品饮料", 19),
    ("801126", "非白酒", "食品饮料", 16),
    ("801127", "饮料乳品", "食品饮料", 26),
    ("801128", "休闲食品", "食品饮料", 22),
    ("801129", "调味发酵品Ⅱ", "食品饮料", 14),
    ("801131", "纺织制造", "纺织服饰", 32),
    ("801132", "服装家纺", "纺织服饰", 59),
    ("801133", "饰品", "纺织服饰", 15),
    ("801143", "造纸", "轻工制造", 23),
    ("801141", "包装印刷", "轻工制造", 40),
    ("801142", "家居用品", "轻工制造", 73),
    ("801145", "文娱用品", "轻工制造", 20),
    ("801151", "化学制药", "医药生物", 149),
    ("801155", "中药Ⅱ", "医药生物", 66),
    ("801152", "生物制品", "医药生物", 52),
    ("801154", "医药商业", "医药生物", 32),
    ("801153", "医疗器械", "医药生物", 129),
    ("801156", "医疗服务", "医药生物", 51),
    ("801161", "电力", "公用事业", 109),
    ("801163", "燃气Ⅱ", "公用事业", 28),
    ("801178", "物流", "交通运输", 47),
    ("801179", "铁路公路", "交通运输", 33),
    ("801991", "航空机场", "交通运输", 12),
    ("801992", "航运港口", "交通运输", 34),
    ("801181", "房地产开发", "房地产", 83),
    ("801183", "房地产服务", "房地产", 12),
    ("801202", "贸易Ⅱ", "商贸零售", 12),
    ("801203", "一般零售", "商贸零售", 60),
    ("801206", "互联网电商", "商贸零售", 18),
    ("801218", "专业服务", "社会服务", 29),
    ("801993", "旅游及景区", "社会服务", 25),
    ("801994", "教育", "社会服务", 17),
    ("801784", "城商行Ⅱ", "银行", 17),
    ("801785", "农商行Ⅱ", "银行", 10),
    ("801193", "证券Ⅱ", "非银金融", 50),
    ("801191", "多元金融", "非银金融", 24),
    ("801231", "综合Ⅱ", "综合", 20),
    ("801711", "水泥", "建筑材料", 21),
    ("801712", "玻璃玻纤", "建筑材料", 16),
    ("801713", "装修建材", "建筑材料", 35),
    ("801722", "装修装饰Ⅱ", "建筑装饰", 22),
    ("801723", "基础建设", "建筑装饰", 40),
    ("801724", "专业工程", "建筑装饰", 37),
    ("801726", "工程咨询服务Ⅱ", "建筑装饰", 40),
    ("801731", "电机Ⅱ", "电力设备", 26),
    ("801733", "其他电源设备Ⅱ", "电力设备", 32),
    ("801735", "光伏设备", "电力设备", 67),
    ("801736", "风电设备", "电力设备", 31),
    ("801737", "电池", "电力设备", 97),
    ("801738", "电网设备", "电力设备", 126),
    ("801072", "通用设备", "机械设备", 220),
    ("801074", "专用设备", "机械设备", 181),
    ("801076", "轨交设备Ⅱ", "机械设备", 30),
    ("801077", "工程机械", "机械设备", 28),
    ("801078", "自动化设备", "机械设备", 82),
    ("801742", "航空装备Ⅱ", "国防军工", 46),
    ("801743", "地面兵装Ⅱ", "国防军工", 12),
    ("801744", "航海装备Ⅱ", "国防军工", 10),
    ("801745", "军工电子Ⅱ", "国防军工", 60),
    ("801101", "计算机设备", "计算机", 82),
    ("801103", "IT服务Ⅱ", "计算机", 122),
    ("801104", "软件开发", "计算机", 130),
    ("801764", "游戏Ⅱ", "传媒", 26),
    ("801765", "广告营销", "传媒", 28),
    ("801766", "影视院线", "传媒", 19),
    ("801767", "数字媒体", "传媒", 14),
    ("801769", "出版", "传媒", 29),
    ("801995", "电视广播Ⅱ", "传媒", 14),
    ("801223", "通信服务", "通信", 37),
    ("801102", "通信设备", "通信", 85),
    ("801951", "煤炭开采", "煤炭", 26),
    ("801962", "油服工程", "石油石化", 13),
    ("801963", "炼化及贸易", "石油石化", 30),
    ("801971", "环境治理", "环保", 104),
    ("801972", "环保设备Ⅱ", "环保", 26),
    ("801981", "个护用品", "美容护理", 12),
    ("801982", "化妆品", "美容护理", 13),
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
    """腾讯 K线公共解析. host/path 不同即不同接入点, 数据口径完全一致(详见 fetch_data 同名函数)."""
    url = "https://%s/%s?param=pt01%s,day,,,1300,day" % (host, path, code)
    d = get_json(url)
    node = (d.get("data", {}) or {}).get("pt01" + code) or {}
    key = "qfqday" if "qfqday" in node else "day"
    rows = []
    for r in (node.get(key) or []):
        rows.append([r[0], float(r[1]), float(r[2]), float(r[3]), float(r[4]), float(r[5])])
    return rows, key


def parse_tencent(code):
    """腾讯 newfqkline；不复权(day 键)；如实回传复权键，逻辑同 fetch_data.parse_tencent。"""
    return _tencent_rows(code, "proxy.finance.qq.com",
                         "ifzqgtimg/appstock/app/newfqkline/get")


def parse_tencent_bak(code):
    """腾讯备用接入点 ifzq.gtimg.cn；与主源逐日完全一致(实测最大绝对差 0.0)。"""
    return _tencent_rows(code, "ifzq.gtimg.cn", "appstock/app/newfqkline/get")


def parse_eastmoney(code):
    """东方财富 push2his；secid=90.{sw}；fqt=0 不复权。"""
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
    """新浪 getKLineData；symbol=sw{sw}；scale=240 日线；不复权。

    [2026-09-05 实测] 不支持申万行业代码(8 种 symbol 格式全部 HTTP 200 + null)，
    已从行业链路移除。基准链路的 sh000300 指数代码仍然有效，那边保留 —— 别一起删。
    """
    url = ("https://money.finance.sina.com.cn/quotes_service/api/json_v2.php/"
           "CN_MarketData.getKLineData?symbol=sw%s&scale=240&ma=no&datalen=1300") % code
    d = get_json(url)
    rows = []
    for r in (d or []):
        rows.append([r["day"], float(r["open"]), float(r["close"]),
                     float(r["high"]), float(r["low"]), float(r["volume"])])
    return rows, "day"


# 优先级：腾讯(主) → 腾讯备用接入点(同口径镜像) → 东财(第三方回退)
# 与 fetch_data.py 保持一致。新浪已移除(不支持申万行业代码)。
SOURCES = [
    ("tencent", parse_tencent),
    ("tencent_bak", parse_tencent_bak),
    ("eastmoney", parse_eastmoney),
]


def fetch_rows(sw):
    """按 SOURCES 顺序尝试，返回 (rows, src_name, fq)；全失败抛 RuntimeError。"""
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
    n_all = len(SW2)
    for i, (sw, name, parent, n_const) in enumerate(SW2):
        try:
            rows, src, fq = fetch_rows(sw)
            out["pt01" + sw] = {"name": name, "sw": sw, "parent": parent,
                                "n_constituents": n_const,
                                "fq_key": ("qfq" if fq == "qfqday" else "day"),
                                "src": src, "rows": rows}
            print("%d/%d pt01%s %s(%s) src=%s rows=%d" % (i + 1, n_all, sw, name, parent, src, len(rows)), flush=True)
        except Exception as e:
            fail.append(("pt01" + sw, name, str(e)[:120]))
            print("%d/%d pt01%s %s FAILED %s" % (i + 1, n_all, sw, name, str(e)[:120]), flush=True)
        time.sleep(0.3)

    # [A5] 防部分部署：>5 行业取数全失败则整体中止（109 的 ~5%），绝不用残缺数据覆盖线上
    if len(out) < n_all - 5:
        raise SystemExit("FAILED: only %d/%d sub-industries fetched -> abort (avoid partial deploy)" % (len(out), n_all))

    path = os.path.join(BASE, "data", "sub_klines.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False)
    print("saved:", len(out), "->", path, flush=True)
    srcset = sorted({v.get("src") for v in out.values()})
    print("sources used:", srcset, flush=True)
    if fail:
        print("FAILED:", json.dumps(fail, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
