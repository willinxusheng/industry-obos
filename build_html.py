# -*- coding: utf-8 -*-
"""组装自包含 HTML: template + echarts内联 + DATA + app.js -> industry-obos-dashboard.html"""
import json, os, io

BASE = os.path.dirname(os.path.abspath(__file__))

def main():
    with io.open(os.path.join(BASE, "template.html"), encoding="utf-8") as f:
        html = f.read()
    with io.open(os.path.join(BASE, "echarts.min.js"), encoding="utf-8") as f:
        echarts_src = f.read()
    with io.open(os.path.join(BASE, "app.js"), encoding="utf-8") as f:
        app_src = f.read()
    with io.open(os.path.join(BASE, "data", "industry_obos.json"), encoding="utf-8") as f:
        data = json.load(f)

    assert len(echarts_src) > 100000, "echarts source missing or truncated"
    data_js = json.dumps(data, ensure_ascii=False)
    assert "NaN" not in data_js and "Infinity" not in data_js, "non-finite number in DATA"

    html = html.replace("__ECHARTS__", "<script>\n" + echarts_src + "\n</script>")
    html = html.replace("__DATA__", "<script>\nvar DATA = " + data_js + ";\n</script>")
    html = html.replace("__APPJS__", "<script>\n" + app_src + "\n</script>")
    assert "__ECHARTS__" not in html and "__DATA__" not in html and "__APPJS__" not in html
    assert "cdn.jsdelivr.net" not in html

    out = os.path.join(BASE, "industry-obos-dashboard.html")
    with io.open(out, "w", encoding="utf-8") as f:
        f.write(html)
    print("built:", out, "size MB:", round(os.path.getsize(out) / 1048576, 2))

if __name__ == "__main__":
    main()
