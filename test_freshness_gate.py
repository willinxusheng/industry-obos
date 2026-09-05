#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""验证 daily.yml 里 Freshness gate 的「push 命中前端源码 -> 强制重建产物」判定。

为什么需要它：
  Freshness gate 原本只判「数据是否最新」，不管源码变没变，导致改了前端代码却要等
  下一个有新交易日的日子才上线。补的这段源码命中判定是纯 shell + git，本地能真实跑，
  但它是写在 YAML 块标量里的——缩进被吃掉、引号转义、正则边界，任何一处笔误都要等
  CI 真跑一次才暴露，反馈周期以天计。

反假绿设计（两条都要）：
  ① 不复制一份 shell 代码来测（那样测的是副本，daily.yml 改了测试照样绿），
     而是 yaml 解析出 fresh step 的 run 块，正则抠出真实的 SRC_HIT 段喂给真实 bash。
     若哪天结构变了抠不到东西，本脚本直接判红。
  ② 用例不硬编码 sha（那会随历史推移变成测副本），而是遍历 git 历史，
     对每个 commit 独立算出「真实是否改动了前端源码」，再与门禁判定比对。

用法：python3 test_freshness_gate.py
依赖：pyyaml（GitHub runner 自带；本地没有则 SKIP，不阻断）
"""
import io
import os
import re
import subprocess
import sys

BASE = os.path.dirname(os.path.abspath(__file__))
YML = os.path.join(BASE, '.github/workflows/daily.yml')

try:
    import yaml
except ImportError:
    # 本地没装 pyyaml 时跳过是方便；但 CI 上跳过 = 门禁形同虚设还显示绿（假绿），
    # 必须显式判红让问题可见。故由 daily.yml 传 REQUIRE_PYYAML=1 区分两种环境。
    if os.environ.get('REQUIRE_PYYAML') == '1':
        print('FAIL: CI 环境下 pyyaml 不可用，本门禁无法执行 —— 拒绝静默跳过')
        sys.exit(1)
    print('SKIP: pyyaml 未安装')
    sys.exit(0)

# 改动这些文件意味着产物必须重建（与 daily.yml 里的 grep 清单保持一致，逐字对应）。
# [2026-09-05] 清单从"前端源码"扩到"计算/取数层 + 本 job 内的门禁"：
#   只列前端时，改了 compute.py / obos_core.py / fetch_*.py 而数据又已是最新，
#   gate 判 skip，算法改动要卡到下一个有新交易日的日子才上线（周末改算法得等好几天）。
#   test_render.js / gate_edge_test.js 挂在同一 job 内，skip 时它们根本不跑，
#   改了断言却没人执行，等同门禁失效。（test_dom_*.js / test_antifake 属独立 job，不列入。）
# ⚠️ 改 daily.yml 的 grep 清单必须同步改这里，否则本门禁立刻判红 —— 这正是它存在的意义。
SRC_FILES = ('app.js', 'sub_app.js', 'build_html.py', 'template.html', 'template_sub.html',
             'compute.py', 'obos_core.py',
             'fetch_data.py', 'fetch_sub_data.py', 'fetch_benchmark.py',
             'test_render.js', 'gate_edge_test.js')

fail = 0
def bad(msg):
    global fail
    fail += 1
    print('  FAIL  ' + msg)

def git(*args):
    return subprocess.run(['git', '-C', BASE] + list(args),
                          capture_output=True, text=True).stdout.strip()

d = yaml.safe_load(io.open(YML, encoding='utf-8'))
fresh = [s for s in d['jobs']['update']['steps'] if s.get('id') == 'fresh']
if not fresh:
    print('FAIL: 找不到 id=fresh 的 step（daily.yml 结构变了）')
    sys.exit(1)
run = fresh[0]['run']
env = fresh[0].get('env') or {}
for k in ('EVENT_NAME', 'BEFORE_SHA', 'AFTER_SHA', 'FORCE_REBUILD'):
    if k not in env:
        print('FAIL: fresh step 缺少 env %s（源码命中判定拿不到 sha）' % k)
        sys.exit(1)

# 抠出 SRC_HIT="" ... 外层 fi。
# 注意：YAML 的 `run: |` 是块标量，safe_load 后共同缩进已被剥离，
# 所以文件里看到的 10 空格缩进在这里不存在 —— 内层 fi 是 2 空格，外层 fi 顶格。
m = re.search(r'SRC_HIT="".*?\nfi\n', run, re.S)
if not m:
    print('FAIL: 无法从 daily.yml 抠出 SRC_HIT 判定段 —— 缩进或结构变了，请同步更新本脚本')
    sys.exit(1)
BLOCK = m.group(0) + ('\nif [ -n "$SRC_HIT" ]; then echo "RESULT=HIT"; '
                      'else echo "RESULT=NONE"; fi\n')

def probe(before, after, event='push'):
    e = dict(os.environ, EVENT_NAME=event, BEFORE_SHA=before, AFTER_SHA=after)
    r = subprocess.run(['bash', '-c', BLOCK], cwd=BASE, env=e,
                       capture_output=True, text=True)
    return ('RESULT=HIT' in r.stdout), r.stderr

def changed(before, sha):
    return [l for l in git('diff', '--name-only', before + '..' + sha).split('\n') if l.strip()]

print('===== 遍历 git 历史：门禁判定必须与真实改动清单逐条一致 =====')
shas = [s for s in git('log', '--format=%h', '--no-merges', '-60').split('\n') if s]
if not shas:
    print('FAIL: 取不到 git 历史')
    sys.exit(1)
n_hit = n_miss = 0
for sha in shas:
    before = git('rev-parse', sha + '^')
    if not before:
        continue                      # 初始 commit 无父提交，跳过
    files = changed(before, sha)
    if not files:
        continue
    truth = any(f in SRC_FILES for f in files)
    got, err = probe(before, sha)
    if got != truth:
        bad('%s 门禁判定=%s 但真实源码命中=%s（改动：%s）'
            % (sha, got, truth, ', '.join(files[:5])))
    if err.strip():
        bad('%s 产生 stderr: %s' % (sha, err.strip()[:120]))
    if truth:
        n_hit += 1
    else:
        n_miss += 1
print('  遍历 %d 个 commit：命中源码 %d 个（应强制重建），未命中 %d 个（应照常跳过）'
      % (len(shas), n_hit, n_miss))
if n_hit == 0:
    bad('历史里没有任何 commit 命中前端源码 —— 正向路径根本没被覆盖，等于没测')
if n_miss == 0:
    bad('历史里没有任何 commit 只改非源码文件 —— 反向路径根本没被覆盖，等于没测')

print('\n===== 边界：不应误触发 / 不应报错 =====')
sha = shas[0]
before = git('rev-parse', sha + '^')
if before:
    got, _ = probe(before, sha, event='schedule')
    if got:
        bad('定时触发(schedule) 不应走强制重建路径，判定=%s' % got)
    else:
        print('  PASS  定时触发(schedule)不触发强制重建')

ZERO = '0' * 40
got, err = probe(ZERO, sha)
if got or err.strip():
    bad('分支首次推送(before=全0) 应静默跳过，判定=%s stderr=%r' % (got, err.strip()[:80]))
else:
    print('  PASS  首次推送(before=全0)静默跳过且不报错')

got, err = probe('', '')
if got:
    bad('空 sha 不应命中，判定=%s' % got)
else:
    print('  PASS  空 sha 不崩且判定为不命中')

print()
if fail == 0:
    print('FRESHNESS GATE TEST PASSED：源码命中判定与真实改动清单逐条一致，边界不误触发')
else:
    print('FRESHNESS GATE TEST FAILED：%d 项' % fail)
sys.exit(1 if fail else 0)
