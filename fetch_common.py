# -*- coding: utf-8 -*-
"""fetch_* 三件套共享工具：未落定K线防护 + 原子写盘 + 数值健全性检查。

[2026-09-30] 背景（本轮审计实证的严重缺口）：
集合竞价窗口(09:15-09:29)拉到的当天K线是"平线"——开=收=高=低、成交量只有
正常交易日的 ~1%（09-30 实测：235,771 vs 前日 23,546,500）。这行数据能穿过
fetch 的 rows>=1000/排序校验和 compute 的 quality_gate（实测 PASS），
而 daily.yml 的盘中封锁只覆盖 09:30-16:00。若前一日更新失败、一个被
schedule 漂移推进 09:15-09:29 的 run 会：发布平线冒充收盘价 → asof 变成
当天 → 后续 run 全部判"数据已最新"skip → 平线挂一整天。

防线分两层（本模块是第一层，quality_gate 的 unsettled_last 是第二层）：
  fetch 层  ：时间判据——末行日期==今天(北京时间) 且 当前<16:00 → 剔除末行。
              零误杀（16:00 前拉到的"当天"K线必然不完整，与 CI 铁律同口径）。
  gate 层   ：系统性平线判据——过半行业末行"开=收=高=低 且 量<前日5%" → fatal。
              真实行业指数全天一个价在数学上不可能（几十只成分股加权），
              该判据只拦数据源的竞价残留/占位行，不拦一字板（行业指数无一字板）。
"""
import datetime
import json
import os


def bj_now():
    """北京时间 now（纯算术 UTC+8，不依赖 tzdata，与 daily.yml Freshness gate 同款）。"""
    return datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(hours=8)


def drop_unsettled_tail(rows, label=""):
    """剔除未落定的末行K线（盘中/集合竞价残留），返回 (rows, reason|None)。

    只剔末行、只看时间：末行日期 == 今天(北京时间) 且 当前北京时间 < 16:00。
    收盘后(16:00 起)拉到的当天数据视为已落定（CI 铁律同口径），一字板等
    真实形态一律不碰。中间行永不修改。
    """
    if not rows:
        return rows, None
    now = bj_now()
    if rows[-1][0] == now.strftime("%Y-%m-%d") and (now.hour * 60 + now.minute) < 960:
        reason = "unsettled(<16:00 Beijing)"
        print("[unsettled] drop last bar %s of %s: %s" % (rows[-1][0], label, reason), flush=True)
        return rows[:-1], reason
    return rows, None


def is_rows_finite(rows):
    """行内数值是否全部有限（防 NaN/Inf 穿透——json 默认接受 NaN 字面量，
    且 NaN 的比较/布尔运算都为真，能骗过 rows>=1000 与排序检查）。"""
    for r in rows:
        for x in r[1:]:
            try:
                v = float(x)
            except (TypeError, ValueError):
                return False
            if v != v or v in (float("inf"), float("-inf")):
                return False
    return True


def atomic_write_json(path, obj):
    """tmp+rename 原子写：本地运行被中断不留截断的 JSON（CI 每次全新 clone，
    此风险主要在本地；成本为零故顺手加固）。"""
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False)
    os.replace(tmp, path)
