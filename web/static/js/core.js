// 易班自动签到 · 管理端脚本 —— 基础设施：全局状态 / api 请求与 CSRF / toast / 模态与焦点管理 / 口令策略 / 密码模态 / 侧栏抽屉 / Tab 切换
// 本文件是 web/static/js/app.js 的**连续区间** L1-L245，内容一字未改（仅加这 3 行头）。
// classic script、共享全局作用域：加载顺序见 templates/index.html，**顺序不可随意调整**。
/* 原 web/templates/index.html 内联脚本外提（A1）：
   第一段 = 原 1284-4006 行主 <script>（状态/api/toast/模态/账号/日志/设置/通知/用户/我的账号/日历）
   第二段 = 原 4007-4023 行 <script>（loadAnnouncement）
   两段顺序与原文档一致。

   本文件必须是 classic script（<script src> 不带 type="module"）：
   模板里有 137 个内联 onclick/onchange 直接调用本文件定义的全局函数，
   模块作用域不进全局，会全部 ReferenceError。
   依赖 index.html 内联脚本先定义的 BASE（= request.script_root），故 <script src>
   的位置必须在 BASE 之后、且在 body 末尾（本文件含直接操作 DOM 的顶层语句）。 */
// ================= 签到状态码 → 图标/文案（与后端 STATUS_ICON/STATUS_TEXT 语义一致，含 pending）=================
// UI 一律用线性 SVG 图标渲染（stateIconSvg）；emoji 仅作后端 API 数据兼容，不再直接展示
const STATUS_ICON_NAME = {
  success: 'check', already: 'check', no_task: 'minus', failed: 'close', retrying: 'retry',
  skipped_window: 'ban', skipped_norange: 'ban', paused: 'pause', user_cancelled: 'stop', pending: 'clock',
};
function stateIconSvg(st) { return icon(STATUS_ICON_NAME[st] || 'clock'); }
const STATUS_TEXT = {
  success: '签到成功', already: '已签到', no_task: '无需签到', failed: '签到失败',
  retrying: '重试中', skipped_window: '时段外', skipped_norange: '未设时段', paused: '暂停',
  user_cancelled: '已取消', pending: '待签',
};
// ================= 状态 =================
const state = {
  accounts: [],       // [{index,name,phone,phone_model,has_password,display_name}]
  states: {},         // {phone: 'success'|'already'|'no_task'|'failed'|'retrying'|'skipped_*'}（状态码，来自 sign-state 文件）
  editingIndex: null, // null=添加
  editSnapshot: null, // 乐观锁：编辑打开时的账号快照 JSON（提交时校验是否被其他管理员修改）
  delays: { gap: 0 }, // 账号间隔（秒，0=关闭；启动延迟 v0.30.0 废弃已删）
  maxUsers: 0,        // 用户容量上限现值（GET /api/settings capacity.users_max；0=不限）
  maxAccounts: 0,     // 账号容量上限现值（capacity.accounts_max；0=不限）
  mailSmtps: [],      // SMTP 发信条目列表（GET /api/mail-config 的 smtps；主管理员编辑器数据源）
  batchMode: false,   // 批量多选开关（会话级：每次进入默认关闭，手动开启仅本次有效，不持久化）
  signOrder: 'sequence', // 调度 v2：排序方式（sequence/random）
  signDist: 'uniform',   // 调度 v2：分布方式（uniform/normal）
  edgeFrontMin: 1,      // 掐头去尾（0.22.0 前后独立）：前裁剪分钟（0-5，0.5 步进）
  edgeBackMin: 1,       // 后裁剪分钟
  allowTimePref: false,  // 调度 v2：用户自选时间片总开关
  signWindow: '',        // 调度 v2：签到窗口 "06:30 ~ 07:50"
  isMasterAdmin: false, // 主管理员（.env 内置管理员）：仅主管理员可设置/取消管理员
  deletedCollapsed: false, // 待删除账号表格折叠状态（标题栏 ▾/▸ 切换）
  saturdaySign: false,  // 周六签到开关（默认关闭，v0.29.0 起；.env YIBAN_SATURDAY_SIGN=1 开启）
};

const $ = (id) => document.getElementById(id);

// ================= 基础请求 =================
let csrfToken = '';  // 登录后从 /api/me 获取，写请求统一携带（CSRF 防护）

async function api(path, opts = {}, _retried = false) {
  const headers = {
    'Content-Type': 'application/json',
    ...(csrfToken ? {'X-CSRF-Token': csrfToken} : {}),
    ...(opts.headers || {}),
  };
  const resp = await fetch(BASE + path, {
    ...opts,
    headers,
  });
  if (resp.status === 401) {
    location.href = BASE + '/login';
    throw new Error('未登录');
  }
  const data = await resp.json().catch(() => ({}));
  // CSRF token 不同步（如服务重启/会话更新）：自动重新获取 token 并重试一次，用户无感
  if (resp.status === 403 && !_retried && data.error && data.error.includes('校验失败')) {
    try {
      const me = await fetch(BASE + '/api/me').then(r => r.json());
      csrfToken = me.csrf_token || '';
      return api(path, opts, true);
    } catch (e) { /* 重试失败则走下方错误提示 */ }
  }
  if (!resp.ok || data.ok === false) {
    throw new Error(data.error || `请求失败 (${resp.status})`);
  }
  return data;
}

