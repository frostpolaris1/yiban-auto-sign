import { describe, expect, it } from "vitest";
import {
  PRIVACY_FIELDS,
  buildPrivacyPayload,
  emptyPrivacyState,
  exposureHint,
  privacyConfirmBody,
  privacyDiff,
  privacyEndpoint,
  privacyFromView,
  privacyTitle,
  profileLine,
  validatePrivacy,
} from "./privacy";

/**
 * 二段资料（工单 qmz4）前端口径。
 *
 * 被测的真实用户动作：账号归属人在「编辑」里进二段，填写字段、拨开关，保存。
 * 断言的是用户可见结果与契约不变量，不是编舞：
 *   ① 默认全关：没有服务端下发 profile 时，五个开关全部关闭、五个输入全空；
 *   ② 视图只下发已暴露字段 ⇒ 关闭字段的既有内容在页面上不可见，保存时**不得**被
 *      空串冲掉（提交体只带用户改动过的键，子集合并语义）；
 *   ③ 提交体逐键对齐契约：profile/expose 都是子集，phone 为防错位令牌；
 *   ④ 暴露文案与 #201 实际语义一致：开＝本人视图＋管理详情可见，管理列表永不出现；
 *      关＝任何视图都不显示。
 */

const phone = "13800138001";

describe("默认全关", () => {
  it("没有 profile 下发时，五个字段全空、五个开关全关", () => {
    const state = privacyFromView(undefined);
    expect(state.profile).toEqual({
      real_name: "",
      student_id: "",
      college: "",
      class_name: "",
      email: "",
    });
    expect(state.expose).toEqual({
      real_name: false,
      student_id: false,
      college: false,
      class_name: false,
      email: false,
    });
    expect(emptyPrivacyState()).toEqual(state);
    expect(PRIVACY_FIELDS.map((f) => f.key)).toEqual([
      "real_name",
      "student_id",
      "college",
      "class_name",
      "email",
    ]);
  });

  it("键在 profile 里＝该字段已暴露；值为下发内容；缺键＝关闭且内容不可见", () => {
    const state = privacyFromView({ student_id: "20250001" });
    expect(state.profile.student_id).toBe("20250001");
    expect(state.expose.student_id).toBe(true);
    expect(state.expose.real_name).toBe(false);
    expect(state.profile.real_name).toBe("");
  });

  it("暴露但内容为空串：开关仍为开（键存在即开）", () => {
    const state = privacyFromView({ college: "" });
    expect(state.expose.college).toBe(true);
    expect(state.profile.college).toBe("");
  });
});

describe("保存只提交改动过的键（隐藏字段不得被空串冲掉）", () => {
  const initial = privacyFromView({ student_id: "20250001" });

  it("没有任何改动 → 无变更、无提交体", () => {
    const diff = privacyDiff(initial, initial);
    expect(diff.hasChanges).toBe(false);
    expect(diff.profile).toEqual({});
    expect(diff.expose).toEqual({});
    expect(diff.newlyExposed).toEqual([]);
    expect(buildPrivacyPayload(initial, initial, phone, { requirePhone: true })).toBeNull();
  });

  it("只改一个字段值 → profile 只带该键，expose 不带", () => {
    const state = privacyFromView({ student_id: "20250001" });
    state.profile.real_name = " 张三 ";
    const diff = privacyDiff(state, initial);
    expect(diff.profile).toEqual({ real_name: "张三" });
    expect(diff.expose).toEqual({});
    expect(diff.hasChanges).toBe(true);
    expect(buildPrivacyPayload(state, initial, phone, { requirePhone: true })).toEqual({
      profile: { real_name: "张三" },
      phone,
    });
  });

  it("只拨开关 → expose 只带该键，profile 不带", () => {
    const state = privacyFromView({ student_id: "20250001" });
    state.expose.class_name = true;
    const diff = privacyDiff(state, initial);
    expect(diff.profile).toEqual({});
    expect(diff.expose).toEqual({ class_name: true });
    expect(diff.newlyExposed).toEqual(["class_name"]);
    expect(buildPrivacyPayload(state, initial, phone, { requirePhone: true })).toEqual({
      expose: { class_name: true },
      phone,
    });
  });

  it("关闭已有字段 → expose 带 false；内容不随提交（保留库里值）", () => {
    const state = privacyFromView({ student_id: "20250001" });
    state.expose.student_id = false;
    const diff = privacyDiff(state, initial);
    expect(diff.expose).toEqual({ student_id: false });
    expect(diff.profile).toEqual({});
    expect(diff.newlyExposed).toEqual([]);
  });

  it("清空已暴露字段 → profile 带空串（显式清空）", () => {
    const state = privacyFromView({ student_id: "20250001" });
    state.profile.student_id = "";
    const diff = privacyDiff(state, initial);
    expect(diff.profile).toEqual({ student_id: "" });
  });

  it("修改隐藏字段的其他键时，隐藏键不出现在提交体里", () => {
    // initial 里 student_id 已暴露；real_name 未被下发（关闭且内容不可见）。
    const state = privacyFromView({ student_id: "20250001" });
    state.profile.class_name = "软件2401";
    const payload = buildPrivacyPayload(state, initial, phone, { requirePhone: true });
    expect(payload?.profile).toEqual({ class_name: "软件2401" });
    expect(payload && "real_name" in (payload.profile ?? {})).toBe(false);
    expect(payload && "student_id" in (payload.profile ?? {})).toBe(false);
  });
});

