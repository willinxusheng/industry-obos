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
    """优先用受管 node（跨平台探测），回退到 PATH；找不到则跳过冒烟测试并显式告警。"""
    managed_candidates = [
        os.path.expanduser(r"~\.workbuddy\binaries\node\versions\22.22.2\node.exe"),  # Windows
        os.path.expanduser("~/.workbuddy/binaries/node/versions/22.22.2/bin/node"),   # macOS/Linux
    ]
    for p in managed_candidates:
        if os.path.exists(p):
            return p
    return shutil.which("node")


def verify_output():
    if not os.path.exists(OUT):
        raise SystemExit("FAILED: 产物不存在 %s" % OUT)
    with open(OUT, encoding="utf-8") as f:
        h = f.read()
    left = [p for p in ("__DATA__", "__APPJS__", "__ECHARTS__") if p in h]
    if left:
        raise SystemExit("FAILED: 模板占位符未替换 %s" % left)
    # 拆分为外链后: 首屏 HTML 应很小(含外链引用)，且 data.js 独立生成
    if "echarts.min.js" not in h or "data.js" not in h:
        raise SystemExit("FAILED: 外链引用缺失 (echarts.min.js / data.js)")
    if "<script" not in h:
        raise SystemExit("FAILED: 缺少 script 段")
    mb = len(h.encode("utf-8")) / 1048576.0
    # 拆外链后首屏 HTML 应远小于原 3.7MB (>5KB 防空壳, <2MB 防又内联回去)
    if mb < 0.005 or mb > 2.0:
        raise SystemExit("FAILED: 首屏 HTML 体积异常 %.2f MB" % mb)
    djs = os.path.join(BASE, "data.js")
    if not os.path.exists(djs):
        raise SystemExit("FAILED: data.js 未生成")
    dmb = os.path.getsize(djs) / 1048576.0
    if dmb < 1.0:
        raise SystemExit("FAILED: data.js 体积异常 %.2f MB (数据未写出)" % dmb)
    print("OK: 产物自检 首屏HTML=%.2f MB, 外链(echarts.min.js+data.js=%.2f MB), 占位符全部替换"
          % (mb, dmb), flush=True)


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
