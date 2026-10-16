# -*- coding: utf-8 -*-
"""产物 → CI 提交清单 完备性自检（门禁 9, 2026-10-16）。

【为什么需要这道门禁】
daily.yml 刻意用**显式清单**提交产物（`git add index.html data.js ...`），而不是
`git add -A` —— 目的是别把 data/*.json 这类中间产物卷进仓库历史。代价是：
**新增产物文件必须手工同步进清单**，而这件事此前只写在注释里，没有任何机制保证它被执行。

它描述的失效模式恰恰是最难发现的一类（历史事故）：
  sub_series.js / series.js 都曾因为漏加清单，导致**首屏表格每天更新、详情曲线永远停在
  首次推送的那一天**——页面能开、数字看着正常、没有任何报错，只是「新数据」和「旧曲线」
  被摆在一起。这正是本站一贯要堵的「算完、格式正常、数字是错的」。

【本门禁覆盖什么】
  A  生产者线索：注册表里每个产物，其「由谁写出」的字面量必须仍在对应源码里。
     作用是**防腐化** —— 脚本改了输出路径而注册表没跟着改时，注册表会继续宣布
     「该产物已被提交」、完备性检查照样全绿。线索对不上就判红，逼人回来同步注册表。
     （一个悄悄失效的门禁，比没有门禁更糟。）
  B  双向完备性：daily.yml 所有 `git add` 参数之和 == 注册表中「必须提交」的产物集合。
     清单缺一个 -> 红；清单多出一个没注册的 -> 红（逼人把新产物显式登记）。
  C  清单卫生：不得出现通配符 / 变量展开（那等于退回 `git add -A`，本仓库明令禁止）；
     已声明「必须提交」的产物不得被 .gitignore 忽略（否则 `git add` 会报错退出）。
  D  接线自检：本门禁必须真的被 daily.yml 调用，且**排在 Freshness gate 之前**
     （挂 gate 之后会被周末/节假日/「数据已最新」一起 skip —— 门禁 8 的教训）。
  E  反向控制：人为删清单项 / 塞幽灵项 / 改坏线索 / 把已提交产物加进 .gitignore /
     把本门禁从 CI 摘掉 —— 五种破坏都必须被抓。缺了反向控制，
     一个「恒判绿」的摆设也能假装通过。

【边界】
纯 stdlib 静态解析：不读 data/、不联网、不 import numpy。因此可以排在 CI 最前面，
那个时刻 runner 只有系统 python3（setup-python / pip install 都还在后面）。

退出码：0 = 全部通过；1 = 有断言失败。
用法：python3 test_commit_manifest.py
"""
import fnmatch
import io
import os
import sys

BASE = os.path.dirname(os.path.abspath(__file__))
DAILY_REL = ".github/workflows/daily.yml"
WATCHDOG_REL = ".github/workflows/watchdog.yml"
DAILY = os.path.join(BASE, DAILY_REL)
WATCHDOG = os.path.join(BASE, WATCHDOG_REL)
GITIGNORE = os.path.join(BASE, ".gitignore")

# CI 里调用本门禁的那一行（接线自检用；改调用方式必须同步改这里）
CI_INVOCATION = "python3 test_commit_manifest.py"
# Freshness gate 的步骤名前缀（用于比较先后顺序）。
# 注意不要全文搜 "Freshness gate" 字样 —— 它在上方注释里出现过多次，会定位到错的锚点。
FRESH_STEP = "- name: Freshness gate"

