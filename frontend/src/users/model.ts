/**
 * 用户管理页口径（纯函数：无 DOM、无网络、无全局）。
 *
 * 与 legacy `web/static/js/pages/work_users.js` 的分工：写操作链路（确认 → 门禁 → 请求 →
 * 提示 → 刷新）留在 `ops.js`（纯 JS，被 tests/test_users_exit_surface_frontend.py 真跑钉住
 * 的 MF-49 出口面），**分组 / 检索 / 计数 / 行菜单 / 展示文案**这些"看着简单、错了很难发现"
 * 的判断收到这里，做成可单测的形态。
 */

export type Group = "pending" | "normal" | "vacant" | "deleted";

export interface UserRecord {
  /** 单条操作的不透明定位符（服务端按它解析回邮箱）；**绝不**把邮箱编进 URL path */
  id: number | null;
  email: string;
  /** 服务端下发的遮罩展示串（唯一出口在 accounts_data._owner_display_of） */
  display: string;
  role: string;
  created_at: string;
  account_count: number;
  review_count: number;
}

export interface DeletedRecord {
  email: string;
  deleted_at: string;
  remaining_days: number;
  status: string;
}

export interface Badge {
  label: string;
  tone: string;
}

/** 分组归类：有待审核/已拒绝账号 → 待处理；否则无账号 → 空用户；否则正式。 */
export function groupOf(u: UserRecord): Exclude<Group, "deleted"> {
  if (u.review_count > 0) return "pending";
  return u.account_count === 0 ? "vacant" : "normal";
}

/** 某组的成员（已注销组走另一个数据源）。 */
export function listOf(users: UserRecord[], group: Group): UserRecord[] {
  if (group === "deleted") return [];
  return users.filter((u) => groupOf(u) === group);
}

/* ---------------- 载荷映射 ---------------- */

/** `/api/users` → 记录。**必须携带 id**（单条定位链路的起点，缺失时 ops 拒绝发请求）。 */
export function mapUsers(payload: unknown): { users: UserRecord[]; builtin: string } {
  const data = (payload ?? {}) as { users?: unknown[]; builtin_admin?: unknown };
  const users = (Array.isArray(data.users) ? data.users : []).map((raw) => {
    const u = (raw ?? {}) as Record<string, unknown>;
    return {
      id: u.id != null ? Number(u.id) : null,
      email: String(u.email ?? ""),
      display: String(u.display ?? ""),
      role: String(u.role ?? "user"),
      created_at: String(u.created_at ?? ""),
      account_count: Number(u.account_count) || 0,
      review_count: Number(u.review_count) || 0,
    };
  });
  return { users, builtin: String(data.builtin_admin ?? "admin") };
}

/** `/api/users/deleted` → 记录。 */
export function mapDeleted(payload: unknown): DeletedRecord[] {
  const data = (payload ?? {}) as { items?: unknown[] };
  return (Array.isArray(data.items) ? data.items : []).map((raw) => {
    const u = (raw ?? {}) as Record<string, unknown>;
    return {
      email: String(u.email ?? ""),
      deleted_at: String(u.deleted_at ?? ""),
      remaining_days: Number(u.remaining_days) || 0,
      status: String(u.status ?? "cooling"),
    };
  });
}

/* ---------------- 检索 ---------------- */

/**
 * 邮箱匹配：**完整值与遮罩值都参与**。
 *
 * 列表只渲染遮罩串（`abc***@x.com`），但用户手上只有完整邮箱——只匹配遮罩值会让"输入完整
 * 邮箱搜不到"（反之亦然）。两个形态都试，成本是两次 indexOf。
 */
export function matches(email: string, masked: string, kw: string): boolean {
  const q = String(kw ?? "").toLowerCase();
  if (!q) return true;
  return (
    String(email ?? "").toLowerCase().includes(q) ||
    String(masked ?? "").toLowerCase().includes(q)
  );
}

/** 组内计数文案：组名已表达分组语义，故只报数量（筛选时补"匹配 / 共"）。 */
export function countLabel(total: number, shown: number, kw: string): string {
  if (!total) return "";
  return kw ? `（${shown} 人匹配 / 共 ${total} 人）` : `（${total} 人）`;
}

/* ---------------- 展示 ---------------- */

export function roleBadge(role: string): Badge {
  return role === "admin" ? { label: "管理员", tone: "info" } : { label: "普通用户", tone: "muted" };
}

/** 已注销行的状态徽标：待清除（可立即清除）与冷却中（等到期）。 */
export function deletedBadge(status: string): Badge {
  return status === "purge_pending" ? { label: "待清除", tone: "bad" } : { label: "冷却中", tone: "warn" };
}

/** 冷却剩余文案：待清除状态没有"剩余"概念，给破折号而不是编一个天数。 */
export function remainText(remainingDays: number, status: string): string {
  if (status === "purge_pending") return "—";
  return remainingDays >= 1 ? `剩余 ${remainingDays} 天` : "不足一天";
}

/**
 * 窄屏补充信息（≤900px 时「待处理数 / 账号数 / 时间」三列被隐藏且本页无第二入口，
 * 故在邮箱下方补一行；由 CSS 断点控制 display，JS 不感知断点）。
 */
export function inlineMeta(u: UserRecord, group: Group): string {
  const parts: string[] = [];
  if (group === "pending" && u.review_count > 0) parts.push(`待处理 ${u.review_count}`);
  if ((group === "normal" || group === "vacant") && u.account_count > 0) parts.push(`账号 ${u.account_count}`);
  return parts.join(" · ");
}

/* ---------------- 行菜单 ---------------- */

export type ActionKey =
  | "grant_admin"
  | "revoke_admin"
  | "reset_password"
  | "clear_accounts"
  | "delete_user"
  | "purge";

