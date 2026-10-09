/**
 * 签到日历口径（**刻意保留纯 JS**，不是 TS）。
 *
 * 为什么不用 TS：`tests/test_calendar_state_visibility.py` 的 `StatusLineJsTest` /
 * `CalendarCellJsTest` 会用 `_extract_function` 从本文件里按花括号配对抽出
 * `statusLine` / `stateEntry` / `dayCell` 三整段，并**在 Node 里真跑**（迁移前它们扫的是
 * legacy 的 `components/sign-calendar-view.js` 与 `js/calendar.js`）。抽函数靠字面量
 * `"function " + 名字 + "("` 定位，故：
 *   · 必须保持标准具名 function 声明写法（不加 TS 类型注解、不改成箭头函数、参数不带
 *     对象默认值解构）——三者都会让抽出的文本在 Node 里跑不起来或配错括号；
 *   · **本注释块内不得出现那三个字面量本身**（即"function 加名字加左括号"的连写）；
 *     抽取器先命中注释里的那次出现，就会从注释处数括号、永远配不平（date-guard.js
 *     实测踩过这个坑，故这里只以描述方式指代）；
 *   · 三个函数体内（含其中注释）不得出现不成对的 `{` / `}`——抽取器只数花括号，字符串/
 *     注释里的裸花括号同样计入。
 *   · 改动实现前先读那个测试。它只认字面量，被格式化即报错（失效方向是红，不是假绿）。
 *
 * 其余口径（格子组合、换月方向、图例并集、日志空态、周末门）由 Vitest 覆盖
 * （`frontend/src/calendar/model.spec.ts`）——Python 抽不到那些，也不必抽。
 *
 * ## 视觉通道分配（产品定版：状态一律用颜色表达，emoji 不上界面）
 *   ① 底色 = 状态本身（五档色相即语义：绿成功/红失败/灰有意不签/明黄异常/蓝白呼吸=正在
 *      签到），色值见 app.css 的 --cal-* 令牌与 sc-breathe 关键帧，由
 *      tests/test_web_css_contrast.py 按「文字 on 格底」实测 AA，五档底色等明度；
 *   ② inset ring = 今天（不占布局，与底色、选中框互不争夺）；
 *   ③ 角标「休」 = 周末停签（中性底 + 文字角标，不单靠颜色区分）；
 *   ④ 选中 = 主色实框 + 主色环（独立于上面三者，且**不改底色**）。
 * ⚠ 周末停签**不**把数字做很浅：该格仍可点（点了提示「周末无需签到」），属有信息的
 *   格子、不是 WCAG 1.4.3 豁免的非活动控件，故同样保证可读。
 *
 * 状态符号只是按日状态文件的存储口径，反查完语气档即弃——符号本身不渲染。
 */

/** 周一起始的星期表头（用户可见契约，顺序即列序）。 */
export var WEEK = ["一", "二", "三", "四", "五", "六", "日"];

/** 固定 6 行 × 7 列 = 42 格：任意月份等高，换月不跳。 */
export var ROWS = 6;
export var CELLS = ROWS * 7;

/** 首屏骨架格 / 前置空位格：与真实格同尺寸，保证网格高度恒定。 */
export var SKELETON_CLS = "sc-cell sc-cell--skeleton";
export var BLANK_CLS = "sc-blank";

/** 日历与日志接口（本页两个数据源，路径唯一定义处）。 */
export var CAL_API = "/api/my-calendar";
export var LOG_API = "/api/my-logs";

/** 短请求不播进入动画：加载越短越不该动（与 legacy calendar.js 同阈值）。 */
export var ANIM_MIN_MS = 80;

export function pad2(n) {
  return String(n).padStart(2, "0");
}

/** 本地日期串（YYYY-MM-DD）。刻意不借 toISOString：那是 UTC，UTC+8 下会差一天。 */
export function todayStr() {
  var d = new Date();
  return d.getFullYear() + "-" + pad2(d.getMonth() + 1) + "-" + pad2(d.getDate());
}

export function monthLabel(year, month) {
  return year + "年" + month + "月";
}

/** 月份键（YYYY-MM）：接口参数、方向比较、日期前缀三处同一口径。 */
export function monthKey(year, month) {
  return year + "-" + pad2(month);
}

/** 月份序号（年 ×12 + 月）：跨年比较用整数差，不比字符串。 */
export function monthIndex(year, month) {
  return year * 12 + month;
}

/** 换月：返回新月份（不就地改传入对象），跨年自动归位。 */
export function shiftMonth(st, delta) {
  var year = st.year;
  var month = st.month + delta;
  if (month < 1) {
    month = 12;
    year--;
  }
  if (month > 12) {
    month = 1;
    year++;
  }
  return { year: year, month: month };
}

/**
 * 换月方向 = 本次月份 相对 **上一次已渲染**月份 的差（+1 往后、-1 往前、0 未换月）。
 *
 * 为什么对"已渲染"而不是"上一次点击"：月份状态在数据落地后才写回，故连续快点两次
 * 也按同一方向累计（两次都是 +1），而不是第二次退化成"未换月"。
 */
export function monthDirection(prevKey, year, month) {
  if (!prevKey) return 0;
  var p = String(prevKey).split("-");
  if (p.length !== 2) return 0;
  return Math.sign(monthIndex(year, month) - monthIndex(Number(p[0]), Number(p[1])));
}

/** 该日是否周末停签 → "日" / "六" / ""（开关取服务端同一份 .env 解析结果）。 */
export function offDayOf(date, flags) {
  var wd = new Date(date + "T00:00:00").getDay();
  if (wd === 0 && !(flags && flags.sunday)) return "日";
  if (wd === 6 && !(flags && flags.saturday)) return "六";
  return "";
}