# ---------------------------------------------------------------------------
# 注册表：全部构建 / 部署产物
#   committed=True  -> 必须出现在 daily.yml 的某个 `git add` 参数里
#   committed=False -> 必须写明理由（>=8 字符）
#   clues           -> [(相对路径, 必须存在于该文件的字面量), ...]
#                      空列表表示「管线不生成它」，此时 static_note 必填
# ---------------------------------------------------------------------------
PRODUCTS = [
    dict(path="index.html", committed=True, reason="", static_note="",
         clues=[(DAILY_REL, "cp industry-obos-dashboard.html index.html")]),

    dict(path="data.js", committed=True, reason="", static_note="",
         clues=[("build_html.py", '"data.js"')]),

    dict(path="series.js", committed=True, reason="", static_note="",
         clues=[("build_html.py", '"series.js"')]),

    dict(path="sub.html", committed=True, reason="", static_note="",
         clues=[("build_html.py", '"sub.html"')]),

    dict(path="sub_data.js", committed=True, reason="", static_note="",
         clues=[("build_html.py", '"sub_data.js"')]),

    dict(path="sub_series.js", committed=True, reason="", static_note="",
         clues=[("build_html.py", '"sub_series.js"')]),

    dict(path="last_build.txt", committed=True, reason="", static_note="",
         clues=[(DAILY_REL, "git add last_build.txt")]),

    dict(path="data/prediction_log.jsonl", committed=True, reason="", static_note="",
         clues=[("compute.py", '"prediction_log.jsonl"')]),

    dict(path="data/model_metrics_log.jsonl", committed=True, reason="", static_note="",
         clues=[("compute.py", '"model_metrics_log.jsonl"')]),

    # 手工入库的第三方库：管线不生成、也永不变化。留在清单里是为了「首次部署自洽」
    # （新克隆的仓库直接可用），删掉它也不会坏 —— 但会静默改变部署语义，故显式登记。
    dict(path="echarts.min.js", committed=True, reason="",
         static_note="手工入库的第三方图表库, 管线不生成; 清单里保留以保新克隆仓库可直出",
         clues=[]),

    # ── 以下为「有意不提交」的产物 ─────────────────────────────────────────
    dict(path="industry-obos-dashboard.html", committed=False,
         reason="中间产物: 由 Prepare Pages entry 步骤 cp 成 index.html; 提交它等于仓库里存两份同样的整页 HTML",
         static_note="",
         clues=[("build_html.py", '"industry-obos-dashboard.html"')]),

    dict(path="data/benchmark.json", committed=False,
         reason="入库历史快照(2026-08-27): CI 每日重算但不提交, 避免中间产物污染仓库历史; 读取方须自带新鲜度判据",
         static_note="",
         clues=[("fetch_benchmark.py", '"benchmark.json"')]),

    dict(path="data/industry_klines.json", committed=False,
         reason="入库历史快照(2026-08-27): 取数中间产物, 与 benchmark.json 同一处理口径",
         static_note="",
         clues=[("fetch_data.py", '"industry_klines.json"')]),

    dict(path="data/industry_obos.json", committed=False,
         reason="入库历史快照(2026-08-27): 主看板计算中间产物; 本地跑门禁前须先跑管线, 否则读到陈旧快照(已知陷阱)",
         static_note="",
         clues=[("compute.py", '"industry_obos.json"')]),

    dict(path="data/sub_klines.json", committed=False,
         reason="在 .gitignore 中: 二级取数中间产物(9MB 级), CI 每日自行生成, 从不入库",
         static_note="",
         clues=[("fetch_sub_data.py", '"sub_klines.json"')]),

    dict(path="data/sub_obos.json", committed=False,
         reason="在 .gitignore 中: 二级计算中间产物(9MB 级), 与 sub_klines.json 同",
         static_note="",
         clues=[("compute.py", '"sub_obos.json"')]),
]

N_CTRL = 5  # 反向控制用例数（E1~E5）


# ---------------------------------------------------------------------------
# 解析工具
# ---------------------------------------------------------------------------
def read_text(path):
    with io.open(path, encoding="utf-8") as f:
        return f.read()


def read_if_exists(path):
    return read_text(path) if os.path.exists(path) else ""


def parse_git_add(text):
    """抽出 workflow 里所有 `git add` 的文件参数。

    返回 (files, suspicious)：
      files      —— 可静态判定的显式路径
      suspicious —— 通配符 / 变量展开 / `-A` 这类「无法静态判定」的参数。
                    本仓库刻意全用显式清单，出现它们即说明有人改回了批量添加，
                    必须报出来（否则完备性检查会因为「清单变空」而误报成
                    「产物全缺」，把排查引向错误方向）。
    """
    files, suspicious = [], []
    for line in text.splitlines():
        s = line.strip()
        if not s.startswith("git add"):
            continue
        for tok in s[len("git add"):].split():
            if tok.startswith("-") or "$" in tok or "*" in tok or "?" in tok:
                suspicious.append(tok)
            else:
                files.append(tok)
    return files, suspicious


