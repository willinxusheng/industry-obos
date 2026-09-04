#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""反假绿验证（第七道门禁，挂在 CI dom-gates job 里）。

故意把 app.js / sub_app.js 改坏，跑 test_dom_noseries.js，它必须在
「404」与「挂起」两个场景里都判红；少一个场景判红就说明那条断言是摆设。

坏法 A（首屏退化）：把 fcNote 从首屏挪回"等时序加载完"才渲染
       —— 这正是当初踩过的坑（首屏只剩骨架，白屏等时序），两个场景都该红。
坏法 B（失败不提示）：把 ensureSeries 的 onerror 里的提示删掉
       —— 404 场景应红在"未收口到终态提示"；挂起场景不应红
          （请求还在路上，停在"加载中"是诚实的，不该误报）。

每个坏法都带锚点断言：补丁没打上就自判失败，绝不允许"改了个寂寞还报通过"。

用法（本地与 CI 同一条命令）：
    python3 test_antifake_noseries.py
node 与 jsdom 的定位：优先 OBO_NODE 环境变量 -> managed node 路径(本机) ->
PATH 里的 node(CI)；NODE_PATH 只在本地 managed workspace 存在时注入，
CI 上 jsdom 已 npm install 到仓库根，node 自己能找到。
"""
import io
import os
import re
import shutil
import subprocess
import sys

REPO = os.path.dirname(os.path.abspath(__file__))
GATE = 'test_dom_noseries.js'
FC_ANCHOR = "    renderFcNote(x);\n"
NOTE_LINE = ("      seriesNote('详情曲线数据加载失败（网络问题）。<br>"
             "上方表格数据不受影响，可刷新或稍后重试。');\n")


def find_node():
    cands = [
        os.environ.get('OBO_NODE'),
        '/Users/willinxusheng/.workbuddy/binaries/node/versions/22.22.2-2/bin/node',
        shutil.which('node'),
    ]
    for c in cands:
        if c and os.path.exists(c):
            return c
    return 'node'   # 都不存在就赌 PATH（CI 上一定会先被 which 命中）


def build_env():
    env = dict(os.environ)
    ws = '/Users/willinxusheng/.workbuddy/binaries/node/workspace/node_modules'
    if os.path.isdir(ws):   # 本机隔离环境；CI 上该目录不存在，保持原样
        env['NODE_PATH'] = ws + ((':' + env['NODE_PATH']) if env.get('NODE_PATH') else '')
    return env


NODE = find_node()
ENV = build_env()


def run_gate():
    p = subprocess.run([NODE, GATE], cwd=REPO, env=ENV,
                       capture_output=True, text=True)
    return p.returncode, p.stdout + p.stderr


def split_scenes(out):
    """切成 {(看板, 场景): [FAIL 行]}。场景标题形如
    '=== 主看板：时序 404（series.js 不存在）===' 或 '=== 二级看板：时序挂起（...）==='。"""
    scenes = {}
    cur = None
    for line in out.splitlines():
        m = re.match(r'=== (.+?)：时序 (404|挂起)', line)
        if m:
            cur = (m.group(1).split('（')[0].strip(), m.group(2))
            scenes[cur] = []
            continue
        if cur and 'FAIL' in line and 'NO-SERIES VERIFY' not in line:
            # 末尾那句 "NO-SERIES VERIFY FAILED：N 项" 是总结，不是某个场景的
            # 断言失败，计进来会让每个场景都看起来"红过"，等于自欺欺人。
            scenes[cur].append(line.strip())
    return scenes


def patch(fname, pairs):
    """pairs = [(old, new), ...]，全部必须命中，否则抛错（已还原）。"""
    src = os.path.join(REPO, fname)
    bak = os.path.join(REPO, '.rb_' + fname + '.bak')
    shutil.copyfile(src, bak)
    s = io.open(src, encoding='utf-8').read()
    try:
        for old, new in pairs:
            if old not in s:
                raise AssertionError('%s 锚点未命中: %r' % (fname, old[:60]))
            s = s.replace(old, new, 1)
    except BaseException:
        shutil.copyfile(bak, src)
        raise
    io.open(src, 'w', encoding='utf-8').write(s)
    return bak


def restore(fname, bak):
    """从备份还原源文件。还原本身失败要抛错（那是必须处理的）；
    备份文件删不掉只告警 —— 还原已成功，别让删除失败掩盖结果。"""
    shutil.copyfile(bak, os.path.join(REPO, fname))
    try:
        os.remove(bak)
    except OSError:
        print('⚠️ 备份文件 %s 删除失败（不影响还原结果）' % bak)


def main():
    results = []

    # ---------- 坏法 A：主看板 + 二级看板（fcNote 挪回图表回调） ----------
    for fname, board in [('app.js', '主看板'), ('sub_app.js', '二级看板')]:
        src = os.path.join(REPO, fname)
        s = io.open(src, encoding='utf-8').read()
        if FC_ANCHOR not in s:
            print('❌ %s 找不到首屏 renderFcNote 调用，坏法 A 无法植入' % fname)
            results.append(False)
            continue
        m = re.search(r'( *)if \(curCode !== code\) return;', s)
        if not m:
            print('❌ %s 找不到 ensureSeries 回调注入点' % fname)
            results.append(False)
            continue
        bak = patch(fname, [
            (FC_ANCHOR, '    /* BROKEN: fcNote 挪回回调 */\n'),
            (m.group(0), m.group(1) + 'renderFcNote(x);\n' + m.group(0)),
        ])
        try:
            rc, out = run_gate()
        finally:
            restore(fname, bak)
        sc = split_scenes(out)
        got = {k[1] for k, v in sc.items() if k[0] == board and v}
        ok = (rc != 0) and got == {'404', '挂起'}
        results.append(ok)
        print('坏法A %-10s 退出码=%d  判红场景=%s  -> %s'
              % (board, rc, sorted(got) or '无',
                 '✅ 两个场景都抓到' if ok else '❌ 漏了（假绿！）'))
        for k, v in sc.items():
            if k[0] == board and v:
                print('      [%s] %d 项 FAIL，首条：%s' % (k[1], len(v), v[0][:80]))

    # ---------- 坏法 B：onerror 不写提示 ----------
    for fname, board in [('app.js', '主看板'), ('sub_app.js', '二级看板')]:
        src = os.path.join(REPO, fname)
        s = io.open(src, encoding='utf-8').read()
        if NOTE_LINE not in s:
            print('❌ %s 找不到 onerror 提示行，坏法 B 无法植入' % fname)
            results.append(False)
            continue
        bak = patch(fname, [(NOTE_LINE, '      /* BROKEN: 失败也不提示 */\n')])
        try:
            rc, out = run_gate()
        finally:
            restore(fname, bak)
        sc = split_scenes(out)
        got = {k[1] for k, v in sc.items() if k[0] == board and v}
        # 期望：404 红（未收口到终态），挂起绿（还在路上，诚实，不该误报）
        ok = (rc != 0) and got == {'404'}
        results.append(ok)
        print('坏法B %-10s 退出码=%d  判红场景=%s  -> %s'
              % (board, rc, sorted(got) or '无',
                 '✅ 404 红 / 挂起不误报' if ok else '❌ 不符合预期（%s）' % (sorted(got) or '无')))
        for k, v in sc.items():
            if k[0] == board and v:
                print('      [%s] %d 项 FAIL，首条：%s' % (k[1], len(v), v[0][:80]))

    # 收尾自检：工作区必须还原干净（不允许留下 .rb_*.bak）
    left = [f for f in os.listdir(REPO) if f.startswith('.rb_') and f.endswith('.bak')]
    if left:
        print('❌ 工作区残留备份文件：%s（还原逻辑有 bug，不能提交）' % left)
        return 1

    # 终检：两个源码文件必须与 git HEAD 一致（补丁全部还原）
    dirty = subprocess.run(
        ['git', '-C', REPO, 'status', '--porcelain', '--', 'app.js', 'sub_app.js'],
        capture_output=True, text=True).stdout.strip()
    if dirty:
        print('❌ 源码未还原干净，git 状态：%r —— 门禁自身就是脏的，判失败' % dirty)
        return 1

    print()
    print('总判定：', '✅ 全部抓到（门禁有效）' if all(results) else '❌ 存在假绿，需继续加固')
    return 0 if all(results) else 1


if __name__ == '__main__':
    sys.exit(main())
