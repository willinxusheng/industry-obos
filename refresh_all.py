# -*- coding: utf-8 -*-
"""一键刷新 A股全行业超买超卖看板（本地手工跑，CI 走 .github/workflows/daily.yml）。

链路（与 daily.yml 的 update job 逐步对齐，顺序不可换）：
    取数:     fetch_benchmark.py -> fetch_data.py -> fetch_sub_data.py
    计算:     compute.py -> compute.py --sub        (内置 quality_gate, FAIL 自行中断)
    复核:     audit_predictions.py -> test_audit_logic.py
              （必须紧跟 compute：此刻 data/ 才是新鲜的，详见 AUDIT_STEPS 注释）
    前端门禁: test_render.js -> test_render.js --sub -> gate_edge_test.js
    组装:     build_html.py -> build_html.py --sub -> cp -> index.html
    产物自检

[2026-09-05] 重写说明：旧版本只跑 3 个 py 步骤 + 一道门禁，且产物指向
  industry-obos-dashboard.html（那只是 build_html.py 的中间产物，已被 .gitignore，
  真正的 Pages 入口是 index.html）。照旧版跑会得到"主看板看着更新了、二级看板没动、
  门禁只跑了三分之一"的假象 —— 比没有这个脚本更危险，故按真实链路重写。

⚠️ 数据纪律：data/ 由 CI 独家写盘。本地跑完请 `git checkout -- data/` 还原，
   不要把本地产物提交上去（data/prediction_log.jsonl 例外：它是正式审计资产，
   本地跑会被污染，用 `git restore` 而非 rm 恢复 CI 版本）。

用法: python refresh_all.py
"""
import os
import shutil
import subprocess
import sys

BASE = os.path.dirname(os.path.abspath(__file__))
# 顺序即依赖：基准是 compute 的输入，两个 fetch 必须先于 compute；
# 门禁必须先在源 JSON 上跑过，再组装产物（否则破损页面会被发布出去）。
PY_STEPS = [
    ["fetch_benchmark.py"],
    ["fetch_data.py"],
    ["fetch_sub_data.py"],
    ["compute.py"],
    ["compute.py", "--sub"],
]
# [2026-09-05] 复核必须紧跟 compute.py：audit_predictions.py 拿 data/industry_obos.json
#   的"后来实际分数"去对 prediction_log.jsonl 里"当时的预测快照"，而仓库里 data/*.json
#   是历史快照、CI 从不回写 —— 只有此刻（刚 fetch+compute 完）data/ 才是新鲜的。
#   顺序挪动或漏跑，复核就永远落在"日志比数据新"的分支里，出不了任何结论。
AUDIT_STEPS = [
    ["audit_predictions.py"],
    ["test_audit_logic.py"],     # 统计口径的受控反演自检（不读真实数据）
]
NODE_STEPS = [
    ["test_render.js"],
    ["test_render.js", "--sub"],
    ["gate_edge_test.js"],
]
PY_BUILD = [
    ["build_html.py"],
    ["build_html.py", "--sub"],
]
# 必须存在的产物：缺任何一个，线上就有一块是空的或旧的
ARTIFACTS = ["index.html", "sub.html", "data.js", "sub_data.js",
             "series.js", "sub_series.js"]
# 首屏 HTML 合理体积区间（拆外链后应很小；过大=又把数据内联回去了，过小=空壳）
HTML_MIN_MB, HTML_MAX_MB = 0.005, 2.0


def run(cmd, label):
    print("=== running %s ===" % label, flush=True)
    r = subprocess.run(cmd, cwd=BASE)
    if r.returncode != 0:
        raise SystemExit("FAILED: %s (exit %d)" % (label, r.returncode))
    print("OK: %s" % label, flush=True)


def find_node():
    """优先用受管 node（跨平台探测），回退到 PATH。

    [2026-09-05] 原实现硬编码版本号 22.22.2，而实际安装目录是 22.22.2-2（带构建后缀）——
      两个候选路径在当前机器上都不存在，"优先受管 node" 这段逻辑从未真正生效过，
      全靠 PATH 里恰好有 node 兜底。换个没有 PATH 注入的终端就会直接 SystemExit，
      而受管 node 明明就在磁盘上：防御写了却从不触发，比不写更误导。
      改为按目录探测并取版本号最高者，以后升级 node 无需再改代码。
    """
    import glob
    import re
    root = os.path.expanduser("~/.workbuddy/binaries/node/versions")
    cands = (glob.glob(os.path.join(root, "*", "bin", "node")) +
             glob.glob(os.path.join(root, "*", "node.exe")))
    best, best_key = None, None
    for p in cands:
        if not os.path.isfile(p):
            continue
        # versions/<ver>/bin/node 或 versions/<ver>/node.exe -> 取 <ver>
        ver = os.path.basename(os.path.dirname(os.path.dirname(p)))
        key = [int(x) for x in re.findall(r"\d+", ver)]
        if key and (best_key is None or key > best_key):
            best, best_key = p, key
    if best:
        print("使用受管 node: %s" % best, flush=True)
    return best or shutil.which("node")