def parse_gitignore(text):
    pats = []
    for line in text.splitlines():
        s = line.strip()
        if s and not s.startswith("#"):
            pats.append(s)
    return pats


def ignored_by(path, pats):
    for p in pats:
        if p.endswith("/"):
            if path.startswith(p):
                return True
            continue
        if path == p:
            return True
        if fnmatch.fnmatch(path, p) or fnmatch.fnmatch(os.path.basename(path), p):
            return True
    return False


# ---------------------------------------------------------------------------
# 核心检查（可对任意 workflow 文本运行 —— 反向控制要拿它跑篡改版）
# ---------------------------------------------------------------------------
def run_checks(daily_text, watchdog_text, gitignore_text, products):
    problems = []

    def bad(msg):
        problems.append(msg)

    # ── A 生产者线索（防腐化）────────────────────────────────────────────
    texts = {DAILY_REL: daily_text, WATCHDOG_REL: watchdog_text}
    for pr in products:
        p = pr["path"]
        if not pr["clues"] and not pr["static_note"].strip():
            bad("A: 产物 %s 既没有生产者线索、也没有 static_note —— 注册表条目不完整" % p)
        for rel, needle in pr["clues"]:
            body = texts.get(rel)
            if body is None:
                body = read_if_exists(os.path.join(BASE, rel))
                if not body:
                    bad("A: 产物 %s 的线索文件不存在或为空: %s" % (p, rel))
                    continue
            if needle not in body:
                bad("A: 产物 %s 的线索 %r 已不在 %s 里 —— 输出路径可能被改过, "
                    "请同步更新 test_commit_manifest.py 的注册表" % (p, needle, rel))

    # ── B 双向完备性 ──────────────────────────────────────────────────────
    manifest, suspicious = parse_git_add(daily_text)
    got, want = set(manifest), {pr["path"] for pr in products if pr["committed"]}

    for tok in sorted(set(suspicious)):
        bad("C: daily.yml 出现无法静态判定的 git add 参数 %r —— 本仓库必须使用显式清单"
            "（通配符 / -A 会把 data/*.json 等中间产物卷进仓库历史）" % tok)

    for p in sorted(want - got):
        bad("B: 产物 %s 在注册表里声明「必须提交」, 但 daily.yml 的 git add 清单里没有它"
            " —— 这就是「表格每天更新、曲线永远停在旧版本」的成因" % p)
    for p in sorted(got - want):
        bad("B: daily.yml 提交了未注册的路径 %s —— 请登记进 test_commit_manifest.py, "
            "并写明「为什么必须提交」或「为什么不提交」" % p)

    for p in sorted(got):
        if not os.path.exists(os.path.join(BASE, p)):
            bad("B: 清单里的 %s 在仓库里不存在 —— git add 会直接失败" % p)

    # ── C 清单卫生 ────────────────────────────────────────────────────────
    pats = parse_gitignore(gitignore_text)
    for pr in products:
        p = pr["path"]
        if pr["committed"]:
            if ignored_by(p, pats):
                bad("C: %s 声明为「必须提交」, 却被 .gitignore 忽略 —— git add 必然失败" % p)
        elif len(pr["reason"].strip()) < 8:
            bad("C: 未提交产物 %s 缺少可读理由（要写清「为什么不提交」）" % p)

    # ── D 接线自检 ────────────────────────────────────────────────────────
    lines = daily_text.splitlines()
    idx_gate = next((i for i, l in enumerate(lines) if l.strip().startswith(FRESH_STEP)), None)
    idx_self = next((i for i, l in enumerate(lines) if CI_INVOCATION in l), None)
    if idx_self is None:
        bad("D: daily.yml 里找不到 `%s` —— 本门禁没有被 CI 调用（等于不存在）" % CI_INVOCATION)
    if idx_gate is None:
        bad("D: daily.yml 里找不到 Freshness gate 步骤, 无法比较先后顺序")
    if idx_self is not None and idx_gate is not None and idx_self > idx_gate:
        bad("D: 本门禁排在 Freshness gate 之后 —— 周末 / 节假日 / 数据已最新时会被一起 skip, "
            "而恰恰是这些场景最需要它（门禁 8 的同款教训）")

    return problems


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------
def main():
    daily = read_text(DAILY)
    watchdog = read_if_exists(WATCHDOG)
    gi = read_if_exists(GITIGNORE)

    print("=" * 78)
    print("门禁 9 · 产物提交清单完备性自检")
    print("=" * 78)

    manifest, _ = parse_git_add(daily)
    print("[注册表] %d 个产物（必须提交 %d / 有意不提交 %d）"
          % (len(PRODUCTS),
             sum(1 for p in PRODUCTS if p["committed"]),
             sum(1 for p in PRODUCTS if not p["committed"])))
    for pr in PRODUCTS:
        print("   %-34s %s" % (pr["path"], "提交" if pr["committed"] else "不提交"))
    print("[daily.yml git add 清单] %d 项: %s" % (len(manifest), " ".join(manifest)))

    problems = run_checks(daily, watchdog, gi, PRODUCTS)

    # ── E 反向控制：五种破坏都必须被抓 ────────────────────────────────────
    print("-" * 78)
    print("反向控制（人为破坏 -> 必须判红）")

    def mutate_daily(old, new):
        assert old in daily, "反向控制的前置条件不成立: daily.yml 里找不到 %r" % old
        return daily.replace(old, new, 1)

    def expect(label, d_text, g_text, must_contain):
        pr = run_checks(d_text, watchdog, g_text, PRODUCTS)
        hit = [m for m in pr if must_contain in m]
        if hit:
            print("   ✅ %s -> 已抓红: %s" % (label, hit[0][:86]))
            return True
        print("   ❌ %s -> **没抓到**! 反向控制失效, 本门禁可能是恒绿的摆设" % label)
        if pr:
            print("        （实际报出的是: %s）" % pr[0][:86])
        return False

    ctrl = [
        # E1 清单缺项
        expect("E1 从清单删掉 series.js",
               mutate_daily("sub_series.js series.js", "sub_series.js"),
               gi, "series.js 在注册表里声明"),

        # E2 幽灵清单项
        expect("E2 往清单塞一个未注册的 docs/ghost.js",
               mutate_daily("git add index.html", "git add index.html docs/ghost.js"),
               gi, "ghost.js"),

        # E4 已提交产物被 .gitignore 忽略
        expect("E4 把 data.js 加进 .gitignore",
               daily, gi + "\ndata.js\n", "data.js 声明为"),

        # E5 本门禁从 CI 摘掉
        expect("E5 从 daily.yml 删掉本门禁的调用",
               mutate_daily("        run: " + CI_INVOCATION + "\n", ""),
               gi, "没有被 CI 调用"),
    ]

    # E3 线索改坏（注册表腐化）—— 篡改的是注册表而非 workflow，故单独跑
    rotted = [dict(x) for x in PRODUCTS]
    for pr in rotted:
        if pr["path"] == "data.js":
            pr["clues"] = [("build_html.py", '"DATA_JS_RENAMED.js"')]
    pr3 = run_checks(daily, watchdog, gi, rotted)
    if any("DATA_JS_RENAMED" in m for m in pr3):
        print("   ✅ E3 把 data.js 的生产者线索改成不存在的字面量 -> 已抓红")
        ctrl.append(True)
    else:
        print("   ❌ E3 线索腐化未被抓 -> 注册表防腐化失效")
        ctrl.append(False)

    n_ctrl_ok = sum(1 for c in ctrl if c)
    print("-" * 78)
    for m in problems:
        print("❌ " + m)

    print("-" * 78)
    if problems or n_ctrl_ok != N_CTRL:
        print("RESULT: FAIL —— 注册表问题 %d 项 / 反向控制通过 %d/%d"
              % (len(problems), n_ctrl_ok, N_CTRL))
        return 1
    print("RESULT: PASS —— 产物注册表与 CI 提交清单双向一致；%d 项反向控制全部抓红" % N_CTRL)
    return 0


if __name__ == "__main__":
    sys.exit(main())
