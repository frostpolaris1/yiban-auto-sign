// 易班自动签到 · 管理端脚本 —— 跨页面共用：时钟与签到状态 / 退出 / 行操作省略号菜单 / 更新日志模态 / 主题切换 / 启动引导
// 本文件是 web/static/js/app.js 的**连续区间** L1935-L2093，内容一字未改（仅加这 3 行头）。
// classic script、共享全局作用域：加载顺序见 templates/index.html，**顺序不可随意调整**。
// ================= 时钟与签到状态（本地平滑走秒，60 秒校准一次） =================
let clockOffset = 0;
let tzOffsetMin = 0;
async function calibrateClock() {
  try {
    const data = await api('/api/clock');
    clockOffset = data.server_ts - Math.floor(Date.now() / 1000);
    tzOffsetMin = Number(data.tz_offset_min) || 0;
    if (!$('tab-settings').classList.contains('hidden')) {
      // 颜色白名单校验：仅接受 #rrggbb（防属性上下文逃逸，纵深防御）
      const color = /^#[0-9a-f]{6}$/i.test(String(data.color || '')) ? data.color : '#7aa2f7';
      $('sign-status').innerHTML = `<span style="color:${color}">${esc(data.sign_status)}</span>`;
    }
  } catch (e) { /* 静默 */ }
}
function tickClock() {
  // 服务器本地时间：epoch + 服务器时区偏移后按 UTC 字段格式化（toISOString 即服务器墙上时间）
  const t = new Date((Math.floor(Date.now() / 1000) + clockOffset + tzOffsetMin * 60) * 1000);
  const str = t.toISOString().slice(0, 19).replace('T', ' ');
  $('sidebar-clock').textContent = '服务器时间：' + str;
  if (!$('tab-settings').classList.contains('hidden')) {
    $('clock-now').textContent = str;
  }
}

// ================= 退出 =================
async function doLogout() {
  try { await api('/api/logout', { method: 'POST' }); } catch (e) {}
  location.href = BASE + '/login';
}

// ============ 行操作省略号菜单（fixed 定位；a11y 整改：基于触发元素定位 + role=menu + 焦点管理） ============
let _rowMenu = null;
let _rowMenuTrigger = null;
// refocus=true：Esc 关闭时焦点归还触发按钮（键盘可达）
function closeRowMenu(refocus) {
  const t = _rowMenuTrigger;
  if (_rowMenu) { _rowMenu.remove(); _rowMenu = null; }
  _rowMenuTrigger = null;
  if (refocus && t && document.contains(t)) t.focus();
}
function openRowMenu(evt, items, anchor) {
  if (evt && evt.stopPropagation) evt.stopPropagation();
  closeRowMenu();
  // 定位改为基于触发元素 getBoundingClientRect：键盘 Enter 触发时 evt.clientX/Y 为 0，
  // 原实现菜单会飞到左上角；现始终出现在按钮旁
  const btn = anchor || (evt && evt.currentTarget) || null;
  _rowMenuTrigger = btn;
  const el = document.createElement('div');
  el.setAttribute('role', 'menu');
  el.setAttribute('aria-label', '更多操作');
  el.className = 'fixed z-50 bg-white dark:bg-zinc-800 rounded-xl border border-zinc-200 dark:border-zinc-700 shadow-lg py-1 min-w-36';
  let left = 16, top = 80;
  if (btn && typeof btn.getBoundingClientRect === 'function') {
    const r = btn.getBoundingClientRect();
    left = Math.max(8, Math.min(r.left, window.innerWidth - 180));
    top = Math.max(8, Math.min(r.bottom + 4, window.innerHeight - items.length * 40 - 12));
  }
  el.style.left = left + 'px';
  el.style.top = top + 'px';
  items.forEach(it => {
    const b = document.createElement('button');
    b.type = 'button';
    b.setAttribute('role', 'menuitem');
    b.className = 'w-full text-left px-4 py-2 min-h-[44px] text-sm ' + it.cls + ' hover:bg-zinc-50 dark:hover:bg-zinc-700 transition-colors duration-150';
    b.textContent = it.label;
    b.onclick = () => {
      const t = _rowMenuTrigger;
      closeRowMenu();
      if (t && document.contains(t)) t.focus();  // 焦点先归还触发元素，后续动作（如打开模态）从那里接管焦点链
      it.fn();
    };
    el.appendChild(b);
  });
  document.body.appendChild(el);
  _rowMenu = el;
  const first = el.querySelector('[role="menuitem"]');
  if (first) first.focus();  // 打开即聚焦第一项，键盘可直接 Enter 确认
}
document.addEventListener('click', () => closeRowMenu(false));

