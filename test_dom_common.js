/* 两道 jsdom 门禁（test_dom_sub.js / test_dom_noseries.js）的公共工具。
 * 抽出来的唯一理由：取值正则只有一份，免得改了这边忘了那边。
 *
 * 背景：两道门禁的输入都是"构建产物 + 当前源码"，不是仓库里的 data/*.json。
 * 后者在 .gitignore 里（data/ 由 CI 独家写盘），CI checkout 根本拿不到 ——
 * 门禁一进 CI 就 ENOENT 挂掉，而本地一直是绿的，属于典型的"只在本机跑过"。
 * 改用已提交的构建产物后，本地与 CI 读的是同一份东西。 */
const fs = require('fs');

/* 从构建产物里取出某个 var 的原文（不做 JSON 解析）。
 * 非贪婪匹配到该 var 结束的 `;`，后面要么跟另一个 var，要么就是文件末尾 ——
 * 这样 PARENTS 跟在 DATA 后面、DATA 跟在别的东西后面都取得准。
 * 取不到返回 null，由调用方决定是判红还是退化（不要在这里替它做主）。 */
function readVar(src, name) {
  const m = src.match(new RegExp('var ' + name + ' = ([\\s\\S]*?);\\s*(?:var |$)'));
  return m ? m[1] : null;
}

/* 时序字段清单。首屏数据里出现任意一个都说明 build_html.py 的剥离没生效，
 * 那样"时序缺席"的测试就是在假装缺席（DATA 里时序字段其实齐着呢）。 */
const SERIES_KEYS = ['close', 'ob_series', 'os_series', 'score', 'rs_pct'];

function seriesLeaked(ind0) {
  return SERIES_KEYS.filter(k => (ind0 || {})[k] !== undefined);
}

module.exports = { fs, readVar, SERIES_KEYS, seriesLeaked };