function toast(msg, isError = false) {
  const el = $('toast');
  el.textContent = msg;
  // a11y 整改：只更新 class 与文本。className 赋值不会触碰 role="status"/aria-live="polite" 属性，
  // 动态消息对屏幕阅读器保持可感知；补回 max-w-[90vw]/text-center/break-words 防长文案溢出（与初始 class 一致）
  el.className = `fixed bottom-4 left-1/2 -translate-x-1/2 z-[60] rounded-lg px-4 py-2 shadow-md text-sm text-white max-w-[90vw] text-center break-words ${isError ? 'bg-red-600' : 'bg-zinc-900 dark:bg-zinc-700'}`;
  el.removeAttribute('data-hidden');  // 显示：配合 #toast 过渡自下方浮入
  clearTimeout(el._timer);
  el._timer = setTimeout(() => el.setAttribute('data-hidden', ''), 3000);  // 隐藏：快速淡出
}

// ================= 模态焦点管理（a11y 整改：打开记录触发元素→聚焦首控件、Tab 圈闭、Esc 关闭、焦点归还） =================
const _modalStack = [];  // [{ el, trigger }]，后进先出支持叠层

// 背景滚动锁：模态打开期间锁 html/body 滚动，防止滚轮/触摸/键盘滚动穿透到背景页（滚动链）。
// 计数支持叠层；锁死前补偿滚动条宽度，避免内容区横向跳动。
let _modalScrollLockCount = 0;
function _lockPageScroll() {
  _modalScrollLockCount++;
  if (_modalScrollLockCount > 1) return;
  const gap = window.innerWidth - document.documentElement.clientWidth;
  if (gap > 0) document.body.style.paddingRight = gap + 'px';
  document.documentElement.style.overflow = 'hidden';
  document.body.style.overflow = 'hidden';
}
function _unlockPageScroll() {
  if (_modalScrollLockCount > 0) _modalScrollLockCount--;
  if (_modalScrollLockCount > 0) return;
  document.documentElement.style.overflow = '';
  document.body.style.overflow = '';
  document.body.style.paddingRight = '';
}

function _modalFocusables(el) {
  return Array.from(el.querySelectorAll('button, [href], input, select, textarea, [tabindex]:not([tabindex="-1"])'))
    .filter(x => !x.disabled && x.offsetParent !== null);  // offsetParent 过滤 hidden/不可见元素
}

function openModal(el, trigger) {
  if (!el || _modalStack.some(m => m.el === el)) return;
  _modalStack.push({ el, trigger: trigger || document.activeElement });
  el.classList.remove('hidden');
  _lockPageScroll();
  const first = _modalFocusables(el)[0];
  if (first) first.focus();
  else el.focus();  // 容器带 tabindex="-1" 兜底
}

function closeModal(el) {
  const i = _modalStack.findIndex(m => m.el === el);
  if (i === -1) return;
  const { trigger } = _modalStack.splice(i, 1)[0];
  el.classList.add('hidden');
  _unlockPageScroll();
  // 焦点归还触发元素（列表重绘可能已移除该元素，contains 防护）
  if (trigger && document.contains(trigger) && typeof trigger.focus === 'function') trigger.focus();
}

// 全局键盘：Esc 逐层关闭（行菜单 → 模态 → 侧栏抽屉遮罩）；Tab 在最上层模态内圈闭
document.addEventListener('keydown', (e) => {
  if (e.key === 'Escape') {
    if (_rowMenu) { closeRowMenu(true); return; }
    if (_modalStack.length) { e.preventDefault(); closeModal(_modalStack[_modalStack.length - 1].el); return; }
    const overlay = $('sidebar-overlay');
    if (overlay && overlay.classList.contains('open')) toggleSidebar(false);
    return;
  }
  if (e.key === 'Tab' && _modalStack.length) {
    const top = _modalStack[_modalStack.length - 1].el;
    const items = _modalFocusables(top);
    if (!items.length) return;
    const first = items[0], last = items[items.length - 1];
    if (!top.contains(document.activeElement)) { e.preventDefault(); first.focus(); }
    else if (e.shiftKey && document.activeElement === first) { e.preventDefault(); last.focus(); }
    else if (!e.shiftKey && document.activeElement === last) { e.preventDefault(); first.focus(); }
  }
});

