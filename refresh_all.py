# -*- coding: utf-8 -*-
"""一键刷新 A股全行业超买超卖看板（每日定时调用）。

链路: 拉取行业日K -> 拉取沪深300基准 -> 计算指标与推演(含数据质量门禁)
      -> 前端冒烟测试(门禁) -> 组装HTML -> 产物自检

任一环节失败即中断, 绝不产出破损或脏数据页面:
  * compute.py 内置 quality_gate, status=FAIL 时自行 SystemExit;
  * test_render.js 校验 JSON 契约 + 在 DOM stub 下实跑 app.js, 捕获运行时错误、
    NaN/undefined 泄漏、PIT 阈值与状态口径不一致、覆盖率未校准等问题;
  * 最后校验 HTML 产物大小与占位符是否全部替换。

用法: python refresh_all.py
"""
import os
import shutil
import subprocess
import sys

BASE = os.path.dirname(os.path.abspath(__file__))
PY_STEPS = ["fetch_data.py", "fetch_benchmark.py", "compute.py"]
OUT = os.path.join(BASE, "industry-obos-dashboard.html")


def run(cmd, label):
    print("=== running %s ===" % label, flush=True)
    r = subprocess.run(cmd, cwd=BASE)
    if r.returncode != 0:
        raise SystemExit("FAILED: %s (exit %d)" % (label, r.returncode))
    print("OK: %s" % label, flush=True)


def find_node():
    """优先用受管 node, 回退到 PATH; 找不到则跳过冒烟测试并显式告警。"""
    managed = os.path.expanduser(
        r"~\.workbuddy\binaries\node\versions\22.22.2\node.exe")
    if os.path.exists(managed):
        return managed
    return shutil.which("node")


def verify_output():
    if not os.path.exists(OUT):
        raise SystemExit("FAILED: 产物不存在 %s" % OUT)
    with open(OUT, encoding="utf-8") as f:
        h = f.read()
    mb = len(h.encode("utf-8")) / 1048576.0
    left = [p for p in ("__DATA__", "__APPJS__", "__ECHARTS__") if p in h]
    if left:
        raise SystemExit("FAILED: 模板占位符未替换 %s" % left)
    if mb < 1.0:
        raise SystemExit("FAILED: 产物体积异常 %.2f MB (疑似 echarts/数据未内联)" % mb)
    if h.count("<script") < 3:
        raise SystemExit("FAILED: script 段数不足, 内联可能失败")
    print("OK: 产物自检 %.2f MB, 占位符全部替换" % mb, flush=True)


def main():
    for s in PY_STEPS:
        run([sys.executable, os.path.join(BASE, s)], s)

    node = find_node()
    if node:
        run([node, os.path.join(BASE, "test_render.js")], "test_render.js (前端门禁)")
    else:
        print("WARN: 未找到 node, 跳过前端冒烟测试(建议修复, 否则破损页面无法被拦截)",
              flush=True)

    run([sys.executable, os.path.join(BASE, "build_html.py")], "build_html.py")
    verify_output()
    print("REFRESH DONE ->", OUT, flush=True)


if __name__ == "__main__":
    main()