export interface MenuAction {
  key: ActionKey;
  label: string;
  icon: string;
  danger?: boolean;
}

/**
 * 行操作菜单（**数据而非闭包**：页面按 key 派发，菜单本身可单测）。
 *
 * 目标为注册管理员且当前不是主管理员时，后端对 role/重置密码/清空账号/删除用户统一 403
 * （`web/app.py` 三个单条端点的 `role == "admin" and not is_master` 判定，以及
 * `/api/users/batch` 里对 reset_password/delete 的同口径软跳过），故这些动作一律不给出。
 * **UI 隐藏不是安全边界**：受门禁请求照旧由后端复核（凭据由 core.js 的受门禁提交 helper
 * 按后端 reason 补）。
 */
export function menuActions(
  u: Pick<UserRecord, "role">,
  group: Group,
  isMaster: boolean,
): MenuAction[] {
  const items: MenuAction[] = [];
  const isAdmin = u.role === "admin";
  if (isMaster && group === "normal") {
    items.push(
      isAdmin
        ? { key: "revoke_admin", label: "取消管理员", icon: "shield" }
        : { key: "grant_admin", label: "设为管理员", icon: "shield" },
    );
  }
  if (isAdmin && !isMaster) return items; // 非主管理员不可操作的注册管理员：不给后续动作
  items.push({ key: "reset_password", label: "重置密码", icon: "key" });
  if (group === "pending" || group === "normal") {
    items.push({ key: "clear_accounts", label: "清空账号", icon: "circle-slash" });
  }
  items.push({ key: "delete_user", label: "删除用户", icon: "trash", danger: true });
  return items;
}

/** 已注销组只有主管理员能清除（后端 403 兜底）；非主管理员行给说明文字而不是空菜单。 */
export function deletedActions(isMaster: boolean): MenuAction[] {
  return isMaster ? [{ key: "purge", label: "立即清除", icon: "trash", danger: true }] : [];
}

/* ---------------- 批量条 ---------------- */

export interface BatchAction {
  key: string;
  label: string;
  variant: string;
}

/** 批量动作集：已注销组只有"彻底清除"（且仅主管理员），其余组是重置密码 + 删除。 */
export function batchActions(group: Group): BatchAction[] {
  if (group === "deleted") {
    return [{ key: "purge", label: "彻底清除", variant: "btn--ghost btn--danger-ghost" }];
  }
  return [
    { key: "reset_password", label: "重置密码", variant: "btn--primary" },
    { key: "delete", label: "删除", variant: "btn--ghost btn--danger-ghost" },
  ];
}

/** 组定义（表格列、文案、可检索性）：四个组共用一套渲染，差异全在这张表里。 */
export interface GroupSpec {
  key: Group;
  title: string;
  sub: string;
  emptyText: string;
  emptyAction?: { label: string; tab: Group };
  searchable: boolean;
  /** 列定义（key 决定单元格渲染分支） */
  columns: { key: string; label: string; cls?: string; narrowHidden?: boolean }[];
  /** 计数列表头（正式/待处理组有，其余组没有） */
  countLabel?: string;
  /** 首行固定为内置主管理员（仅正式用户组） */
  showBuiltin?: boolean;
}

export const GROUPS: GroupSpec[] = [
  {
    key: "pending",
    title: "待处理用户",
    sub: "以下用户名下有待审核或已拒绝的易班账号。",
    emptyText: "暂无待处理用户",
    emptyAction: { label: "查看正式用户", tab: "normal" },
    searchable: true,
    countLabel: "待处理数",
    columns: [
      { key: "check", label: "" },
      { key: "mail", label: "邮箱" },
      { key: "role", label: "角色" },
      { key: "count", label: "待处理数", narrowHidden: true },
      { key: "time", label: "注册时间", narrowHidden: true },
      { key: "actions", label: "操作" },
    ],
  },
  {
    key: "normal",
    title: "正式用户",
    sub: "已提交账号且无待审核的用户；内置主管理员不可被修改。",
    emptyText: "暂无正式用户",
    emptyAction: { label: "查看空用户", tab: "vacant" },
    searchable: true,
    countLabel: "账号数",
    showBuiltin: true,
    columns: [
      { key: "check", label: "" },
      { key: "mail", label: "邮箱" },
      { key: "role", label: "角色" },
      { key: "count", label: "账号数", narrowHidden: true },
      { key: "time", label: "注册时间", narrowHidden: true },
      { key: "actions", label: "操作" },
    ],
  },
  {
    key: "vacant",
    title: "空用户",
    sub: "已注册但未提交任何账号。",
    emptyText: "暂无空用户",
    emptyAction: { label: "查看正式用户", tab: "normal" },
    searchable: true,
    columns: [
      { key: "check", label: "" },
      { key: "mail", label: "邮箱" },
      { key: "role", label: "角色" },
      { key: "time", label: "注册时间", narrowHidden: true },
      { key: "actions", label: "操作" },
    ],
  },
  {
    key: "deleted",
    title: "已注销用户",
    sub: "用户自助注销后进入 7 天冷却期，期内可由主管理员清除；到期系统自动清除。",
    emptyText: "暂无已注销用户",
    emptyAction: { label: "返回正式用户", tab: "normal" },
    searchable: false,
    columns: [
      { key: "check", label: "" },
      { key: "mail", label: "邮箱" },
      { key: "deleted_at", label: "注销时间", narrowHidden: true },
      { key: "remain", label: "剩余时间", narrowHidden: true },
      { key: "status", label: "状态" },
      { key: "actions", label: "操作" },
    ],
  },
];

export function groupSpec(group: Group): GroupSpec {
  const spec = GROUPS.find((g) => g.key === group);
  if (!spec) throw new Error(`未知分组：${group}`);
  return spec;
}
