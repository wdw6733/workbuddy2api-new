/* 面板里「调用了但没定义」的页面函数，必须在提交前就被抓住。
 *
 * 背景：renderStationSolver 被 loadStations 调用但从未定义，浏览器里表现为
 * 站点列表整块「加载失败：renderStationSolver is not defined」。内联 onclick
 * 的检查（_test_dashboard_handlers.js）看不到函数体内部的调用，所以这类漏掉
 * 的定义会一路进到线上。
 *
 * 做法：只看「像页面辅助函数」的名字（render* / open* / close* / load* /
 * station* / checkin* / ... ），要求它们在 dashboard.html 里被定义过。
 * 用前缀白名单而不是全量标识符，是为了避开浏览器内置与第三方全局的噪音。
 *
 * Run with Node: node tests/_test_dashboard_page_functions.js
 */
const assert = require('assert');
const fs = require('fs');
const path = require('path');

const html = fs.readFileSync(path.join(__dirname, '..', 'dashboard.html'), 'utf8');

// 页面辅助函数的命名前缀（大小写敏感的第二段或直接跟名字）
const PAGE_FN = /^(render|open|close|load|save|delete|probe|fetch|checkin|login|logout|station|toggle|refresh|switch|apply|clear|reset|start|stop|build|draw|paint|update|show|hide)[A-Z_$][\w$]*$/;

// 定义：function foo( / const foo = / let foo = / var foo = / window.foo =
const DEFS = [
  /\bfunction\s+([A-Za-z_$][\w$]*)\s*\(/g,
  /\b(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*=/g,
  /\bwindow\.([A-Za-z_$][\w$]*)\s*=/g,
];

// 前缀撞上浏览器内置的几个，直接放行
const BUILTINS = new Set(['clearInterval', 'clearTimeout', 'clearImmediate',
                          'close', 'open', 'update']);

const defined = new Set();
for (const re of DEFS) {
  for (const [, name] of html.matchAll(re)) defined.add(name);
}

// 只看 <script> 里的调用（内联 onclick 由另一个测试负责）
const blocks = [...html.matchAll(/<script[^>]*>([\s\S]*?)<\/script>/g)].map(m => m[1]);
const CALL = /(?<![.\w$])([A-Za-z_$][\w$]*)\s*\(/g;

const missing = new Map(); // name -> 第一次出现的位置片段
for (const block of blocks) {
  for (const m of block.matchAll(CALL)) {
    const name = m[1];
    if (!PAGE_FN.test(name)) continue;
    if (BUILTINS.has(name)) continue;
    if (defined.has(name)) continue;
    if (!missing.has(name)) {
      const at = Math.max(0, m.index - 40);
      missing.set(name, block.slice(at, m.index + name.length + 2).replace(/\s+/g, ' '));
    }
  }
}

const problems = [...missing.entries()].map(([name, ctx]) => `${name}  (…${ctx}…)`);
assert.deepStrictEqual(problems, [],
  '这些页面函数被调用了但没有定义：\n' + problems.join('\n'));

console.log(`dashboard page functions: ${defined.size} defined, all calls resolved`);
