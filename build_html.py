# -*- coding: utf-8 -*-
"""组装自包含 HTML: template + echarts内联 + DATA + app.js -> industry-obos-dashboard.html

[2026-09-03] --sub 模式: template_sub.html + sub_app.js + data/sub_obos.json
  -> sub.html + sub_data.js (申万二级 109 个细分行业看板, 与主看板同构建契约)

[2026-09-03] --sub 追加 PARENTS: 31 个申万一级行业的关键字段, 供二级看板的"一级行业
  可展开行"使用。只抽关键字段(不含 score/forecast 等时间序列), 避免为此加载主看板
  2.6MB 全量数据。口径与主看板完全一致(同源 industry_obos.json), 杜绝"同一行业两个分数"。
"""
import json, os, io, sys

BASE = os.path.dirname(os.path.abspath(__file__))

# 一级行业行需要的字段(对应排名表 16 列中的可见列 + 状态判定阈值线)
PARENT_KEYS = ['code', 'name', 'cur_score', 'state', 'sig_label', 'divergence',
               'chg5', 'ret20', 'ret60', 'ret250', 'vol_ratio', 'vol_state',
               'rs_pct_now', 'above_ma200', 'fdr_q', 'sig',
               'ob_line', 'hot_line', 'cold_line', 'os_line']

def _parents_js(sub_data):
    """[2026-09-03] 从主看板 industry_obos.json 抽 31 个一级行业关键字段 -> PARENTS 数组
    只抽标量字段 + 推演末值, 不带时间序列, 追加进 sub_data.js 而非整包引入主看板数据。

    同步判据(降级而非硬失败): 一级与二级必须同日, 否则"电力设备 29.4 / 光伏设备 18.3"
    会是两个时点的数, 比不显示更误导 -> 此时返回空数组, 前端一级行退化为占位(指标列一律 "-")。
    不抛异常的原因: daily.yml 的 commit 步骤在最后, 此处失败会连带主看板也发不出去。
    """
    with io.open(os.path.join(BASE, "data", "industry_obos.json"), encoding="utf-8") as f:
        pdata = json.load(f)
    pa, sa = pdata.get("asof"), sub_data.get("asof")
    if pa != sa:
        print("::warning::一级/二级数据不同步(一级 asof=%s vs 二级 asof=%s)，"
              "本轮不注入 PARENTS，一级行业行退化为占位(指标列显示 -)" % (pa, sa))
        return "[]"
    out = []
    for x in pdata["industries"]:
        rec = {}
        for k in PARENT_KEYS:
            rec[k] = x.get(k)
        # 推演30日中位: 只取末值(表格列只需一个数), 不搬整个 forecast
        med = (x.get("forecast") or {}).get("median")
        rec["fc_end"] = med[-1] if isinstance(med, list) and med else None
        out.append(rec)
    if len(out) != 31:
        print("::warning::一级行业数异常(%d，期望 31)，本轮不注入 PARENTS" % len(out))
        return "[]"
    js = json.dumps(out, ensure_ascii=False).replace("</script>", "<\\/script>")
    assert "NaN" not in js and "Infinity" not in js, "non-finite number in PARENTS"
    return js


def main(sub_mode=False):
    tpl_file = "template_sub.html" if sub_mode else "template.html"
    app_file = "sub_app.js" if sub_mode else "app.js"
    json_file = "sub_obos.json" if sub_mode else "industry_obos.json"
    data_js_name = "sub_data.js" if sub_mode else "data.js"
    out_name = "sub.html" if sub_mode else "industry-obos-dashboard.html"

    with io.open(os.path.join(BASE, tpl_file), encoding="utf-8") as f:
        html = f.read()
    with io.open(os.path.join(BASE, app_file), encoding="utf-8") as f:
        app_src = f.read()
    with io.open(os.path.join(BASE, "data", json_file), encoding="utf-8") as f:
        data = json.load(f)

    # [2026-09-03] 剔除前端从未引用的时序数组 —— 纯占体积, 直接拖慢首屏。
    #   ma200 : 仅 compute.py 内部用于算 above_ma200(布尔标量, 已单独输出), 前端 0 处引用
    #           —— 占 19.9KB/行业, 且是 17 位浮点垃圾(922.1242000000001)
    #   vol   : 前端只用 vol_ratio / vol_state 两个标量, 原始成交量数组 0 处引用
    #           —— 占 10.3KB/行业(app.js 里那个 "vol" 只是 CSS 类名 c-vol)
    #   收益: 二级 sub_data.js 9.0MB -> 5.7MB, 主看板 data.js 2.6MB -> 1.7MB
    #   (Pages 侧会再 gzip 约 3.6:1, 线上传输量同比减少)
    DROP_KEYS = ('ma200', 'vol')
    for _x in data.get("industries", []):
        for _k in DROP_KEYS:
            _x.pop(_k, None)

    # 性能优化: 不再内联 echarts(≈1MB)/数据(≈2.6MB)，改独立文件外链，
    # 浏览器可缓存，首屏 HTML 从 ~3.7MB 降至 <50KB，二次访问秒开。
    assert os.path.exists(os.path.join(BASE, "echarts.min.js")), "echarts.min.js 缺失(外链所需)"
    data_js = json.dumps(data, ensure_ascii=False)
    data_js = data_js.replace("</script>", "<\\/script>")  # [C] 防 DATA 内合法字符串提前闭合 script 标签
    assert "NaN" not in data_js and "Infinity" not in data_js, "non-finite number in DATA"

    # 数据拆为独立外链(同样可缓存)，不再塞进 HTML
    payload = "var DATA = " + data_js + ";\n"
    if sub_mode:
        payload += "var PARENTS = " + _parents_js(data) + ";\n"
    data_js_path = os.path.join(BASE, data_js_name)
    with io.open(data_js_path, "w", encoding="utf-8") as f:
        f.write(payload)
    assert os.path.getsize(data_js_path) > 100000, data_js_name + " 写入异常(过小)"

    # [2026-09-03] echarts 不再作为首屏阻塞式外链。
    # 原来 1MB 图表库与数 MB 数据串行下载, 全部到齐才开始渲染, 期间整页白屏无提示,
    # 国内访问 Pages 时表现为"看板加载不出来数据"。现由 app 内 ensureEcharts() 动态注入,
    # 表格/摘要等纯 DOM 内容先出, 图表库后台拉取, 到位后补图; 图表库失败也不影响数据。
    html = html.replace("__ECHARTS__", "<!-- echarts 由 app 按需注入, 不阻塞首屏 -->")
    html = html.replace("__DATA__", '<script src="' + data_js_name + '"></script>')
    html = html.replace("__APPJS__", "<script>\n" + app_src + "\n</script>")
    assert "__ECHARTS__" not in html and "__DATA__" not in html and "__APPJS__" not in html
    assert "cdn.jsdelivr.net" not in html
    # echarts.min.js 字样由内联 app 源码(ensureEcharts)带入, 非外链标签
    assert "echarts.min.js" in html and data_js_name in html, "外链引用缺失"
    assert '<script src="echarts.min.js">' not in html, "echarts 仍为首屏阻塞外链"

    out = os.path.join(BASE, out_name)
    with io.open(out, "w", encoding="utf-8") as f:
        f.write(html)
    print("built:", out,
          "首屏HTML MB:", round(os.path.getsize(out) / 1048576, 2),
          "|", data_js_name, "MB:", round(os.path.getsize(data_js_path) / 1048576, 2))

if __name__ == "__main__":
    main(sub_mode=("--sub" in sys.argv))
