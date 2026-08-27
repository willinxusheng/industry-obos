# -*- coding: utf-8 -*-
"""组装自包含 HTML: template + echarts内联 + DATA + app.js -> industry-obos-dashboard.html"""
import json, os, io

BASE = os.path.dirname(os.path.abspath(__file__))

def main():
    with io.open(os.path.join(BASE, "template.html"), encoding="utf-8") as f:
        html = f.read()
    with io.open(os.path.join(BASE, "app.js"), encoding="utf-8") as f:
        app_src = f.read()
    with io.open(os.path.join(BASE, "data", "industry_obos.json"), encoding="utf-8") as f:
        data = json.load(f)

    # 性能优化: 不再内联 echarts(≈1MB)/数据(≈2.6MB)，改独立文件外链，
    # 浏览器可缓存，首屏 HTML 从 ~3.7MB 降至 <50KB，二次访问秒开。
    assert os.path.exists(os.path.join(BASE, "echarts.min.js")), "echarts.min.js 缺失(外链所需)"
    data_js = json.dumps(data, ensure_ascii=False)
    data_js = data_js.replace("</script>", "<\\/script>")  # [C] 防 DATA 内合法字符串提前闭合 script 标签
    assert "NaN" not in data_js and "Infinity" not in data_js, "non-finite number in DATA"

    # 数据拆为独立 data.js 外链(同样可缓存)，不再塞进 HTML
    data_js_path = os.path.join(BASE, "data.js")
    with io.open(data_js_path, "w", encoding="utf-8") as f:
        f.write("var DATA = " + data_js + ";\n")
    assert os.path.getsize(data_js_path) > 100000, "data.js 写入异常(过小)"

    html = html.replace("__ECHARTS__", '<script src="echarts.min.js"></script>')
    html = html.replace("__DATA__", '<script src="data.js"></script>')
    html = html.replace("__APPJS__", "<script>\n" + app_src + "\n</script>")
    assert "__ECHARTS__" not in html and "__DATA__" not in html and "__APPJS__" not in html
    assert "cdn.jsdelivr.net" not in html
    assert "echarts.min.js" in html and "data.js" in html, "外链引用缺失"

    out = os.path.join(BASE, "industry-obos-dashboard.html")
    with io.open(out, "w", encoding="utf-8") as f:
        f.write(html)
    print("built:", out,
          "首屏HTML MB:", round(os.path.getsize(out) / 1048576, 2),
          "| data.js MB:", round(os.path.getsize(data_js_path) / 1048576, 2))

if __name__ == "__main__":
    main()