// ================= 口令策略（本模板内单一事实源） =================
// 与后端 web/app.py 的 _PASSWORD_CLASS_PATTERNS / _PASSWORD_POLICY_HINT 逐字同序同串——
// tests/test_batch14_fixes_0829.py 的元测试会从本文件源码提取这四个正则与后端比对，漂移即红。
// 判定语义：命中类别数 >= PW_MIN_CLASSES 即过（符号算一类，不额外要求必须含符号）。
const PW_CLASS_PATTERNS = [/[A-Z]/, /[a-z]/, /\d/, /[^A-Za-z0-9]/];
const PW_MIN_LEN = 10, PW_MIN_CLASSES = 2;
const PW_POLICY_HINT = '至少 10 位，且包含大小写字母、数字、符号中的至少两类';
// 主管理员（内置 .env 管理员）口令单独提档：12 位三类（与后端 _admin_password_policy_error 同口径）
const PW_ADMIN_MIN_LEN = 12, PW_ADMIN_MIN_CLASSES = 3;
const PW_ADMIN_HINT = '至少 12 位，且包含大写字母、小写字母、数字、符号中的至少三类';
function passwordClasses(v) { return PW_CLASS_PATTERNS.filter(re => re.test(v)).length; }
function passwordPolicyOk(v) { return v.length >= PW_MIN_LEN && passwordClasses(v) >= PW_MIN_CLASSES; }
function passwordPolicyOkAdmin(v) { return v.length >= PW_ADMIN_MIN_LEN && passwordClasses(v) >= PW_ADMIN_MIN_CLASSES; }

// ================= 密码模态（重置密码 / 高危操作二次确认共用） =================
let _pwModalCb = null;
let _pwModalMode = 'set';  // 'set'=设置新密码（走口令策略校验）；'confirm'=确认当前管理员密码
function openPasswordModal(desc, cb) { openPwModal(desc, cb, 'set'); }
function openConfirmPasswordModal(desc, cb) { openPwModal(desc, cb, 'confirm'); }
function openPwModal(desc, cb, mode) {
  _pwModalCb = cb;
  _pwModalMode = mode;
  const isConfirm = mode === 'confirm';
  $('modal-password-title').textContent = isConfirm ? '安全确认' : '重置密码';
  $('modal-password-desc').textContent = desc;  // textContent 赋值：动态数据（邮箱等）无注入面
  $('modal-password-input').value = '';
  // placeholder 保持短句：手机端输入框内不换行，完整口径由可换行的 desc 承载
  $('modal-password-input').placeholder = isConfirm ? '输入当前管理员密码' : '设置新密码（至少 10 位）';
  $('modal-password-input').autocomplete = isConfirm ? 'current-password' : 'new-password';
  $('modal-password-submit').textContent = isConfirm ? '确认操作' : '确认重置';
  openModal($('modal-password'));  // 打开即聚焦密码输入框（容器内首个可交互元素）
}
function closePasswordModal() {
  _pwModalCb = null;
  closeModal($('modal-password'));
}
$('modal-password-form').addEventListener('submit', (e) => {
  e.preventDefault();
  const pw = $('modal-password-input').value;
  if (!pw) return;  // 空值静默不提交
  // set 模式按完整口令策略校验（长度 + 至少两类）：与后端 _password_policy_error 及
  // saveMyPassword 同口径，避免"前端放行、提交后才 400"；confirm 模式仍是只验非空
  if (_pwModalMode === 'set' && !passwordPolicyOk(pw)) { toast(`密码${PW_POLICY_HINT}`, true); return; }
  const cb = _pwModalCb;
  closePasswordModal();
  if (cb) cb(pw);
});

// ================= 侧边栏（移动端抽屉） =================
function toggleSidebar(open) {
  $('sidebar').classList.toggle('-translate-x-full', !open);
  $('sidebar-overlay').classList.toggle('open', open);  // 遮罩经 CSS 淡入淡出（替代 hidden 瞬显）
}

// ================= Tab 切换 =================
function switchTab(name) {
  // 未保存的调度改动守卫（2026-08-15 对抗性审查 F-1）：切走会静默丢失，先确认
  if (name !== 'settings' && schedDirty) {
    if (!confirm('调度设置还有未保存的修改，切换页面将丢失。\n是否继续？')) return;
    schedDirty = false;
    $('schedule-save-btn').classList.add('hidden');
    $('sched-dirty-tip').classList.add('hidden');
  }
  ['accounts', 'logs', 'settings', 'users', 'mine'].forEach(t => {
    $('tab-' + t).classList.toggle('hidden', t !== name);
  });
  document.querySelectorAll('[data-tab-btn]').forEach(btn => {
    const active = btn.dataset.tabBtn === name;
    btn.classList.toggle('text-zinc-600', !active);
    btn.classList.toggle('dark:text-zinc-300', !active);
    btn.classList.toggle('text-zinc-900', active);
    btn.classList.toggle('dark:text-zinc-100', active);
    btn.classList.toggle('bg-zinc-100', active);
    btn.classList.toggle('dark:bg-zinc-700', active);
    btn.classList.toggle('border-l-blue-500', active);
    btn.classList.toggle('border-l-2', active);
  });
  toggleSidebar(false);
  const tabEl = $('tab-' + name);
  tabEl.classList.remove('tab-enter');
  void tabEl.offsetWidth;  // 重触发动画
  tabEl.classList.add('tab-enter');
  if (name === 'accounts') { loadAccounts(); updateSignModeHint(); }
  if (name === 'logs') { initLogSearch(); loadLogs(); fillSigninSelect(); }
  if (name === 'settings') { loadSettings(); calibrateClock(); tickClock(); }
  if (name === 'users') loadUsers();
  if (name === 'mine') loadMine();
}

