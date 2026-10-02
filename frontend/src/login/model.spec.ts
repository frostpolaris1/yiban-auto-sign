import { describe, expect, it } from "vitest";
import {
  AGREE_REQUIRED_MSG,
  AUTH_TITLES,
  EMAIL_FORMAT_MSG,
  EMAIL_LOCAL_TOO_LONG_MSG,
  EMAIL_RE,
  PW_MISMATCH_MSG,
  canSwitchTo,
  checkConfirm,
  checkEmail,
  checkPassword,
  landingPath,
  loginOutcome,
  networkErrorText,
  registerGate,
  restoreOutcome,
  type PasswordPolicy,
} from "./model";

/* 登录/注册口径单测。
   ⚠ 口令策略在真机上是 core.js 的实现（跨层对拍见 tests/test_rekey_key_source.py），
   这里用一个**同口径的测试替身**注入——本文件只测"拿策略做什么判断"，不测策略本身。 */

/** 与 core.js 同口径的替身：10 位、四类里至少两类。 */
const POLICY: PasswordPolicy = {
  minLen: 10,
  minClasses: 2,
  classes(v: string) {
    return [/[A-Z]/, /[a-z]/, /\d/, /[^A-Za-z0-9]/].filter((re) => re.test(v)).length;
  },
  hint: "至少 10 位，且包含大小写字母、数字、符号中的至少两类",
};

describe("邮箱校验", () => {
  it("空值不算错（提交由 required 兜底）", () => {
    expect(checkEmail("")).toEqual({ invalid: false, message: EMAIL_FORMAT_MSG });
    expect(checkEmail("   ").invalid).toBe(false);
  });

  it("合法邮箱放行，含常见形态", () => {
    for (const ok of ["a@b.co", "name@example.com", "a.b+c-d_e@sub.example.com.cn", "1@2.3"]) {
      expect(checkEmail(ok).invalid, ok).toBe(false);
    }
  });

  it("非法形态一律拦下（含缺域名、缺 @、含空格）", () => {
    for (const bad of ["a@b", "a@", "@b.com", "a b@c.com", "a@b.", "中文@example.com"]) {
      expect(checkEmail(bad).invalid, bad).toBe(true);
    }
  });

  it("本地部超长给**独立**文案（只说格式不正确会让人反复检查域名）", () => {
    const long = "a".repeat(33) + "@example.com";
    expect(checkEmail(long)).toEqual({ invalid: true, message: EMAIL_LOCAL_TOO_LONG_MSG });
    // 32 字符是边界内
    expect(checkEmail("a".repeat(32) + "@example.com").invalid).toBe(false);
  });

  it("整体超 64 也拦下（后端同口径）", () => {
    const v = "a".repeat(32) + "@" + "b".repeat(20) + ".com"; // 32 + 1 + 24 = 57，先确认长度
    expect(v.length).toBeLessThanOrEqual(64);
    expect(checkEmail(v).invalid).toBe(false);
    const tooLong = "a".repeat(32) + "@" + "b".repeat(28) + ".com"; // 32+1+32 = 65
    expect(tooLong.length).toBe(65);
    expect(checkEmail(tooLong).invalid).toBe(true);
  });

  it("正则本身与后端同形（@ 前限 32）", () => {
    expect(EMAIL_RE.test("a@b.co")).toBe(true);
    expect(EMAIL_RE.test("a@b")).toBe(false);
  });
});

describe("口令校验（策略注入）", () => {
  it("空值不算错，但提示里已给出当前计数", () => {
    const r = checkPassword("", POLICY);
    expect(r.invalid).toBe(false);
    expect(r.message).toBe(`密码${POLICY.hint}（当前 0 位，0 类）`);
  });

  it("长度不足或类别不足都算错，提示带实时计数", () => {
    expect(checkPassword("Ab1", POLICY).invalid).toBe(true);
    expect(checkPassword("abcdefghij", POLICY).invalid).toBe(true); // 10 位但单类
    const r = checkPassword("Abcdefg1", POLICY);
    expect(r.invalid).toBe(true);
    expect(r.message).toContain("（当前 8 位，3 类）");
  });

  it("达标放行（10 位两类是下界）", () => {
    expect(checkPassword("abcdefgh12", POLICY).invalid).toBe(false);
    expect(checkPassword("Abcdefghij", POLICY).invalid).toBe(false);
  });

  it("类别计数借的是注入的策略（不在此重写正则）", () => {
    const spy: PasswordPolicy = { ...POLICY, classes: () => 4 };
    expect(checkPassword("x", spy).message).toContain("4 类");
    expect(checkPassword("x", spy).invalid).toBe(true); // 长度仍不够
  });
});