/**
 * 符号 → 显示档（语气档 + 读屏短名）。
 *
 * ctx 由服务端渲染进页面（`window.YB_CALENDAR_STATE`，唯一事实源 yiban.status.DISPLAY），
 * 经 props 显式传入而不是在这里读全局：状态行的对拍测试因此无需伪造 window。
 */
export function stateEntry(symbol, ctx) {
  var table = (ctx && ctx.by_symbol) || {};
  return table[symbol] || null;
}

/**
 * 日期格：语气档 → 底色类名，并给出完整读屏名（日期 + 今天 + 状态 + 周末停签）。
 *
 * 返回数据而不是 HTML 串：类名/读屏名是口径（Python 的对拍测试断言这两个字段），
 * 标签与属性绑定属于 Vue 模板。`pressed`/`offBadge` 一并给出，避免模板里再判一次。
 */
export function dayCell(o, ctx) {
  var entry = stateEntry(o.state, ctx);
  var tone = entry ? entry.tone : "";
  var cls = "sc-cell";
  if (o.off) cls += " sc-cell--off";
  else if (tone) cls += " sc-cell--" + tone;
  else cls += " sc-cell--none";
  if (o.isToday) cls += " sc-cell--today";
  if (o.selected) cls += " is-selected";
  var off = o.offDay ? "（周" + o.offDay + "不签到）" : "";
  var stateText = entry ? entry.label : "";
  var label = o.date + (o.isToday ? "，今天" : "")
    + (stateText ? "，" + stateText : (off ? "" : "，查看签到记录"))
    + off;
  return {
    d: o.d,
    date: o.date,
    cls: cls,
    label: label,
    pressed: !!o.selected,
    offBadge: o.offDay || "",
  };
}

/**
 * 账号卡状态行：当前签到状态（含排队位次）的文案与语气档。
 *
 * 文案与语气档一律取自**服务端下发的状态表**（ctx.by_code）。为什么不再逐码手写：
 * 手写清单会漏码——global_paused（急停）与 no_position（无点位）原先都落进默认分支，
 * 被渲染成"待签到 · 前方排队 N 人"，于是引擎真的暂停了、面板却报"在排队"。
 *
 * 没有当日记录的账号（pending）先看今天是否被急停/周末门挡下（ctx.day_off，由服务端
 * 读 .env 真值后下发），否则才按排队口径。
 */
export function statusLine(a, ctx) {
  ctx = ctx || {};
  var s = a.state_status || "pending";
  if (s === "pending") {
    if (ctx.day_off) {
      return { cls: "state-line--" + (ctx.day_off.tone || "muted"), text: ctx.day_off.text };
    }
    var plan = a.state_message && a.state_message.indexOf("计划") === 0 ? " · 今日" + a.state_message : "";
    return { cls: "state-line--muted", text: "待签到" + plan + " · 前方排队 " + a.queue_ahead + " 人" };
  }
  var d = (ctx.by_code || {})[s];
  if (!d) {
    return { cls: "state-line--muted", text: "状态未知（" + s + "），请刷新页面后重试" };
  }
  var text = d.text;
  if (s === "failed") text += a.state_message ? "：" + a.state_message : "";
  return { cls: "state-line--" + d.tone, text: text };
}

/**
 * 月历格子组合：前置空位 + 当月日期 + 尾部补齐 → 恒定 42 格（空格为 null）。
 *
 * 同时回报**真正显示出来**的语气档集合：图例的职责是解释当前看得见的颜色，语气档
 * 全量陈列是服务端的事；周末停签格显示「休」而不显示状态底色，故不计入（legacy 同口径）。
 */
export function gridCells(opts) {
  var data = opts.data || {};
  var year = opts.year;
  var month = opts.month;
  var prefix = monthKey(year, month);
  var firstDay = (new Date(year, month - 1, 1).getDay() + 6) % 7;
  var days = new Date(year, month, 0).getDate();
  var cells = [];
  var used = [];
  var lead;
  for (lead = 0; lead < firstDay; lead++) cells.push(null);
  for (var d = 1; d <= days; d++) {
    var date = prefix + "-" + pad2(d);
    var stt = data.days && data.days[date] ? data.days[date][opts.phone] || "" : "";
    var offDay = offDayOf(date, opts.flags);
    var entry = stateEntry(stt, opts.ctx);
    if (entry && !offDay) used.push(entry.tone);
    cells.push(dayCell({
      d: d, date: date, state: stt, off: !!offDay,
      selected: date === opts.selected, offDay: offDay, isToday: date === opts.today,
    }, opts.ctx));
  }
  while (cells.length < CELLS) cells.push(null);
  return { cells: cells, used: used };
}

/** 多张日历卡的语气档并集（图例收敛键）：并集驱动显隐，切月只重算该卡。 */
export function unionTones(byKey) {
  var used = {};
  Object.keys(byKey || {}).forEach(function (k) {
    (byKey[k] || []).forEach(function (t) { used[t] = true; });
  });
  return used;
}

/** 周末停签当日无需查日志：返回跳过说明；照常签到日返回空串。 */
export function logSkipText(date, flags) {
  var off = offDayOf(date, flags);
  if (off === "日") return "周日无需签到";
  if (off === "六") return "周六无需签到";
  return "";
}

/** 当日无日志时的空态正文（带日期，避免与"还没选日期"的引导态混淆）。 */
export function logEmptyText(date) {
  return date + " 暂无签到记录";
}

/**
 * 日志面板折叠/展开之外的显示决策：`is-fresh` 淡入只在慢请求时播（短请求不播）。
 */
export function shouldAnimate(elapsedMs) {
  return elapsedMs > ANIM_MIN_MS;
}
