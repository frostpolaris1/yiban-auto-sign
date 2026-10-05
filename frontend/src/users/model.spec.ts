import { describe, expect, it } from "vitest";
import {
  GROUPS,
  batchActions,
  countLabel,
  deletedActions,
  deletedBadge,
  groupOf,
  groupSpec,
  inlineMeta,
  listOf,
  mapDeleted,
  mapUsers,
  matches,
  menuActions,
  remainText,
  roleBadge,
  type UserRecord,
} from "./model";

/* 用户管理页口径单测。
   ⚠ 写操作链路（MF-49 出口面：单条端点按不透明 id 编 path、邮箱只进 batch/purge 请求体、
   id 缺失拒绝发请求）**不在本文件**——它由 tests/test_users_exit_surface_frontend.py 把
   ops.js 整段放进 node 真跑钉住（本文件只测页面侧的判断）。 */

function user(over: Partial<UserRecord> = {}): UserRecord {
  return {
    id: 1,
    email: "a@test.local",
    display: "a***",
    role: "user",
    created_at: "2026-10-01 08:00",
    account_count: 0,
    review_count: 0,
    ...over,
  };
}

describe("分组归类", () => {
  it("有待审核/已拒绝 → 待处理（优先于账号数）", () => {
    expect(groupOf(user({ review_count: 2, account_count: 5 }))).toBe("pending");
    expect(groupOf(user({ review_count: 1, account_count: 0 }))).toBe("pending");
  });

  it("无待审核且无账号 → 空用户；有账号 → 正式", () => {
    expect(groupOf(user({ account_count: 0 }))).toBe("vacant");
    expect(groupOf(user({ account_count: 3 }))).toBe("normal");
  });

  it("listOf 按归类筛选，已注销组走另一数据源（返回空）", () => {
    const users = [
      user({ id: 1, review_count: 1 }),
      user({ id: 2, account_count: 2 }),
      user({ id: 3, account_count: 0 }),
    ];
    expect(listOf(users, "pending").map((u) => u.id)).toEqual([1]);
    expect(listOf(users, "normal").map((u) => u.id)).toEqual([2]);
    expect(listOf(users, "vacant").map((u) => u.id)).toEqual([3]);
    expect(listOf(users, "deleted")).toEqual([]);
  });
});

describe("载荷映射", () => {
  it("记录必须携带 id（单条定位链路的起点）与遮罩 display", () => {
    const { users, builtin } = mapUsers({
      users: [
        { id: 42, email: "trail-user@test.local", display: "tra***", role: "user", account_count: 1 },
        { id: 7, email: "b@q.com", display: "***", role: "admin", review_count: 3 },
      ],
      builtin_admin: "admin",
    });
    expect(builtin).toBe("admin");
    expect(users[0]).toMatchObject({ id: 42, email: "trail-user@test.local", display: "tra***" });
    expect(users[1]).toMatchObject({ id: 7, role: "admin", review_count: 3 });
  });

  it("id 缺失时保留 null（而不是伪造 0）——ops 会因此拒绝发请求", () => {
    const { users } = mapUsers({ users: [{ email: "noid@test.local" }] });
    expect(users[0].id).toBeNull();
    expect(users[0].role).toBe("user");
    expect(users[0].account_count).toBe(0);
  });

  it("缺字段/空载荷不抛（接口降级时页面仍可渲染空表）", () => {
    for (const p of [null, undefined, {}, { users: null }, { users: [null] }]) {
      const { users, builtin } = mapUsers(p);
      expect(builtin).toBe("admin");
      expect(Array.isArray(users)).toBe(true);
    }
    expect(mapDeleted(null)).toEqual([]);
    expect(mapDeleted({ items: [null] })[0]).toMatchObject({ status: "cooling", remaining_days: 0 });
  });

  it("已注销映射带剩余天数与状态", () => {
    const [d] = mapDeleted({ items: [{ email: "x@y.z", deleted_at: "2026-10-01", remaining_days: 6, status: "purge_pending" }] });
    expect(d).toEqual({ email: "x@y.z", deleted_at: "2026-10-01", remaining_days: 6, status: "purge_pending" });
  });
});

describe("检索", () => {
  it("完整邮箱与遮罩值都参与匹配（列表只渲染遮罩，用户手上只有完整值）", () => {
    expect(matches("13800000000@qq.com", "138****0000", "13800000000")).toBe(true);
    expect(matches("13800000000@qq.com", "138****0000", "138****0000")).toBe(true);
    expect(matches("alice@qq.com", "ali***", "ALI")).toBe(true);
  });

  it("空关键词全通过；不匹配则 false", () => {
    expect(matches("a@b.c", "a***", "")).toBe(true);
    expect(matches("a@b.c", "a***", "zzz")).toBe(false);
  });

  it("计数文案：组名已表达语义，故只报数量；筛选时补「匹配 / 共」", () => {
    expect(countLabel(0, 0, "")).toBe("");
    expect(countLabel(5, 5, "")).toBe("（5 人）");
    expect(countLabel(5, 2, "ali")).toBe("（2 人匹配 / 共 5 人）");
  });
});