describe("提交体与端点", () => {
  it("管理面缺完整号 = 拒提交（fail-closed 对齐电话令牌门）", () => {
    const state = privacyFromView(undefined);
    state.profile.email = "a@b.c";
    expect(buildPrivacyPayload(state, emptyPrivacyState(), "", { requirePhone: true })).toEqual({
      error: "无法获取账号完整信息，请刷新页面后重试",
    });
  });

  it("本人面端点与管理面端点各自对齐契约", () => {
    expect(privacyEndpoint("my", 2)).toBe("/api/my-accounts/2/profile");
    expect(privacyEndpoint("admin", 7)).toBe("/api/accounts/7/profile");
  });

  it("长度上限与后端同口径（50；email 254）", () => {
    const state = privacyFromView(undefined);
    state.profile.real_name = "名".repeat(51);
    expect(validatePrivacy(state)).toContain("姓名");
    expect(validatePrivacy(state)).toContain("50");
    state.profile.real_name = "";
    state.profile.email = "a".repeat(255);
    expect(validatePrivacy(state)).toContain("邮箱");
    expect(validatePrivacy(state)).toContain("254");
    expect(validatePrivacy(emptyPrivacyState())).toBeNull();
  });
});

describe("暴露语义文案与 #201 实际口径一致", () => {
  it("开：本人视图与管理详情可见，管理列表永不出现", () => {
    const on = exposureHint(true, "user");
    expect(on).toContain("账号页");
    expect(on).toContain("管理详情");
    expect(on).toContain("管理列表始终不显示");
    expect(exposureHint(true, "admin")).toBe("开启：显示在账号详情；管理列表始终不显示。");
  });

  it("关：任何视图都不显示（管理详情也不显示，与管理端写口可维护不矛盾）", () => {
    for (const variant of ["user", "admin"] as const) {
      const off = exposureHint(false, variant);
      expect(off).toContain("任何列表或详情");
      expect(off).not.toContain("管理员除外");
    }
  });

  it("确认文案列出新增暴露字段；只关闭时不谎称新增暴露", () => {
    const state = privacyFromView(undefined);
    state.expose.real_name = true;
    state.expose.student_id = true;
    const body = privacyConfirmBody(privacyDiff(state, emptyPrivacyState()));
    expect(body).toContain("姓名");
    expect(body).toContain("学号");
    expect(body).toContain("管理列表");
    const offState = privacyFromView({ real_name: "张三" });
    offState.expose.real_name = false;
    expect(privacyConfirmBody(privacyDiff(offState, privacyFromView({ real_name: "张三" })))).not.toContain(
      "即将暴露",
    );
  });

  it("标题固定写明「二段：隐私资料与暴露」", () => {
    expect(privacyTitle()).toContain("二段：隐私资料与暴露");
  });
});

describe("我的视图展示行", () => {
  it("按词表顺序拼已暴露且有内容的字段", () => {
    expect(profileLine({ student_id: "20250001", real_name: "张三" })).toBe(
      "姓名：张三 · 学号：20250001",
    );
  });

  it("无暴露或无内容 → 空串（视图不出空行）", () => {
    expect(profileLine(undefined)).toBe("");
    expect(profileLine({})).toBe("");
    expect(profileLine({ real_name: "", student_id: "  " })).toBe("");
  });
});