def verify_output():
    """产物自检：占位符、外链、体积、以及两个看板的产物是否齐全。

    只查 index.html 的占位符是不够的 —— 两个看板各有一套产物，
    少提交任何一个都会出现"表格是新的、曲线是旧的"这种跨交易日混搭。
    """
    for name in ARTIFACTS:
        p = os.path.join(BASE, name)
        if not os.path.exists(p):
            raise SystemExit("FAILED: 产物缺失 %s" % name)

    for name in ("index.html", "sub.html"):
        with open(os.path.join(BASE, name), encoding="utf-8") as f:
            h = f.read()
        left = [p for p in ("__DATA__", "__APPJS__", "__ECHARTS__") if p in h]
        if left:
            raise SystemExit("FAILED: %s 模板占位符未替换 %s" % (name, left))
        if "<script" not in h:
            raise SystemExit("FAILED: %s 缺少 script 段" % name)
        if '<script src="echarts.min.js">' in h:
            raise SystemExit("FAILED: %s 的 echarts 仍是首屏阻塞外链" % name)
        djs = "sub_data.js" if name == "sub.html" else "data.js"
        if djs not in h:
            raise SystemExit("FAILED: %s 缺少数据外链 %s" % (name, djs))
        mb = len(h.encode("utf-8")) / 1048576.0
        if mb < HTML_MIN_MB or mb > HTML_MAX_MB:
            raise SystemExit("FAILED: %s 首屏体积异常 %.2f MB" % (name, mb))
        print("OK: %-11s 首屏 %.2f MB" % (name, mb), flush=True)

    # 首屏数据必须只剩标量（时序已剥离，否则首屏又要下完几 MB 才开始渲染）
    for name, lo, hi in (("data.js", 30000, 300000), ("sub_data.js", 50000, 700000)):
        n = os.path.getsize(os.path.join(BASE, name))
        if n < lo or n > hi:
            raise SystemExit("FAILED: %s 体积 %d 不在 [%d,%d]，时序可能未剥离干净" % (name, n, lo, hi))
        print("OK: %-11s %6.2f MB（首屏标量）" % (name, n / 1048576.0), flush=True)

    # 时序文件必须与首屏同日：跨日混搭比整页失败更误导
    import re
    with open(os.path.join(BASE, "data.js"), encoding="utf-8") as f:
        m = re.search(r'"asof"\s*:\s*"([0-9-]+)"', f.read())
    asof = m.group(1) if m else ""
    if not asof:
        raise SystemExit("FAILED: data.js 里读不到 asof")
    for name, var in (("series.js", "SERIES_ASOF"), ("sub_series.js", "SUB_SERIES_ASOF")):
        with open(os.path.join(BASE, name), encoding="utf-8") as f:
            head = f.read(200)
        m = re.search(re.escape(var) + r'\s*=\s*"([0-9-]+)"', head)
        got = m.group(1) if m else ""
        if got != asof:
            raise SystemExit("FAILED: %s 的 %s=%s 与首屏 asof=%s 不一致（跨交易日混搭）"
                             % (name, var, got, asof))
        print("OK: %-11s %s=%s 与首屏一致" % (name, var, got), flush=True)

    print("OK: 产物自检通过（占位符已替换、外链就绪、时序与首屏同日）", flush=True)


def main():
    for args in PY_STEPS:
        run([sys.executable] + [os.path.join(BASE, args[0])] + args[1:], " ".join(args))

    # 必须在 compute 之后、且 data/ 仍是本次结果时执行（见 AUDIT_STEPS 注释）
    for args in AUDIT_STEPS:
        run([sys.executable] + [os.path.join(BASE, args[0])] + args[1:], " ".join(args))

    node = find_node()
    if not node:
        # 不静默放过：门禁跑不了 = 破损页面无法被拦截，那这份产物不该被信任。
        # 确实只想刷数据、不产物时，显式设 SKIP_NODE_GATES=1 绕开。
        if os.environ.get("SKIP_NODE_GATES") == "1":
            print("WARN: 未找到 node，按 SKIP_NODE_GATES=1 跳过前端门禁（产物未经渲染校验）",
                  flush=True)
        else:
            raise SystemExit("FAILED: 未找到 node，前端门禁无法执行。"
                             "若只想刷新数据，请显式设 SKIP_NODE_GATES=1 以确认你接受这个风险。")
    else:
        for args in NODE_STEPS:
            run([node, os.path.join(BASE, args[0])] + args[1:], " ".join(args))

    for args in PY_BUILD:
        run([sys.executable] + [os.path.join(BASE, args[0])] + args[1:], " ".join(args))

    # build_html.py 主模式输出的是中间产物名，Pages 入口固定为 index.html
    shutil.copyfile(os.path.join(BASE, "industry-obos-dashboard.html"),
                    os.path.join(BASE, "index.html"))
    verify_output()
    print("REFRESH DONE ->", os.path.join(BASE, "index.html"), "+ sub.html", flush=True)


if __name__ == "__main__":
    main()