describe("两次一致", () => {
  it("确认框为空时不提示（提交由 required 兜底）", () => {
    expect(checkConfirm("Abcdefgh12", "")).toEqual({ invalid: false, message: PW_MISMATCH_MSG });
  });

  it("非空即实时比对", () => {
    expect(checkConfirm("Abcdefgh12", "Abcdefgh12").invalid).toBe(false);
    expect(checkConfirm("Abcdefgh12", "Abcdefgh13").invalid).toBe(true);
  });
});

describe("注册提交闸门", () => {
  const base = { email: "a@b.co", password: "Abcdefgh12", confirm: "Abcdefgh12", agree: true, policy: POLICY };

  it("全部合规放行", () => {
    const g = registerGate(base);
    expect(g.blocked).toBe(false);
    expect(g.topMessage).toBe("");
  });

  it("未勾选协议：挡下并**弹顶部框**（不对应任何单个字段）", () => {
    const g = registerGate({ ...base, agree: false });
    expect(g.blocked).toBe(true);
    expect(g.topMessage).toBe(AGREE_REQUIRED_MSG);
  });

  it("字段不合规：只标字段，不弹顶部框（避免重复提示）", () => {
    for (const over of [{ email: "bad" }, { password: "abc" }, { confirm: "nope" }]) {
      const g = registerGate({ ...base, ...over });
      expect(g.blocked).toBe(true);
      expect(g.topMessage).toBe("");
    }
  });

  it("未勾选 + 字段也不合规：仍以协议文案为准（先说明最上层原因）", () => {
    const g = registerGate({ ...base, agree: false, email: "bad" });
    expect(g.topMessage).toBe(AGREE_REQUIRED_MSG);
    expect(g.email.invalid).toBe(true);
  });
});

describe("响应分支", () => {
  it("recoverable 必须先于 ok 判断（否则恢复入口成死代码）", () => {
    const out = loginOutcome({ ok: true, recoverable: true, msg: "账号已注销，7 天内可恢复" });
    expect(out.kind).toBe("recoverable");
    expect(out).toMatchObject({ message: "账号已注销，7 天内可恢复" });
  });

  it("recoverable 缺 msg 时给默认文案", () => {
    expect(loginOutcome({ recoverable: true })).toEqual({
      kind: "recoverable",
      message: "账号已注销，7 天内可恢复",
    });
  });

  it("成功按角色分流落点", () => {
    expect(loginOutcome({ ok: true, role: "admin" })).toEqual({ kind: "ok", path: "/data/dashboard" });
    expect(loginOutcome({ ok: true, role: "user" })).toEqual({ kind: "ok", path: "/user/calendar" });
    // 角色缺失/未知一律回用户端（含注册即登录，不带 role）
    expect(loginOutcome({ ok: true })).toEqual({ kind: "ok", path: "/user/calendar" });
  });

  it("失败取后端 error，缺省给固定文案", () => {
    expect(loginOutcome({ error: "密码错误" })).toEqual({ kind: "error", message: "密码错误" });
    expect(loginOutcome(null)).toEqual({ kind: "error", message: "登录失败，请重试" });
    expect(loginOutcome(undefined).kind).toBe("error");
  });

  it("恢复：成功分流，失败用恢复专属文案", () => {
    expect(restoreOutcome({ ok: true, role: "user" })).toEqual({ kind: "ok", path: "/user/calendar" });
    expect(restoreOutcome({ error: "凭据不正确" })).toEqual({ kind: "error", message: "凭据不正确" });
    expect(restoreOutcome({})).toEqual({ kind: "error", message: "恢复失败，请稍后再试" });
  });

  it("落点函数可单独用（注册即登录走它）", () => {
    expect(landingPath("admin")).toBe("/data/dashboard");
    expect(landingPath(undefined)).toBe("/user/calendar");
  });

  it("网络异常文案：有 message 用 message，否则固定句", () => {
    expect(networkErrorText(new Error("请求失败（500）"))).toBe("请求失败（500）");
    expect(networkErrorText(null)).toBe("网络异常，请检查网络后重试");
  });
});

describe("页签", () => {
  it("标题随模式切换（避免出现「标题说登录、表单是注册」）", () => {
    expect(AUTH_TITLES.login[0]).toBe("登录");
    expect(AUTH_TITLES.register[0]).toBe("注册");
  });

  it("注册暂停时不得切入注册（键盘/脚本路径的兜底）", () => {
    expect(canSwitchTo("register", true)).toBe(false);
    expect(canSwitchTo("register", false)).toBe(true);
    expect(canSwitchTo("login", true)).toBe(true); // 暂停注册不影响回登录
  });
});
