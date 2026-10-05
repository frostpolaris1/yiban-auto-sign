/**
 * 日志页日期校验（**刻意保留纯 JS**，不是 TS）。
 *
 * 为什么不用 TS：`tests/test_logs_by_date.py::LogsDateValidationTest` 会用正则从本文件
 * 抽出校验函数整段，并**在 Node 里真跑**，按两个时区、VALID/INVALID 两组日期逐项断言
 * （迁移前它扫的是 legacy 的 `web/static/js/pages/data_logs.js`）。抽函数靠字面量匹配，故：
 *   · 必须保持标准的具名 function 声明写法（不要加 TS 注解、不要改成箭头函数）；
 *   · **本注释块内不得出现那个字面量本身**——抽取器先命中注释里的那次出现，就会从注释处
 *     数括号、永远配不平（实测踩过：注释里写了"必须保持 <字面量>"，测试直接报
 *     "未找到匹配的右花括号"）。同理不要在这里写正则里的花括号量词实例。
 *   · 该测试是本文件行为的唯一守卫，改动实现前先读它。
 *
 * 时区安全的由来（不可退回的写法）：不能用
 * `new Date(s + "T00:00:00").toISOString().slice(0,10) === s` 做回环校验——字符串按
 * **本地时区**解析而 toISOString 转 UTC，UTC+8 下会回退一天，所有日期都被判非法
 * （实测 Asia/Shanghai 下 "2026-09-11" → "2026-09-10"），「查看」与 ?date= 分享双双失效。
 *
 * @param {string} s
 * @returns {boolean}
 */
export function isValidDate(s) {
  if (!/^\d{4}-\d{2}-\d{2}$/.test(s)) return false;
  var y = +s.slice(0, 4), m = +s.slice(5, 7), d = +s.slice(8, 10);
  var dt = new Date(y, m - 1, d);
  return dt.getFullYear() === y && dt.getMonth() === m - 1 && dt.getDate() === d;
}

/** 本地日期字符串（YYYY-MM-DD），「回到今天」用。 */
export function todayStr() {
  const d = new Date();
  const pad2 = (n) => (n < 10 ? "0" : "") + n;
  return d.getFullYear() + "-" + pad2(d.getMonth() + 1) + "-" + pad2(d.getDate());
}