describe("展示", () => {
  it("角色徽标：管理员 info / 其余 muted", () => {
    expect(roleBadge("admin")).toEqual({ label: "管理员", tone: "info" });
    expect(roleBadge("user")).toEqual({ label: "普通用户", tone: "muted" });
  });

  it("已注销徽标：待清除 bad / 冷却中 warn", () => {
    expect(deletedBadge("purge_pending").tone).toBe("bad");
    expect(deletedBadge("cooling").tone).toBe("warn");
  });

  it("剩余文案：待清除不编天数（给破折号）", () => {
    expect(remainText(0, "purge_pending")).toBe("—");
    expect(remainText(6, "cooling")).toBe("剩余 6 天");
    expect(remainText(0, "cooling")).toBe("不足一天");
  });

  it("窄屏补充信息：只在有内容时出现，且不含邮箱", () => {
    expect(inlineMeta(user({ review_count: 2 }), "pending")).toBe("待处理 2");
    expect(inlineMeta(user({ account_count: 3 }), "normal")).toBe("账号 3");
    expect(inlineMeta(user({ account_count: 3 }), "vacant")).toBe("账号 3");
    expect(inlineMeta(user({ account_count: 0 }), "vacant")).toBe("");
    expect(inlineMeta(user({ review_count: 1 }), "normal")).toBe("");
  });
});

describe("行菜单（数据而非闭包）", () => {
  it("主管理员在正式组可切换管理员身份；其余组不给", () => {
    const keys = menuActions({ role: "user" }, "normal", true).map((a) => a.key);
    expect(keys).toContain("grant_admin");
    expect(menuActions({ role: "admin" }, "normal", true).map((a) => a.key)).toContain("revoke_admin");
    expect(menuActions({ role: "user" }, "pending", true).map((a) => a.key)).not.toContain("grant_admin");
  });

  it("清空账号只出现在待处理/正式组", () => {
    expect(menuActions({ role: "user" }, "pending", true).map((a) => a.key)).toContain("clear_accounts");
    expect(menuActions({ role: "user" }, "normal", true).map((a) => a.key)).toContain("clear_accounts");
    expect(menuActions({ role: "user" }, "vacant", true).map((a) => a.key)).not.toContain("clear_accounts");
  });

  it("非主管理员对注册管理员目标：不给任何动作（后端 403 兜底，UI 只是不给出不可能成功的动作）", () => {
    expect(menuActions({ role: "admin" }, "normal", false)).toEqual([]);
    expect(menuActions({ role: "admin" }, "pending", false)).toEqual([]);
  });

  it("非主管理员对普通用户：仍有重置密码与删除用户", () => {
    const keys = menuActions({ role: "user" }, "normal", false).map((a) => a.key);
    expect(keys).toEqual(["reset_password", "clear_accounts", "delete_user"]);
  });

  it("删除用户始终带 danger 标记（走不可逆确认链）", () => {
    const del = menuActions({ role: "user" }, "normal", true).find((a) => a.key === "delete_user");
    expect(del?.danger).toBe(true);
  });

  it("已注销组只有主管理员能清除", () => {
    expect(deletedActions(true).map((a) => a.key)).toEqual(["purge"]);
    expect(deletedActions(false)).toEqual([]);
  });
});

describe("批量动作集", () => {
  it("已注销组只有彻底清除；其余组是重置密码 + 删除", () => {
    expect(batchActions("deleted").map((a) => a.key)).toEqual(["purge"]);
    expect(batchActions("pending").map((a) => a.key)).toEqual(["reset_password", "delete"]);
    expect(batchActions("normal").map((a) => a.key)).toEqual(["reset_password", "delete"]);
    expect(batchActions("vacant").map((a) => a.key)).toEqual(["reset_password", "delete"]);
  });
});

describe("分组定义表", () => {
  it("四组齐全，键唯一", () => {
    expect(GROUPS.map((g) => g.key)).toEqual(["pending", "normal", "vacant", "deleted"]);
    expect(groupSpec("normal").title).toBe("正式用户");
    expect(() => groupSpec("nope" as never)).toThrow();
  });

  it("只有正式组有内置主管理员行；只有已注销组不可检索", () => {
    expect(GROUPS.filter((g) => g.showBuiltin).map((g) => g.key)).toEqual(["normal"]);
    expect(GROUPS.filter((g) => !g.searchable).map((g) => g.key)).toEqual(["deleted"]);
  });

  it("列定义含检查列与操作列（渲染分支靠 key）", () => {
    for (const g of GROUPS) {
      const keys = g.columns.map((c) => c.key);
      expect(keys[0]).toBe("check");
      expect(keys[keys.length - 1]).toBe("actions");
    }
    expect(groupSpec("pending").columns.map((c) => c.key)).toContain("count");
    expect(groupSpec("vacant").columns.map((c) => c.key)).not.toContain("count");
    expect(groupSpec("deleted").columns.map((c) => c.key)).toEqual([
      "check", "mail", "deleted_at", "remain", "status", "actions",
    ]);
  });

  it("空态出口指向同页另一分组（不新增页面跳转）", () => {
    for (const g of GROUPS) {
      if (g.emptyAction) expect(GROUPS.map((x) => x.key)).toContain(g.emptyAction.tab);
    }
  });
});
