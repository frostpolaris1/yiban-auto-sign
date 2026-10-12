import { describe, expect, it } from "vitest";
import {
  CODE_CLEAR,
  CREDS_GATE_DESC,
  FORM_TEXTS,
  MISSING_DETAIL,
  availableUserItems,
  buildAccountPayload,
  credsWritten,
  emailBaseItems,
  formSubtitle,
  formTexts,
  formTitle,
  isMaskedPhone,
  makeSnapshot,
  resolvePhone,
  submitLabel,
  type AccountFormInput,
} from "./accountform";

function input(over: Partial<AccountFormInput> = {}): AccountFormInput {
  return {
    name: "A1",
    phoneVisible: "13800138001",
    password: "pw",
    phoneModel: "",
    phoneCode: "",
    clearCode: false,
    ...over,
  };
}

describe("resolvePhone（完整号只经内存暂存；打码号无内存值即拒提交）", () => {
  it("打码号 + 内存有完整号 → 用内存值（不回显）", () => {
    expect(resolvePhone("138****8001", "13800138001")).toEqual({ value: "13800138001" });
  });

  it("打码号 + 内存无值 → 拒提交（不让用户手填猜）", () => {
    expect(resolvePhone("138****8001", null)).toEqual({ error: MISSING_DETAIL });
    expect(resolvePhone("138****8001", "")).toEqual({ error: MISSING_DETAIL });
  });

  it("未打码 → 用输入值；空值 → 必填提示", () => {
    expect(resolvePhone(" 13900139002 ", null)).toEqual({ value: "13900139002" });
    expect(resolvePhone("   ", null)).toEqual({ error: "请填写易班手机号" });
    expect(isMaskedPhone("138****8001")).toBe(true);
    expect(isMaskedPhone("13800138001")).toBe(false);
  });
});

describe("buildAccountPayload（校验顺序与文案）", () => {
  it("新增：缺密码先拦（顺序在手机号之后）", () => {
    expect(buildAccountPayload(input({ password: "" }), { editing: false })).toEqual({
      error: "请填写易班密码",
      focus: "password",
    });
  });

  it("要求快照的编辑态：无快照即拒（不进入字段校验）", () => {
    expect(
      buildAccountPayload(input({ phoneVisible: "138****8001" }), { editing: true, requiresSnapshot: true, snapshot: null }),
    ).toEqual({ error: MISSING_DETAIL });
  });

  it("不需要快照的编辑态（用户端改自己的账号）：照常提交", () => {
    const r = buildAccountPayload(input(), { editing: true, requiresSnapshot: false });
    expect(r.payload).toEqual({
      name: "A1",
      phone: "13800138001",
      password: "pw",
      phone_model: "",
      phone_code: "",
    });
  });

  it("编辑态允许空密码（留空=不改密码），并带上快照指纹", () => {
    const snap = makeSnapshot({ name: "A1", phone: "13800138001", status: "active" });
    const r = buildAccountPayload(input({ password: "" }), { editing: true, requiresSnapshot: true, snapshot: snap });
    expect(r.payload?.password).toBe("");
    expect(r.payload?._snapshot).toBe(snap);
  });

  it("清除识别码：phone_code 用哨兵值；否则用输入值（含 trim）", () => {
    expect(buildAccountPayload(input({ clearCode: true, phoneCode: "abc" }), { editing: true }).payload?.phone_code).toBe(
      CODE_CLEAR,
    );
    expect(buildAccountPayload(input({ phoneCode: "  deadbeef  " }), { editing: true }).payload?.phone_code).toBe("deadbeef");
  });

  it("绑定用户（新增）：选已注册用户 → 带 email，不带手填校验", () => {
    const r = buildAccountPayload(input({ email: "u@example.com" }), {
      editing: false,
      allowEmail: true,
      policyOk: () => false, // 非手填分支不查口令策略
    });
    expect(r.payload?.email).toBe("u@example.com");
    expect(r.payload?.initial_password).toBe("");
  });

  it("手填邮箱分支：缺邮箱 / 口令不合策略各自拦下（策略判定注入）", () => {
    const opts = { editing: false, allowEmail: true, policyOk: (pw: string) => pw.length >= 10, policyHint: "至少 10 位" };
    expect(buildAccountPayload(input({ email: "__manual__", manualEmail: "" }), opts)).toEqual({
      error: "请填写邮箱",
      focus: "manualEmail",
    });
    expect(buildAccountPayload(input({ email: "__manual__", manualEmail: "a@b.c", initialPassword: "short" }), opts)).toEqual(
      { error: "初始密码至少 10 位", focus: "initialPassword" },
    );
    const ok = buildAccountPayload(
      input({ email: "__manual__", manualEmail: " a@b.c ", initialPassword: "longenough1" }),
      opts,
    );
    expect(ok.payload?.email).toBe("a@b.c");
    expect(ok.payload?.initial_password).toBe("longenough1");
  });

  it("不绑定（空 email）：不写 email 键，但仍按契约带空 initial_password", () => {
    const r = buildAccountPayload(input({ email: "" }), { editing: false, allowEmail: true });
    expect(r.payload && "email" in r.payload).toBe(false);
    expect(r.payload?.initial_password).toBe("");
  });
});