// 动态行内控件统一事件委托（2026-09-08）：用户可控值（邮箱）只经 esc() 放 data-* 属性，
// 不再拼进 onclick/onchange 的 JS 字符串——属性值不进 JS 解析器，无从借引号/二次解码逃逸。
// 账号行的批量复选框仍用内联 onchange（id 为数字下标，无注入面），不带 data-batch-key，不会重复触发。
document.addEventListener('click', (e) => {
  const btn = e.target.closest('[data-purge-email]');
  if (btn) purgeDeletedUser(btn.dataset.purgeEmail);
});
document.addEventListener('change', (e) => {
  const el = e.target;
  if (el && el.matches && el.matches('[data-batch-key]')) toggleRow(el.dataset.batchKey, el.dataset.batchId, el);
});

// 更新日志弹窗（版本号点击；Markdown 渲染）
async function openChangelog() {
  openModal($('changelog-modal'));  // 记录触发元素并聚焦模态内首控件；Esc 可关闭
  const content = $('changelog-content');
  content.innerHTML = '<span class="inline-flex items-center gap-2 text-xs text-zinc-500 dark:text-zinc-400"><span class="yb-spinner"></span>加载中…</span>';
  try {
    const data = await api('/api/changelog');
    content.innerHTML = window.renderMarkdown ? renderMarkdown(data.text || '暂无更新日志') : esc(data.text || '暂无更新日志');
  } catch (e) { content.innerHTML = '加载失败，请稍后重试'; }
}
function closeChangelog() { closeModal($('changelog-modal')); }

// 初始化
(async function init() {
  try {
    const me = await api('/api/me');
    if (me.role !== 'admin') { location.href = BASE + '/user'; return; }  // 普通用户 → 用户页
    csrfToken = me.csrf_token || '';
    state.isMasterAdmin = !!me.is_builtin_admin;  // 主管理员才可设置/取消管理员
    if (state.isMasterAdmin) {
      // 主管理员改密卡片提示提档（12 位三类，与后端口令策略同口径）
      const lbl = document.querySelector('label[for="my-new-password"]');
      if (lbl) lbl.textContent = `新密码（${PW_ADMIN_HINT}）`;
    }
    mailSelfOn = !!me.mail_notify;  // 普通管理员个人邮件开关（同普通用户）
    // 侧边栏显示当前账号名（防登录错管理员账号，2026-08-16）
    const acctName = me.username || me.email || '';
    $('sidebar-account-name').textContent = acctName + (state.isMasterAdmin ? '（主管理员）' : '');
    $('sidebar-account-name').title = acctName;
    // 修改密码卡片标题按角色区分：主管理员=修改主管理员密码，普通管理员=修改密码（改的是自己账号）
    $('my-password-title').textContent = state.isMasterAdmin ? '修改主管理员密码' : '修改密码';
    // 「我的账号」改密入口：仅普通管理员可见（主管理员改密走设置页危险区，v0.30.0）
    const minePwCard = $('mine-password-card');
    if (minePwCard) minePwCard.classList.toggle('hidden', state.isMasterAdmin);
    if (!state.isMasterAdmin) {
      // 普通管理员：隐藏「设为/取消管理员」批量按钮（仅主管理员权限）
      document.querySelectorAll('.batch-admin-only').forEach(el => el.classList.add('hidden'));
    }
    await loadSettings();  // 先完成设置加载（末尾 renderAccounts 依赖），再切账号 tab 拉状态——防首次"待签"竞态
    const pauseBtn = $('global-pause-btn');
    if (pauseBtn) pauseBtn.addEventListener('click', toggleGlobalPause);
    const regPauseBtn = $('reg-pause-btn');
    if (regPauseBtn) regPauseBtn.addEventListener('click', toggleRegPause);
    switchTab('accounts');
    loadLogs();
    calibrateClock();
    tickClock();
    setInterval(pollVisible, 10000);    // 可见性轮询：仅当前 tab 可见时请求（日志/账号）10s
    setInterval(calibrateClock, 60000);  // 时钟校准 60s
    setInterval(tickClock, 1000);    // 时钟走秒 1s
  } catch (e) {
    location.href = BASE + '/login';
  }
})();
// 暗色主题切换（localStorage 记忆）
function toggleTheme() {
  const dark = document.documentElement.classList.toggle('dark');
  try { localStorage.setItem('yiban-theme', dark ? 'dark' : 'light'); } catch (e) {}
  updateThemeBtn();
}
function updateThemeBtn() {
  const dark = document.documentElement.classList.contains('dark');
  document.querySelectorAll('[data-theme-btn]').forEach(b => { b.innerHTML = icon(dark ? 'sun' : 'moon'); });
}
document.addEventListener('DOMContentLoaded', () => { updateThemeBtn(); hydrateIcons(); renderGlobalPauseUI?.(); initFullTables(); });
// ================= 用户管理（仅管理员）=================