describe("credsWritten（口径 3：只有写密码或改绑手机号才进门禁）", () => {
  const snap = makeSnapshot({ name: "A1", phone: "13800138001", status: "active" });

  it("新增永远不算改写凭据", () => {
    expect(credsWritten({ password: "x", phone: "1" }, { editing: false })).toBe(false);
  });

  it("编辑：写了非空密码 → 是", () => {
    expect(credsWritten({ password: "newpw", phone: "13800138001" }, { editing: true, snapshot: snap })).toBe(true);
  });

  it("编辑：改绑手机号 → 是；同号 → 不是", () => {
    expect(credsWritten({ password: "", phone: "13900139002" }, { editing: true, snapshot: snap })).toBe(true);
    expect(credsWritten({ password: "", phone: "13800138001" }, { editing: true, snapshot: snap })).toBe(false);
  });

  it("编辑：快照缺失时判不出改绑，退化为只看密码", () => {
    expect(credsWritten({ password: "", phone: "13900139002" }, { editing: true, snapshot: null })).toBe(false);
    expect(credsWritten({ password: "p", phone: "13900139002" }, { editing: true, snapshot: null })).toBe(true);
  });

  it("快照 JSON 损坏时不抛错（按判不出处理）", () => {
    expect(credsWritten({ password: "", phone: "1" }, { editing: true, snapshot: "{not json" })).toBe(false);
  });

  it("空白密码不算写了密码（trim 后判空）", () => {
    expect(credsWritten({ password: "   ", phone: "13800138001" }, { editing: true, snapshot: snap })).toBe(false);
  });

  it("门禁说明是该行为的固定文案", () => {
    expect(CREDS_GATE_DESC).toContain("请输入当前管理员密码确认");
  });
});

describe("绑定用户下拉项（只列无账号用户；文本用服务端遮罩 display）", () => {
  it("固定条目 + 分组头在未加载时也在（用户抢先打开不会见空面板）", () => {
    expect(emailBaseItems()).toEqual([
      { v: "", t: "不绑定（管理员自有账号）" },
      { v: "__manual__", t: "手填邮箱（未注册）" },
      { group: "已注册用户（无账号）" },
    ]);
  });

  it("过滤掉已有账号的用户；value 是完整邮箱、文本是遮罩 display", () => {
    const items = availableUserItems([
      { email: "a@x.com", display: "a***@x.com", account_count: 0 },
      { email: "b@x.com", display: "b***@x.com", account_count: 2 },
    ]);
    expect(items).toEqual([
      { v: "", t: "不绑定（管理员自有账号）" },
      { v: "__manual__", t: "手填邮箱（未注册）" },
      { group: "已注册用户（无账号）" },
      { v: "a@x.com", t: "a***@x.com" },
    ]);
  });

  it("没有可绑定用户时给不可选空态行", () => {
    expect(availableUserItems([])).toEqual([
      { v: "", t: "不绑定（管理员自有账号）" },
      { v: "__manual__", t: "手填邮箱（未注册）" },
      { group: "已注册用户（无账号）" },
      { empty: "（暂无）" },
    ]);
  });
});

describe("标题/副标题/主按钮文案（两变体 × 新增/编辑）", () => {
  it("标题", () => {
    expect(formTitle({ editing: false, variant: "user", index: null })).toBe("提交我的易班账号");
    expect(formTitle({ editing: true, variant: "user", index: 0 })).toBe("编辑我的易班账号");
    expect(formTitle({ editing: false, variant: "admin", index: null })).toBe("添加账号");
    expect(formTitle({ editing: true, variant: "admin", index: 2 })).toBe("编辑账号 #3");
  });

  it("副标题", () => {
    expect(formSubtitle({ editing: false, variant: "user" })).toBe("提交后等待管理员审核，通过即自动签到。");
    expect(formSubtitle({ editing: true, variant: "admin" })).toBe("手机号已打码显示；不改动则按原号提交。");
  });

  it("主按钮", () => {
    expect(submitLabel({ editing: true, variant: "admin" })).toBe("保存修改");
    expect(submitLabel({ editing: false, variant: "user" })).toBe("提交账号");
    expect(submitLabel({ editing: false, variant: "admin" })).toBe("添加账号");
  });
});

describe("口令字段文案（两套凭据必须读得出区别）", () => {
  // 本站登录密码与易班口令是两套凭据：本字段改的是**易班**口令，且刻意不做本地策略校验，
  // 标签一旦丢掉限定词，管理员会把错误口令一路存下去（生产已发生过：用户至今登不上）。
  it("标签带「易班」限定词，两变体都不省", () => {
    expect(formTexts("user").passwordLabel).toBe("易班密码");
    expect(formTexts("admin").passwordLabel).toBe("易班密码");
  });

  it("两变体都有一行区分说明，且写明不是本站登录密码", () => {
    for (const variant of ["user", "admin"] as const) {
      expect(FORM_TEXTS[variant].passwordHelp).toContain("易班");
      expect(FORM_TEXTS[variant].passwordHelp).toContain("本站");
    }
  });
});

describe("占位符必须写成真实形状（不得用说明式）", () => {
  // 说明式占位（"如：我的易班账号"）实测被用户照抄当名称提交；占位符要放一个形状真实的
  // 示例，且必须带「如：」前缀让人读得出"这是示例"。
  it("两个变体的名称占位符都不是说明式，且可辨识为示例", () => {
    for (const variant of ["user", "admin"] as const) {
      const ph = FORM_TEXTS[variant].namePlaceholder;
      expect(ph).not.toContain("我的易班账号");
      expect(ph.startsWith("如：")).toBe(true);
      expect(ph).toContain("123");
    }
  });
});
