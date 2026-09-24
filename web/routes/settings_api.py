# -*- coding: utf-8 -*-
# SPDX-License-Identifier: AGPL-3.0-only
"""设置与执行体域路由：系统开关、调度执行体清单、全站公告与更新日志。

**功能**
`GET/POST /api/settings` 系统设置读取与保存（档位口令门禁）；
`GET/PUT /api/scheduler/executors` 执行体数量与出口配置；
`PUT /api/scheduler/executors/workers/<index>`、`/fallback` 单段出口改写；
`POST /api/scheduler/executors/rows` 与 `PUT/DELETE /api/scheduler/executors/rows/<slot>`
执行体清单行的增删改；`POST /api/scheduler/executors/measure` 现场实测单账号耗时；
`GET/PUT /api/announcement` 与 `POST /api/announcement/publish` 公告草稿、读取与双人发布；
`GET /api/registration_paused` 注册暂停状态；`GET /api/changelog` 项目更新日志。

**归属**
`web.app.create_app` 的"设置 / 执行体 / 公告"面（管理端为主，公告与更新日志对外可读）。
工厂骨架、跨域中间件与库访问层仍留在 `web/app.py`。

**复用**
`register(app)` 供 `web.routes.register_all` 装配；`sensitive_password_gate()` 取回敏感
口令复核门禁（系统开关与执行体写落点的唯一入口，每 app 实例一份），
`admin_delete_limited()` 取回同族的窗口限速判定。

**通信**
视图体不直接读 web.app 的模块级名字，一律经 `web.routes.appmod()` 按属性取——测试用
`mock.patch.object(web.app, …)` 打桩（db / logger / clock / signin / yb_egress /
write_env_batch / read_env / ENV_FILE / _in_sign_window / edge_config 等）必须继续生效。
`.env` 写入一律经 `write_env_batch` 原子批写；被 `web.app` 的 `register_all(app)` 一次接入。
"""
import os
import time

from flask import jsonify, session

from web.routes import admin_delete_limited, sensitive_password_gate
from web.routes import appmod as _appmod
from web.services import signstatus as _signstatus
from yiban import window as yb_window


def _executor_write_guard(data, action, changed):
    """执行体写操作的口令复核（返回 None = 通过，否则是 `(响应, 状态码)`）。

    与 `POST /api/settings` 的系统开关**同一个门禁入口**（前端 88 号提示词要求别另立
    一套），本函数只剩两条落点特有的判断：

    - **只在"真的会改配置"时要求**（`changed=False` = 请求值与现值一致 → 不要求）：
      日常无变更的保存不该多一道口令；
    - **审计只落动作与槽位**：绝不记口令，也不记代理串（可能带凭据）与自定义名
      （用户输入，可能整串是敏感内容）。

    为什么执行体写操作要这道门：持被窃的主管理员会话（Cookie + CSRF）可以直接
    改出口、增删执行体、关掉兜底——而这恰恰是最容易造成**静默漏签**的一类配置。
    属"配置类"动作，故可被 TTL 豁免（刚复核过口令的同一出口不必再输一次）。
    """
    m = _appmod()
    if not changed:
        return None
    denied = sensitive_password_gate()(data, f"执行体写操作: {action}")
    if denied is not None:
        m.db.audit(session.get("username") or "?", "executors_pw_fail", "executors",
                 action)
        return denied
    return None


def _executor_change_alert(action, changed):
    """执行体写成功后的事后告警（非 `full` 档的管理员侧补偿信号）。

    为什么要有：非 `full` 档下执行体写不再当次索要口令，而改出口等于把全站签到流量
    交给任意代理（顺手还能把兜底关掉）——这一族写端点此前一处都不发告警，是受门禁
    操作里唯一没有补偿信号的一族（既无口令也无管理员侧痕迹，只剩审计链自己看自己）。
    `full` 档当次已要求口令，行为逐字不变：不重复发。

    只在**真的会改配置**时发（与 `_executor_write_guard` 同一判据）：无变更的保存不是
    "变更"，发了只会稀释同类告警。正文只落动作名与槽位（调用方给的 `action`，全是内部
    常量），过一道 `_nl_safe` 只为杜绝换行伪造告警正文；代理串可能带凭据、自定义名是
    用户输入，一律不进正文。
    """
    m = _appmod()
    if not changed or m._pw_gate_tier(m.ENV_FILE) == m.PW_GATE_FULL:
        return
    try:
        m.send_notification(
            "系统设置变更告警",
            m._change_mail(
                "执行体配置已变更。",
                detail=[("动作", m._nl_safe(action))],
                advice=["如非本人操作，请核对 .env 的执行体清单与出口并回滚"],
            ),
            urgent=True,
        )
    except Exception as e:  # 配置已落盘，告警失败不得把结果带崩成 500
        m.logger.warning("执行体变更告警发送失败（不影响已写入的配置）: %s", e)


def _reply_slot_egress(env_key, index):
    """单段出口写接口的公共实现（两个路由只差"哪一段"）。

    `index=None` 表示兜底；否则是第 index 个并行执行体。**两种模式同一契约**：

    - 清单存在（`YIBAN_EXECUTORS` 可解析）→ 只改清单里该行的出口，其余行逐字保留；
    - 清单缺失 → 走旧逗号列表/单键模式（`_save_slot_egress`），行为与升级前一致。

    请求体契约见两个路由的 docstring。**审计只落槽位名**（如 `YIBAN_PROXY_LIST[2]`
    或 `YIBAN_EXECUTORS[2]`），代理串可能带凭据，绝不进审计链。
    """
    m = _appmod()
    data = m._json_body()
    if "egress" not in data:
        # 缺键不给"什么都不改"的歧义：清空必须显式写 null 或空串
        return jsonify({"error": "缺少 egress 字段（该槽位要直连请显式传 null 或空串）"}), 400
    value, err = m._validated_proxy_value(data["egress"])
    if err:
        return jsonify({"error": err}), 400
    rows = m.yb_egress.parse_manifest(m.read_env(m.ENV_FILE).get(m.yb_egress.ENV_MANIFEST))
    if rows is not None:
        # "值真的变了"才要口令（与其他执行体写端点同一口径）：清单模式下比该行的现值，
        # 没有那一行（如兜底行尚未建立）也算要改。
        cur_row = (m.yb_egress.fallback_row(rows) if index is None
                   else m.yb_egress.row_by_slot(rows, index))
        cur_value = "" if cur_row is None else str(cur_row.get("proxy") or "")
        audit_detail = (f"{m.yb_egress.ENV_MANIFEST}[fallback]" if index is None
                        else f"{m.yb_egress.ENV_MANIFEST}[{index}]")
    else:
        env_now = m.read_env(m.ENV_FILE)
        role_now = m.yb_egress.ROLE_FALLBACK if index is None else m.yb_egress.ROLE_WORKER
        cur_value = str(m.yb_egress.resolve(role_now, index or 0, env=env_now) or "")
        audit_detail = env_key if index is None else f"{env_key}[{index}]"
    changed = value != cur_value
    denied = _executor_write_guard(data, f"{audit_detail} 改出口", changed)
    if denied:
        return denied
    if rows is not None:
        err, code = (m._save_fallback_egress(m.ENV_FILE, value) if index is None
                     else m._save_row_egress(m.ENV_FILE, index, value))
        # 清单模式下该行的出口**就是刚提交的值**（不经过旧键），故不能拿 resolve 读
        desc = m.yb_egress.describe(value)
    else:
        err, code = m._save_slot_egress(m.ENV_FILE, env_key, index, value)
        # 回"该槽位此刻生效的描述串"：与 GET 的 assignments[i].egress 同走
        # yiban.egress.resolve，故 PUT 之后 GET 读到的与这里回的**是同一个值**
        role = m.yb_egress.ROLE_FALLBACK if index is None else m.yb_egress.ROLE_WORKER
        desc = m.yb_egress.describe(
            m.yb_egress.resolve(role, index or 0, env=m.read_env(m.ENV_FILE)))
    if err:
        return jsonify({"error": err}), code
    m.db.audit(m._audit_actor(), "settings", "executors", audit_detail)
    _executor_change_alert(f"{audit_detail} 改出口", changed)
    return jsonify({"ok": True,
                    "index": index if index is not None else "fallback",
                    "egress": desc})


def api_settings():
    m = _appmod()
    env = m.read_env(m.ENV_FILE)
    mode = env.get("YIBAN_SIGN_MODE", "").strip().lower()
    sw = m._sign_window()
    # 窗口不可用（已回退默认）时把"配置异常、已按 X~Y 运行"暴露给设置页：那才是管理员
    # 会去修的地方，只写日志+邮件等于让他继续按错的窗口签到。文案与 /api/clock 同源
    # （`signstatus.window_fallback_text`），正常窗口为空串（响应逐字不变）。
    _fallback_text = _signstatus.window_fallback_text(m.sign_window_bounds())
    # 容量口径（与配额检查同源 `_capacity_account_count`）：
    #   用户 = 全部未删除注册用户（含尚未添加账号的空用户，仅注册名额口径）
    #   账号 = **会发起易班请求的账号**（非删除且审核态已通过，含 admin 直属裸账号）
    # 性能：单请求只读一次 users / 一次 accounts（raw，不解密）——逐项读同一请求
    # 要多次 AES-GCM 解密且长持 _conn_lock，2 核机上把 /api/settings 串行化到
    # ~13.5 rps。此处四处
    # 用途（计数/三分类/未通过审核数/owners 集合）都只用明文列，共享同一快照还
    # 消除并发下 _cur_accounts 与三分类求和不一致的可能。
    _users = m.db.load_users()
    _accts_raw = m.load_accounts_raw()
    _cap_users = len(_users)
    # 与 _capacity_account_count() 同口径，只是复用同一快照
    _cur_accounts = sum(1 for a in _accts_raw if m.db.account_signs_in(a))
    # 账号容量拆解（**纯展示**）：名额制下"停签/故障账号占满名额、
    # 新账号被拒但实际负载不高"是管理者的真实困惑，故展示分类计数。
    # 三桶互斥且求和 = _cur_accounts（= 计容量的账号数），优先级：
    # 用户自暂停 > 账密故障暂停 > 正常（同一账号两者都命中时归"用户自暂停"——
    # 那是用户主动行为，先说清楚"是他自己要停的"）。
    # 未通过审核的行**不属于任何一桶**（它们不计容量，见 accounts_audit），
    # 归进"正常"会让管理者以为名额被有效账号占满。
    _cred_paused = m._cred_paused_phones()
    _bd_normal = _bd_user_paused = _bd_cred_paused = _bd_audit = 0
    for _a in _accts_raw:
        if _a.get("deleted"):
            continue
        if not m.db.account_signs_in(_a):
            _bd_audit += 1
        elif _a.get("user_paused"):
            _bd_user_paused += 1
        elif str(_a.get("phone", "")) in _cred_paused:
            _bd_cred_paused += 1
        else:
            _bd_normal += 1
    # 潜在负载：已注册未提交人数（注册用户中尚无任何非删除账号者）
    _owners = {a.get("owner") for a in _accts_raw if not a["deleted"] and a.get("owner")}
    _potential = sum(1 for u in _users if u["email"] not in _owners)
    # gap 缺省取 DEFAULT_ACCOUNT_GAP_MAX（10），与设置页展示一致
    _est_start = m.load_env_int(m.ENV_FILE, "YIBAN_START_DELAY_MAX", 0)
    _est_gap = m.load_env_int(m.ENV_FILE, "YIBAN_ACCOUNT_GAP_MAX", m.DEFAULT_ACCOUNT_GAP_MAX)
    _est_accounts = m._capacity_estimate(_est_gap)
    return jsonify(
        {
            "ok": True,
            "start_delay_max": _est_start,
            "gap_max": _est_gap,
            "default_start_delay_max": m.DEFAULT_START_DELAY_MAX,
            "default_gap_max": m.DEFAULT_ACCOUNT_GAP_MAX,
            # 容量预估（单档口径）：签到容量按当前账号间隔与有效窗口
            # （已扣掐头去尾）估算；前端已用（或含潜在负载）超容量仅警示变色
            "capacity_estimate": {
                "accounts_cap": _est_accounts,
                "current_accounts": _cur_accounts,
                "potential_load": _potential,
            },
            # 签到模式：sequence（列表顺序，默认）/ random（列表随机打散）
            "sign_mode": mode or "sequence",
            # 调度 v2：排序×分布二级开关 + 首尾缓冲 + 自选总开关 + 窗口
            "sign_order": env.get("YIBAN_SIGN_ORDER", "").strip().lower() or (
                "random" if mode == "random" else "sequence"),
            "sign_dist": env.get("YIBAN_SIGN_DIST", "").strip().lower() or (
                "normal" if mode == "normal" else "uniform"),
            # 掐头去尾（前后独立，秒；window_edge_sec 兼容旧前端 = 前裁）
            "window_edge_sec": m.edge_config()[0],
            "edge_front_sec": m.edge_config()[0],
            "edge_back_sec": m.edge_config()[1],
            "allow_time_pref": m.load_env_int(m.ENV_FILE, "YIBAN_ALLOW_TIME_PREF", 0),
            "sign_window": f"{sw[0][0]:02d}:{sw[0][1]:02d} ~ {sw[1][0]:02d}:{sw[1][1]:02d}",
            # 窗口不可用（已回退默认）的可见提示；正常窗口时 false + 空串
            "window_fallback": bool(_fallback_text),
            "window_fallback_text": _fallback_text,
            # 容量状态：注册用户/计容量账号 当前使用量 vs 上限（管理员知情）
            "capacity": {
                "users": _cap_users,
                "users_max": m.load_env_int(m.ENV_FILE, "YIBAN_MAX_USERS", m.DEFAULT_MAX_USERS),
                "accounts": _cur_accounts,
                "accounts_max": m.load_env_int(m.ENV_FILE, "YIBAN_MAX_ACCOUNTS", m.DEFAULT_MAX_ACCOUNTS),
                # 账号容量拆解（仅展示）：三桶互斥、求和 = accounts。
                # 定义见 api_settings 顶部注释；配额判定与 accounts 同源。
                "accounts_breakdown": {
                    "normal": _bd_normal,
                    "user_paused": _bd_user_paused,
                    "cred_paused": _bd_cred_paused,
                },
                # 未通过审核、**不占容量**的账号数（与 accounts 互斥互补：
                # accounts + accounts_audit = 全部非删除账号）。前端据此解释
                # "账号管理里有很多行、容量却只算 N 个"。
                "accounts_audit": _bd_audit,
            },
            # 周日签到：1=开启（周日也尝试签到），0=关闭（默认）
            "sunday_sign": m.load_env_int(m.ENV_FILE, "YIBAN_SUNDAY_SIGN", 0),
            # 周六签到：1=开启（周六照常签到），0=关闭（默认，周六暂停）
            "saturday_sign": m.load_env_int(m.ENV_FILE, "YIBAN_SATURDAY_SIGN", 0),
            # 全局暂停（一键暂停签到）：1=暂停（下一轮 cron 跳过），0=正常
            "global_pause": m.load_env_int(m.ENV_FILE, "YIBAN_GLOBAL_PAUSE", 0),
            # 暂停注册：1=暂停（登录页关闭注册入口），0/未配置=允许
            "registration_pause": m.load_env_int(m.ENV_FILE, "YIBAN_REGISTRATION_PAUSE", 0),
            # 批量多选：前端会话级开关（不持久化，每次进入页面默认关闭）
            "batch_mode": False,
            # 注册账号验证 + 探针模式（任意管理员可改）
            "account_verify": 1 if env.get("YIBAN_ACCOUNT_VERIFY", "").strip().lower() in ("1", "true", "on", "yes") else 0,
            "probe_enable": 1 if env.get("YIBAN_PROBE_ENABLE", "").strip().lower() in ("1", "true", "on", "yes") else 0,
            "probe_time": env.get("YIBAN_PROBE_TIME", "20:00").strip() or "20:00",
            "probe_interval": env.get("YIBAN_PROBE_INTERVAL_DAYS", "1").strip() or "1",
        }
    )


def api_settings_save():
    m = _appmod()
    data = m._json_body()
    is_master = m._is_builtin_admin_session()
    # 档位判定读单源常量（见 MASTER_ONLY_KEYS 的定义处）：只看"键是否出现"、不看值，
    # 与普通管理员即便提交同值也无从改动这些键的既有 403 语义一致。
    # global_pause 是唯一例外——0→1「急停」任意管理员都能做，1→0「恢复签到」仍仅
    # 主管理员：能把全站停下去是止损，能放开来是权力。
    gp_req = None
    if m.GLOBAL_PAUSE_KEY in data:
        gp_req = 1 if m._env_flag(data.get(m.GLOBAL_PAUSE_KEY, "")) else 0
    wanted_a = set()
    if not is_master:
        wanted_a = set(m.MASTER_ONLY_KEYS.intersection(data))
        if gp_req == 0:
            wanted_a.add(m.GLOBAL_PAUSE_KEY)
    if wanted_a:
        return jsonify({"error": "仅主管理员可修改调度设置"}), 403
    # 字段携带才写——若缺省即 0 且无条件写两个键，
    # "只改周日开关"之类的部分更新会把已配置的延迟静默清零
    has_start = "start_delay_max" in data
    has_gap = "gap_max" in data
    try:
        start = int(data.get("start_delay_max", 0))
        gap = int(data.get("gap_max", 0))
    except (TypeError, ValueError):
        return jsonify({"error": "延迟秒数必须是整数"}), 400
    # 上限 1 小时：防止误填超大值破坏签到随机延迟
    start = min(max(start, 0), 3600)
    gap = min(max(gap, 0), 3600)
    # 随机延迟影响自动+手动签到节奏，且新设置预估容量不足（当前活跃账号
    # 超过预估值）时拒绝保存。口令复核按档位收在下面的统一门禁块里（不在这里各判一次），
    # 本段只留"就算口令对也不该落盘"的容量硬门。
    if has_start or has_gap:
        # 单门：容量口径收敛为活跃账号数（账号容量约束易班请求负载），
        # 注册用户多但活跃账号少不构成负载；存量站点瞬间显示超限仅警示，
        # 仅此处保存延迟时保留既有硬门
        # gap 未携带（部分更新只改启动延迟）时必须按 `.env` 现值算：用请求缺省的 0
        # 会把预估容量顶到窗口上限，硬门被"乐观上限"整个绕过。现值读法与 GET
        # /api/settings 的 gap_max 展示同源（0 是合法间隔，即不留间隔）。
        gate_gap = gap if has_gap else m.load_env_int(
            m.ENV_FILE, "YIBAN_ACCOUNT_GAP_MAX", m.DEFAULT_ACCOUNT_GAP_MAX)
        est_accounts = m._capacity_estimate(gate_gap)
        cur_accounts = m._capacity_account_count()
        if cur_accounts > est_accounts:
            return jsonify({
                "error": f"按新设置预估账号容量仅 {est_accounts} 个，当前活跃账号 {cur_accounts} 个，"
                         "保存被拒绝。请缩短账号间隔、延长签到窗口或清理不用的账号后再试"
            }), 400
    # 先全量校验、再统一写入——边校验边写时，
    # 后续字段非法返回 400 时前面的字段已落盘（"报错但设置变了"的部分写入）。
    # 签到模式（sequence/random）：写入 .env，cron 的 run.sh 加载后 signin.py 生效
    sign_mode = str(data.get("sign_mode", "")).strip().lower()
    if sign_mode and sign_mode not in ("sequence", "random"):
        return jsonify({"error": "签到模式取值应为 sequence 或 random"}), 400
    # 调度 v2：排序×分布二级开关（替代旧三选一，旧值自动映射兼容）
    sign_order = str(data.get("sign_order", "")).strip().lower()
    sign_dist = str(data.get("sign_dist", "")).strip().lower()
    if sign_order and sign_order not in ("sequence", "random"):
        return jsonify({"error": "排序方式取值应为 sequence 或 random"}), 400
    if sign_dist and sign_dist not in ("uniform", "normal"):
        return jsonify({"error": "分布方式取值应为 uniform 或 normal"}), 400
    # 掐头去尾（前后独立）：window_edge_sec 兼容旧前端（对称写）；
    # edge_front_sec / edge_back_sec 各自独立（秒，0~300，30 的倍数 = 0.5 分钟粒度）。
    # 0 是合法值（不裁切），不能用 write_env_int（其语义为 <=0 删除行）。
    edge_front = edge_back = None
    edge_raw = data.get("window_edge_sec")
    if edge_raw is not None:
        try:
            edge = int(edge_raw)
        except (TypeError, ValueError):
            return jsonify({"error": "首尾缓冲必须是整数秒"}), 400
        if not (0 <= edge <= 300 and edge % 30 == 0):
            return jsonify({"error": "首尾缓冲应为 0~300 秒且为 30 的倍数（0.5 分钟粒度）"}), 400
        edge_front = edge_back = edge
    for _key, _field in (("edge_front_sec", "front"), ("edge_back_sec", "back")):
        if _key in data:
            try:
                v = int(data[_key])
            except (TypeError, ValueError):
                return jsonify({"error": f"{_field}裁剪秒数必须是整数"}), 400
            if not (0 <= v <= 300 and v % 30 == 0):
                return jsonify({"error": f"{_field}裁剪应为 0~300 秒且为 30 的倍数（0.5 分钟粒度）"}), 400
            if _key == "edge_front_sec":
                edge_front = v
            else:
                edge_back = v
    # 用户自选总开关（0/1；0 同样需显式写入）
    pref_raw = data.get("allow_time_pref")
    pref = None
    if pref_raw is not None:
        pref = 1 if str(pref_raw).strip().lower() in ("1", "true", "on", "yes") else 0
    # 签到窗口（HH:MM，管理员可调；校验非法拒绝）
    win = str(data.get("sign_window", "")).strip()
    win_start_str = win_end_str = None
    if win:
        try:
            w_start, w_end = win.split("~")
            sh, sm = (int(x) for x in w_start.strip().split(":"))
            eh, em = (int(x) for x in w_end.strip().split(":"))
        except (ValueError, AttributeError):
            return jsonify({"error": "签到窗口格式应为 HH:MM ~ HH:MM"}), 400
        if not (0 <= sh <= 23 and 0 <= sm <= 59 and 0 <= eh <= 23 and 0 <= em <= 59 and (sh, sm) < (eh, em)):
            return jsonify({"error": "签到窗口非法（需 HH:MM 且开始早于结束）"}), 400
        win_start_str = f"{sh:02d}:{sm:02d}"
        win_end_str = f"{eh:02d}:{em:02d}"
    # 周日签到开关（1=开启/0=关闭）：仅请求携带时才更新，避免保存其他设置时误关
    sunday_sign = None
    if "sunday_sign" in data:
        sunday_sign = 1 if str(data.get("sunday_sign", "")).strip().lower() in ("1", "true", "on", "yes") else 0
    # 周六签到开关（1=开启/0=关闭）：仅请求携带时才更新，避免保存其他设置时误关
    saturday_sign = None
    if "saturday_sign" in data:
        saturday_sign = 1 if str(data.get("saturday_sign", "")).strip().lower() in ("1", "true", "on", "yes") else 0
    # 全局暂停（一键暂停签到）：0→1 急停任意管理员可做、1→0 恢复仅主管理员
    # （方向判定在档位门禁块里），下一轮 cron 生效。
    global_pause = gp_req
    # 暂停注册：A 档，仅主管理员可写（与签到窗口同权限口径）
    registration_pause = None
    if "registration_pause" in data:
        registration_pause = 1 if str(data.get("registration_pause", "")).strip().lower() in ("1", "true", "on", "yes") else 0
    # ---- 注册账号验证 + 探针模式（A 档：仅主管理员可改）----
    account_verify = None
    if "account_verify" in data:
        account_verify = 1 if str(data.get("account_verify", "")).strip().lower() in ("1", "true", "on", "yes") else 0
    probe_enable = None
    if "probe_enable" in data:
        probe_enable = 1 if str(data.get("probe_enable", "")).strip().lower() in ("1", "true", "on", "yes") else 0
    probe_time = None
    if "probe_time" in data:
        pt = str(data.get("probe_time", "")).strip()
        try:
            ph, pm = (int(x) for x in pt.split(":"))
        except (ValueError, AttributeError):
            return jsonify({"error": "探针触发时间应为 HH:MM 格式"}), 400
        if not (0 <= ph <= 23 and 0 <= pm <= 59):
            return jsonify({"error": "探针触发时间非法（需 HH:MM）"}), 400
        probe_time = f"{ph:02d}:{pm:02d}"
    probe_interval = None
    if "probe_interval" in data:
        pi = str(data.get("probe_interval", "")).strip()
        if pi.lower() == "once":
            probe_interval = "once"
        else:
            try:
                n = int(pi)
            except (TypeError, ValueError):
                return jsonify({"error": "探针触发频率应为正整数（每 N 天）或 once（单次）"}), 400
            if n <= 0:
                return jsonify({"error": "探针触发频率应为正整数（每 N 天）或 once（单次）"}), 400
            probe_interval = str(n)
    # 容量上限（主管理员专属；0=不限，钳位 0~100000）：
    # 字段携带才写——部分更新不得把未携带的 max_* 静默清零
    max_users_val = max_accounts_val = None
    for _key, _label in (("max_users", "用户容量上限"), ("max_accounts", "账号容量上限")):
        if _key not in data:
            continue
        try:
            v = int(data[_key])
        except (TypeError, ValueError):
            return jsonify({"error": f"{_label}必须是整数"}), 400
        if not (0 <= v <= 100000):
            return jsonify({"error": f"{_label}应为 0~100000（0=不限）"}), 400
        if _key == "max_users":
            max_users_val = v
        else:
            max_accounts_val = v
    # ---- 档位门禁：A/B 档的口令复核只在这一处判（档位表见 MASTER_ONLY_KEYS）----
    # 只带其中一个边缘键时另一侧保持现值——先按写侧同一口径补齐，否则"改了前裁、
    # 后裁跟着变"这件事在变更判定里是隐形的
    edge_note = ""
    if edge_front is not None or edge_back is not None:
        _cur_edge = m.edge_config()
        if edge_front is None:
            edge_front = _cur_edge[0]
        if edge_back is None:
            edge_back = _cur_edge[1]
        # 预防性夹取（夹取而非拒绝）：单边不超过窗口宽度的 20%。拒绝会让"已有超限配置
        # 的站点连别的字段都存不了"；夹到 20%（合计 40%）远小于运行时收缩阈值，
        # 故保存过的配置不会再让有效窗口被缓冲吃空。窗口以本次提交为准（改窗口时
        # 按新窗口算上限），未提交则按现值。
        if win_start_str is not None:
            _win_lo, _win_hi = (sh, sm), (eh, em)
        else:
            _win_lo, _win_hi = m._sign_window()
        _win_sec = ((_win_hi[0] * 60 + _win_hi[1]) - (_win_lo[0] * 60 + _win_lo[1])) * 60
        _cap = yb_window.edge_cap_sec(_win_sec)
        _edge_before = (edge_front, edge_back)
        edge_front, edge_back = min(edge_front, _cap), min(edge_back, _cap)
        if (edge_front, edge_back) != _edge_before:
            edge_note = (f"缓冲已按窗口宽度上限收缩为 前 {edge_front}s / 后 {edge_back}s"
                         f"（单边不超过窗口的 20%，避免有效窗口被裁剪吃空）")
    # 每个档位键 → 本次落盘后的**生效值**（None = 该键本次不写）。注意几处"删键≠0"：
    # gap 写 0 是删键、生效值回到默认，按 0 比会把"没改"当成"改了"（反之亦然）。
    proposed = {
        "start_delay_max": str(start) if has_start else None,
        "gap_max": str(gap if gap > 0 else m.DEFAULT_ACCOUNT_GAP_MAX) if has_gap else None,
        "sign_window": None if win_start_str is None else f"{win_start_str}~{win_end_str}",
        "edge_front_sec": None if edge_front is None else str(edge_front),
        "edge_back_sec": None if edge_back is None else str(edge_back),
        "window_edge_sec": (None if edge_front is None or edge_back is None
                            else f"{edge_front}/{edge_back}"),
        "allow_time_pref": None if pref is None else str(pref),
        "sign_mode": sign_mode or None,
        "sign_order": sign_order or None,
        "sign_dist": sign_dist or None,
        "sunday_sign": None if sunday_sign is None else str(sunday_sign),
        "saturday_sign": None if saturday_sign is None else str(saturday_sign),
        "global_pause": None if global_pause is None else str(global_pause),
        "registration_pause": None if registration_pause is None else str(registration_pause),
        "account_verify": None if account_verify is None else str(account_verify),
        "probe_enable": None if probe_enable is None else str(probe_enable),
        "probe_time": probe_time,
        "probe_interval": probe_interval,
        "max_users": None if max_users_val is None else str(max_users_val),
        "max_accounts": None if max_accounts_val is None else str(max_accounts_val),
    }
    cur_vals = m._settings_effective_values(m.ENV_FILE)
    # 真变化的档位键（旧值现读，绝不用请求自带的旧值——抄一份当前值即可自称"没改"）
    changes = []
    for _k in sorted(set(data).intersection(
            m.MASTER_ONLY_KEYS | m.GATED_KEYS | {m.GLOBAL_PAUSE_KEY})):
        _new, _old = proposed.get(_k), cur_vals.get(_k)
        if _new is not None and _new != _old:
            changes.append((_k, _old or "-", _new))
    a_changes = [c for c in changes if c[0] in m.MASTER_ONLY_KEYS]
    b_changes = [c for c in changes if c[0] in m.GATED_KEYS]
    pause_change = next((c for c in changes if c[0] == m.GLOBAL_PAUSE_KEY), None)

    def _tier_gate(action_label, always_required, attempted, irreversible=False):
        """档位口令门禁被拒时的统一处置：留痕 + 把响应交回调用方直接 return。

        `irreversible=True`（0→1 急停）在非 `full` 档还要求请求体带倒计时确认凭据；
        `always_required` 只在 `full` 档区分 A/B 档，其余档由门禁按档位自行决定
        （见 `_sensitive_password_gate`）。
        """
        denied = sensitive_password_gate()(data, action_label,
                                          always_required=always_required,
                                          irreversible=irreversible)
        if denied is not None:
            m.db.audit(
                session.get("username") or "?",
                "settings_switch_pw_fail",
                "settings",
                f"「{action_label}」口令复核未通过（尝试变更："
                + "、".join(f"{m._settings_label(k)}={o}→{n}" for k, o, n in attempted)
                + "）",
            )
        return denied

    if a_changes or pause_change:
        # A 档**不吃豁免**：豁免给的是"刚复核过的同一出口不必再输一次"，而 A 档
        # 要防的恰是持被窃会话者改一次配好手感、再连改全站停摆项。
        _action = "、".join(
            ([f"破坏性设置（{('、'.join(m._settings_label(k) for k, _, _ in a_changes))}）"]
             if a_changes else [])
            + (["系统开关"] if pause_change else []))
        denied = _tier_gate(_action, True,
                            (a_changes or []) + ([pause_change] if pause_change else []),
                            # 0→1 急停不可逆（当场把全站停下来）；1→0 恢复可逆
                            irreversible=bool(pause_change and pause_change[2] == "1"))
        if denied is not None:
            return denied
    if pause_change and pause_change[2] == "1" and admin_delete_limited()():
        # 急停与"删数据/拆报警器"同属一次点击即全站停摆，故共用同一套高危额度
        # （判定即占用，必须排在口令复核之后——否则不知口令者能用错口令刷光额度）。
        return jsonify({"error": "操作过于频繁，请稍后再试"}), 429
    if b_changes:
        denied = _tier_gate("调度配置", False, b_changes)
        if denied is not None:
            return denied
    # ---- 全部校验通过，批量原子写入（避免多次独立写导致配置不一致）----
    # 仅请求携带的字段才写入（缺失不重置）
    updates = {}
    if has_start:
        updates["YIBAN_START_DELAY_MAX"] = str(start) if start > 0 else ""
    if has_gap:
        updates["YIBAN_ACCOUNT_GAP_MAX"] = str(gap) if gap > 0 else ""
    if sign_mode:
        updates["YIBAN_SIGN_MODE"] = sign_mode
    if sign_order:
        updates["YIBAN_SIGN_ORDER"] = sign_order
    if sign_dist:
        updates["YIBAN_SIGN_DIST"] = sign_dist
    if edge_front is not None or edge_back is not None:
        # 写入新键并删除旧键（迁移）；未携带的一侧已在门禁块前按现值补齐
        updates["YIBAN_WINDOW_EDGE_FRONT_SEC"] = str(edge_front)
        updates["YIBAN_WINDOW_EDGE_BACK_SEC"] = str(edge_back)
        updates["YIBAN_WINDOW_EDGE_SEC"] = ""  # 旧键删除（前后对称语义已拆分为两键）
    if pref is not None:
        updates["YIBAN_ALLOW_TIME_PREF"] = str(pref)
    if win_start_str is not None:
        updates["YIBAN_SIGN_START"] = win_start_str
        updates["YIBAN_SIGN_END"] = win_end_str
    if sunday_sign is not None:
        updates["YIBAN_SUNDAY_SIGN"] = "1" if sunday_sign else ""
    if saturday_sign is not None:
        # 显式写 0/1（默认已是 0，显式落盘自文档化；不改写 sunday 的删键风格以保持各自历史口径）
        updates["YIBAN_SATURDAY_SIGN"] = "1" if saturday_sign else "0"
    if global_pause is not None:
        updates["YIBAN_GLOBAL_PAUSE"] = "1" if global_pause else ""
    if registration_pause is not None:
        # ""=开放（删键，读侧默认 0），与 global_pause 同口径；"1"=暂停
        updates["YIBAN_REGISTRATION_PAUSE"] = "1" if registration_pause else ""
    if account_verify is not None:
        updates["YIBAN_ACCOUNT_VERIFY"] = "1" if account_verify else ""
    if probe_enable is not None:
        updates["YIBAN_PROBE_ENABLE"] = "1" if probe_enable else ""
    if probe_time is not None:
        updates["YIBAN_PROBE_TIME"] = probe_time
    if probe_interval is not None:
        updates["YIBAN_PROBE_INTERVAL_DAYS"] = probe_interval
    if max_users_val is not None:
        # 0=不限须显式落盘 "0"（删键会回退默认 500/200，语义不同）
        updates["YIBAN_MAX_USERS"] = str(max_users_val)
    if max_accounts_val is not None:
        updates["YIBAN_MAX_ACCOUNTS"] = str(max_accounts_val)
    m.write_env_batch(m.ENV_FILE, updates)
    sunday_display = "不变" if sunday_sign is None else sunday_sign
    saturday_display = "不变" if saturday_sign is None else saturday_sign
    pause_display = "不变" if global_pause is None else ("暂停" if global_pause else "恢复")
    reg_pause_display = "不变" if registration_pause is None else ("暂停" if registration_pause else "开放")
    if edge_front is None and edge_back is None:
        edge_display = "不变"
    elif edge_front == edge_back:
        edge_display = f"各{edge_front / 60:g}分钟"
    else:
        edge_display = f"前{edge_front / 60:g}后{edge_back / 60:g}分钟"
    # 批量多选为前端会话级开关，不写入配置
    probe_display = "不变" if (probe_enable is None and probe_time is None and probe_interval is None) else \
        f"启={'1' if probe_enable else '0'}/时={probe_time or '-'}/频={probe_interval or '-'}"
    cap_limits_display = (
        f"用户={'不变' if max_users_val is None else max_users_val}"
        f"/账号={'不变' if max_accounts_val is None else max_accounts_val}"
    )
    # 真变化键的旧→新明细（一次请求只算一份，审计与告警共用同一串）
    changes_desc = "、".join(
        f"{m._settings_label(k)}={m._settings_value_text(k, o)}→{m._settings_value_text(k, n)}"
        for k, o, n in changes) or "无实质变更"
    m.logger.info(
        "更新设置: 启动=%s 间隔=%s 签到模式=%s 排序=%s 分布=%s 掐头去尾=%s 自选=%s 窗口=%s 周日=%s 周六=%s 暂停=%s 注册=%s 账号验证=%s 探针=%s 容量上限=%s",
        start, gap, sign_mode or "不变", sign_order or "不变", sign_dist or "不变",
        edge_display, pref_raw if pref_raw is not None else "不变",
        win or "不变", sunday_display, saturday_display, pause_display,
        reg_pause_display,
        "不变" if account_verify is None else ("开" if account_verify else "关"),
        probe_display, cap_limits_display,
    )
    # 设置变更审计（否则调度/系统设置保存无留痕，与其他管理操作不一致）
    m.db.audit(
        session.get("username") or "?",
        "settings_save",
        "settings",
        f"启动延迟={start} 间隔={gap} 模式={sign_mode or '-'} 排序={sign_order or '-'} "
        f"分布={sign_dist or '-'} 掐头去尾={edge_display} "
        f"自选={pref_raw if pref_raw is not None else '-'} 窗口={win or '-'} 周日={sunday_display} "
        f"周六={saturday_display} "
        f"全局暂停={pause_display} 注册={reg_pause_display} "
        f"账号验证={'开' if account_verify else '关'} "
        f"探针={probe_display} 容量上限={cap_limits_display} "
        f"变更=[{changes_desc}]",
    )
    # 变更告警：整次请求**合并成一条**（一键一封会被拿来刷告警日额度与邮箱）。
    # A 档/急停 → urgent（进手机推送），但只有"全停急停"才 force 跳过同类节流与
    # 推送日额度——它要立刻叫醒；其余 A 档变更沿用既有的同类节流与紧急账日额度。
    # 纯 B 档变更非紧急。值全部来自现读+本次落盘的配置项，不含凭据，仍过一道
    # _nl_safe 只为杜绝换行伪造告警正文。
    if changes:
        try:
            m.send_notification(
                "系统设置变更告警",
                m._change_mail(
                    "系统设置已变更。",
                    detail=[
                        (m._settings_label(k),
                         f"{m._nl_safe(m._settings_value_text(k, o))} → "
                         f"{m._nl_safe(m._settings_value_text(k, n))}")
                        for k, o, n in changes
                    ],
                    operator=str(session.get("username") or "?")[:64],
                    level="urgent" if (a_changes or pause_change is not None) else "info",
                ),
                urgent=bool(a_changes) or pause_change is not None,
                force=bool(pause_change and pause_change[2] == "1"),
            )
        except Exception as e:  # 配置已落盘，告警失败不得把结果带崩成 500
            m.logger.warning("设置变更告警发送失败（不影响已保存的配置）: %s", e)
    _saved_msg = "设置已保存（cron 下次触发自动生效）"
    if edge_note:
        # 被夹过就必须说清"夹到多少、为什么"：否则管理员看到滑块/输入框里的值
        # 与自己提交的不同，只会以为保存坏了
        _saved_msg = f"{_saved_msg}；{edge_note}"
    return jsonify({"ok": True, "msg": _saved_msg})


def api_changelog():
    """更新日志：读取项目根 CHANGELOG.md（公开，无需登录；启动后缓存，部署重启自然失效）。"""
    m = _appmod()
    if m._changelog_cache[0] is None:
        base = os.path.dirname(os.path.dirname(os.path.abspath(m.__file__)))
        path = os.path.join(base, "CHANGELOG.md")
        try:
            with open(path, encoding="utf-8") as f:
                m._changelog_cache[0] = f.read()
        except OSError:
            m._changelog_cache[0] = "暂无更新日志"
    return jsonify({"ok": True, "text": m._changelog_cache[0]})


def api_executors():
    """执行体与出口（多执行体形态的只读视图；**仅主管理员**可见）。

    前端要做"执行体配置"页时读这个接口即可，不必知道 .env 键名。字段说明：

    - `workers.configured`：当前配置的并行执行体数（`YIBAN_WORKERS`，0/未设=1）
    - `workers.assignments[]`：每个执行体的出口描述 + 角色标签（**已脱敏**，见下）
    - `fallback`：兜底常驻执行体的出口、扫描间隔、角色标签，以及
      `enabled`（.env 里声明的开关）/ `alive`（心跳判定是否真在跑）/
      `status`（四态：off / running / declared_not_running / running_not_declared）/
      `in_window`（当前是否在有效签到窗口内；窗口外 alive=false 属正常）
    - `activity`：当日**按执行体归属**的计数（谁做了多少），**已脱敏**
    - `executors[]`：**执行体清单**逐行（`{slot, type, egress, label, state, last_seen_at}`）。
      `type` 取 `worker` / `fallback` / `disabled`；`disabled` 行**保留出口、
      不参与分配、不拉起、不计入建议值**，故它**不报存活**（`state`/`last_seen_at`
      为 `null`，不是缺字段）；`fallback` 行的存活在 `fallback.*` 里（心跳口径不同），
      这两个字段同样为 `null`。只有 `worker` 行带存活四态。
    - `measured` / `recommendation`：容量建议（只有部署者实测过才有值，
      **建议值不是上限**；没实测就是 null，不编数字）

    清单与旧键的关系（迁移期）：`YIBAN_EXECUTORS`（单键 JSON 数组）优先；清单缺失时
    按旧三键（`YIBAN_WORKERS` / `YIBAN_PROXY_LIST` / `YIBAN_PROXY_FALLBACK`）读取。
    **首次读到旧键且清单缺失时**会一次性迁移写回清单键（旧键保留一个版本周期）。
    `workers.*` / `fallback.*` 的取值口径不变：`workers.configured` 只数 `worker` 行，
    `fallback.egress` 取清单里的兜底行（没有该行则继续按旧键解析）。

    脱敏：① 代理串可能带 `user:pass@`，一律只回 `scheme://host[:port]`；
    ② 执行体身份（`sign_claims.owner`）含主机名与进程号，**一律不回原串**——
    `activity.by_executor[].slot` 用 1-based 槽位序号替代它，角色来自
    `yiban.egress.parse_owner`（判不出即 `unknown`，照实回不猜）。
    """
    m = _appmod()
    if not m._is_builtin_admin_session():
        return jsonify({"error": "仅主管理员可查看执行体配置"}), 403
    env = m.read_env(m.ENV_FILE)
    # 执行体清单（唯一口径 yiban.egress）：清单缺失时按旧三键回退读，并在首次读到
    # 旧键时一次性迁移写回（旧键保留）。清单存在时**以清单为准**。
    rows = m._executor_rows()
    # `workers.configured` 只数 `worker` 行（disabled 与 fallback 都不计，
    # 与"建议值分母只数 worker"同一口径）；列表项 index 就是清单槽位号。
    active = m.yb_egress.worker_rows(rows)
    configured = max(1, len(active))
    if active:
        assignments = [
            {"index": r["slot"], "egress": m.yb_egress.describe(r["proxy"]),
             "role": m.yb_egress.ROLE_WORKER,
             "label": m.yb_egress.executor_label(m.yb_egress.TYPE_WORKER, r["slot"])}
            for r in active
        ]
    else:
        # 清单里没有并行执行体行（全被停用/删除）→ 回退旧口径的单执行体形态：
        # configured 按契约仍 ≥1，出口走 `single` 角色（= `YIBAN_PROXY`）——
        # 这正是这种情况下**实际运行**的单执行体用的出口（停用行的出口不参与分配）
        fallback_single = m.yb_egress.resolve(m.yb_egress.ROLE_SINGLE, 0, env=env)
        assignments = [{"index": 0, "egress": m.yb_egress.describe(fallback_single),
                        "role": m.yb_egress.ROLE_WORKER,
                        "label": m.yb_egress.role_label(m.yb_egress.ROLE_WORKER, 0)}]
    # 每个并行执行体的存活四态：后端算好，前端不必自己拼（也不用知道心跳周期）。
    # `last_seen_at` 是最后一次见到它活着的时间串；**不含 pid/主机名**。
    for item in assignments:
        item["state"], item["last_seen_at"] = m.signin.worker_presence(item["index"])
    # 兜底出口：清单里有兜底行就用它（值在迁移时已按旧口径落定）；没有该行
    # （被删除/停用）则继续按旧键解析，接口字段与旧口径保持一致。
    fb_row = m.yb_egress.fallback_row(rows)
    fallback_proxy = (fb_row["proxy"] if fb_row is not None
                      else m.yb_egress.resolve(m.yb_egress.ROLE_FALLBACK, env=env))
    # 窗口：`_executors_window()`（与引擎同一份解析：_sign_window + window.bounds）
    bounds = m._executors_window()
    measured = m.load_env_int(m.ENV_FILE, "YIBAN_CAPACITY_MEASURED", 0)
    cur_accounts = m._capacity_account_count()
    fallback_interval = m.load_env_int(m.ENV_FILE, "YIBAN_FALLBACK_INTERVAL", 60)
    # 是否在跑：按心跳新鲜度判定（进程被强杀时心跳会过期，故不能只看文件在不在）
    fallback_alive = m.signin.fallback_alive(fallback_interval)[0]
    # 声明的开关（.env 里网页写入的键）与"实际在跑"分开回，让前端能分辨
    # "声明了没跑起来"（要查 cron）与"没声明却在跑"（人工起的进程）
    fallback_enabled = m._env_flag(env.get("YIBAN_FALLBACK_ENABLE"))
    # `in_window` 用"本应运行时段"口径（窗口内 且 今天没被周末门/暂停门挡下）：
    # 兜底在这两种日子会直接退出，只按钟点算会让页面每逢周末/暂停就误报"开了没跑起来"
    in_window = m._in_run_period(bounds)
    if fallback_enabled:
        fallback_status = "running" if fallback_alive else "declared_not_running"
    else:
        fallback_status = "running_not_declared" if fallback_alive else "off"
    day = m.clock.today()
    by_executor, activity_totals = m._executor_activity(day)
    payload = {
        "ok": True,
        "workers": {
            "configured": configured,
            "assignments": assignments,
            "env_keys": {
                "list": m.yb_egress.ENV_WORKER_LIST,
                "single": m.yb_egress.ENV_SINGLE,
                # 清单键名一并给出：前端不硬编码字符串（与 env_keys 的用意一致）
                "manifest": m.yb_egress.ENV_MANIFEST,
            },
        },
        # 执行体清单逐行（含 disabled/fallback）：`workers.assignments` 只列会真正
        # 被拉起的并行执行体，停用行只有在这里才看得到（页面据此做"重新启用"）。
        "executors": [m._executor_row_payload(r) for r in rows],
        "fallback": {
            "egress": m.yb_egress.describe(fallback_proxy),
            "interval_sec": fallback_interval,
            "env_key": m.yb_egress.ENV_FALLBACK,
            "role": m.yb_egress.ROLE_FALLBACK,
            "label": m.yb_egress.role_label(m.yb_egress.ROLE_FALLBACK),
            "alive": fallback_alive,
            # 声明开关（读 .env）与四态 status 的键名一并给出，前端不必硬编码
            "enabled": fallback_enabled,
            "env_key_enable": "YIBAN_FALLBACK_ENABLE",
            "status": fallback_status,
            # 窗口外 alive=false 属正常：前端只该对 in_window=true 的
            # declared_not_running 报警，否则每天非签到时段都在误报
            "in_window": in_window,
        },
        "window": {
            "effective_sec": bounds.full_sec() if bounds else None,
            "start": f"{int(bounds.lo_min) // 60:02d}:{int(bounds.lo_min) % 60:02d}"
                     if bounds else None,
            "end": f"{int(bounds.hi_min) // 60:02d}:{int(bounds.hi_min) % 60:02d}"
                   if bounds else None,
        },
        "activity": {
            "day": day,
            "in_window": in_window,
            "by_executor": by_executor,
            "totals": activity_totals,
        },
        "measured": ({"per_executor_capacity": measured,
                      "source": "capacity_probe（部署者实测录入）",
                      "env_key": "YIBAN_CAPACITY_MEASURED"} if measured else None),
        "recommendation": None,
        "current_accounts": cur_accounts,
    }
    if measured:
        per_exec = max(1, int(measured * 2 / 3))
        payload["recommendation"] = {
            "per_executor_accounts": per_exec,
            "executors_needed": -(-cur_accounts // per_exec),
            "note": "实测容量 × 2/3 的建议值；这是建议，不是程序上限",
        }
    return jsonify(payload)


def api_scheduler_executors_save():
    """改执行体数量与出口配置（D-17：由主管理员手动调整；可部分提交）。

    **只写 `.env`，不重启也不拉起进程**——下一轮定时任务/容器重启后生效
    （与既有设置项同一语义，页面上要如实说明）。非法值一律 400 且不落盘。

    `fallback_enable` 只落盘这个开关；进程由部署形态各自拉起——宿主形态还要加一条
    cron（模板见 `scripts/yiban-fallback.sh` 头注释），容器形态由容器调度器在签到
    窗口内自动拉起。故页面上不能写成"打开即在跑"——接口回的 `fallback.status`
    才是"实际在不在跑"的判据。
    """
    m = _appmod()
    if not m._is_builtin_admin_session():
        return jsonify({"error": "仅主管理员可修改执行体设置"}), 403
    data = m._json_body()
    updates = {}
    if "workers" in data:
        try:
            workers = int(data["workers"])
        except (TypeError, ValueError):
            return jsonify({"error": "执行体数量必须是整数"}), 400
        if not (1 <= workers <= 64):
            return jsonify({"error": "执行体数量应为 1~64"}), 400
        updates["YIBAN_WORKERS"] = str(workers)
    for field, env_key in (("proxy_list", m.yb_egress.ENV_WORKER_LIST),
                           ("proxy_fallback", m.yb_egress.ENV_FALLBACK)):
        if field not in data:
            continue
        raw = str(data[field] or "").strip()
        if m.env_io.has_line_break(raw):
            return jsonify({"error": "代理配置不能包含换行"}), 400
        items = m.yb_egress.parse_list(raw) if field == "proxy_list" else [raw]
        for item in items:
            if item and not m._is_http_proxy_url(item):
                return jsonify({"error": f"代理地址格式不正确: {m._mask_url_userinfo(item)[:40]}"}), 400
        updates[env_key] = raw
    if "capacity_measured" in data:
        try:
            cap = int(data["capacity_measured"])
        except (TypeError, ValueError):
            return jsonify({"error": "实测容量必须是整数"}), 400
        if not (0 <= cap <= 100000):
            return jsonify({"error": "实测容量应为 0~100000（0 表示清除）"}), 400
        updates["YIBAN_CAPACITY_MEASURED"] = str(cap) if cap else ""
    if "fallback_enable" in data:
        # 白名单式解析：只接受 0/1/true/false（大小写不敏感，JSON 布尔与字符串
        # 都认）——这同时就是"拒绝换行"的校验，写不进去任何拼出来的行
        raw_flag = str(data["fallback_enable"]).strip().lower()
        if raw_flag not in ("0", "1", "true", "false"):
            return jsonify({"error": "兜底开关只接受 0/1/true/false"}), 400
        updates["YIBAN_FALLBACK_ENABLE"] = "1" if raw_flag in ("1", "true") else "0"
    if not updates:
        return jsonify({"error": "没有可更新的字段"}), 400
    try:
        with m._env_write_lock(m.ENV_FILE):
            current_env = m.read_env(m.ENV_FILE)
            # 清单已存在时同步维护它：旧键写入否则会被"以清单为准"的读接口盖过，
            # 表现为这次保存"点了没生效"。槽位保留（只增不复用），停用行不动。
            current_rows = m.yb_egress.parse_manifest(
                current_env.get(m.yb_egress.ENV_MANIFEST))
            if current_rows is not None and any(
                    k in updates for k in (m.yb_egress.ENV_WORKER_COUNT,
                                           m.yb_egress.ENV_WORKER_LIST,
                                           m.yb_egress.ENV_FALLBACK)):
                env_after = dict(current_env)
                env_after.update(updates)
                updates[m.yb_egress.ENV_MANIFEST] = m.yb_egress.dump_manifest(
                    m.yb_egress.apply_legacy_config(current_rows, env_after))
            # 口令门：**逐键比对"这次真会写下去的值"与文件里的现值**——值没变（或该键
            # 未携带）就不要求口令，日常保存零影响。比对放在写锁内、用同一份 current_env，
            # 免得"比的时候是一版、写的时候又一版"。失败时不写盘、提前返回（with 会释放锁）。
            changed_keys = sorted(k for k, v in updates.items()
                                  if str(current_env.get(k, "")) != str(v))
            denied = _executor_write_guard(
                data, "整条保存:" + ",".join(changed_keys)[:160], bool(changed_keys))
            if denied:
                return denied
            m.write_env_batch(m.ENV_FILE, updates)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    # 审计只记键名：代理串可能带凭据，不得进审计链
    m.db.audit(m._audit_actor(), "settings", "executors", ",".join(sorted(updates))[:200])
    _executor_change_alert("整条保存:" + ",".join(changed_keys)[:160], bool(changed_keys))
    return jsonify({"ok": True, "applied": sorted(updates),
                    "note": "已写入配置；下一轮定时任务或容器重启后生效"})


def api_scheduler_executor_worker_egress(index):
    """**只替换第 index 个并行执行体的出口段**（仅主管理员；CSRF 由 before_request 统一校验）。

    存在的理由：整条写入（`PUT /api/scheduler/executors` 的 `proxy_list`）收的是
    **整条**逗号列表，而读接口只回脱敏描述串（不含 userinfo）——前端拿读回的值整条
    回写，就会把别人段的代理凭据清成空、静默退回直连。本接口按序号只改一段，
    其余段**逐字保留**（不重新校验、不规范化、不排序），从接口形状上消灭这次数据破坏。

    请求体：`{"egress": "<代理串>"}` = 设置该段；`{"egress": null}` 或 `{"egress": ""}`
    = 该槽位直连；**缺 `egress` 键 → 400**（不给"什么都不改"的歧义）。
    槽位校验分两种模式：清单存在时该槽位必须在清单里；否则沿用旧口径
    （`0 <= index <= 63` 且 `< 当前执行体数 YIBAN_WORKERS`），不满足一律 400。

    响应：`{"ok": true, "index": <int>, "egress": "<脱敏描述串>"}`（不含 userinfo）。
    **只写 `.env`，不重启也不拉起进程**——下一轮定时任务或重启执行体后生效。
    """
    m = _appmod()
    if not m._is_builtin_admin_session():
        return jsonify({"error": "仅主管理员可修改执行体设置"}), 403
    manifest_rows = m.yb_egress.parse_manifest(
        m.read_env(m.ENV_FILE).get(m.yb_egress.ENV_MANIFEST))
    if manifest_rows is None:
        configured = max(1, m.load_env_int(m.ENV_FILE, "YIBAN_WORKERS", 1))
        if not (0 <= index <= m.EXECUTOR_INDEX_MAX) or index >= configured:
            return jsonify({"error": f"槽位 {index} 未被使用（当前执行体数 {configured}）"}), 400
    elif not (0 <= index <= m.EXECUTOR_INDEX_MAX) or \
            m.yb_egress.row_by_slot(manifest_rows, index) is None:
        return jsonify({"error": f"槽位 {index} 不在执行体清单里"}), 400
    return _reply_slot_egress(m.yb_egress.ENV_WORKER_LIST, index)


def api_scheduler_executor_fallback_egress():
    """**只替换兜底常驻执行体的那一段出口**（仅主管理员；CSRF 由 before_request 统一校验）。

    与 `PUT /api/scheduler/executors/workers/<index>` 同一契约、同一实现：只改
    `YIBAN_PROXY_FALLBACK` 这一段，其余键逐字不动，也就不会把并行执行体的出口表
    连带重写（同理，那条整条写入会把兜底段清空）。

    请求体：`{"egress": "<代理串>"}` = 设置；`{"egress": null}` 或 `{"egress": ""}`
    = 清掉该键，即"未单独配置兜底出口"——`resolve` 的既有语义是退回 `YIBAN_PROXY`，
    两者都没配就是直连（本接口不改这条语义，故响应回的是**该槽位此刻生效**的描述串）。
    **缺 `egress` 键 → 400**。响应：`{"ok": true, "index": "fallback", "egress": "…"}`。
    **只写 `.env`，不重启也不拉起进程**——下一轮定时任务或重启兜底执行体后生效。
    """
    m = _appmod()
    if not m._is_builtin_admin_session():
        return jsonify({"error": "仅主管理员可修改执行体设置"}), 403
    return _reply_slot_egress(m.yb_egress.ENV_FALLBACK, None)


def api_scheduler_executor_row_add():
    """**追加一行执行体**（仅主管理员；CSRF 由 before_request 统一校验）。

    槽位号 = **清单现有最大 + 1**，并**跳过保留期内真用过的号**（下标只增不复用：
    删中间行不重排；删掉当前最大行后，那个号若在领取历史里出现过就不会被再发一次。
    上限 63，满了 400）。
    请求体：`{"type": "worker"|"fallback"|"disabled", "proxy": "<代理串>",
    "name": "<自定义名>"}`，字段都可省（`type` 默认 `worker`；`proxy` 省/`null`/空串 =
    直连；`name` 省/`null`/空串 = 不设名，页面显示后端标签）。`fallback` 最多 1 行，
    已有则 400。响应：`{"ok": true, "slot": <int>, "type": ..., "egress": "<脱敏描述串>",
    "name": <自定义名或 null>}`。
    **追加一定会改配置，故必须带 `confirm_password`**（与系统开关同一条门：只比对
    不计数、失败 403 + 审计）；同规矩：**只写 `.env`，不重启也不拉起进程**；
    审计只落槽位名，不记凭据、不记自定义名。
    """
    m = _appmod()
    if not m._is_builtin_admin_session():
        return jsonify({"error": "仅主管理员可修改执行体设置"}), 403
    data = m._json_body()
    rtype = str(data.get("type") or m.yb_egress.TYPE_WORKER).strip()
    value, err = m._validated_proxy_value(data.get("proxy"))
    if err:
        return jsonify({"error": err}), 400
    name, err = m._validated_name(data.get("name"))
    if err:
        return jsonify({"error": err}), 400
    denied = _executor_write_guard(data, "追加执行体行", True)
    if denied:
        return denied

    def _apply(rows):
        slot = m._next_executor_slot(rows)
        return m.yb_egress.add_row(rows, rtype, value, min_slot=slot, name=name), slot

    try:
        slot = m._mutate_executor_rows(_apply)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    m.db.audit(m._audit_actor(), "settings", "executors", f"{m.yb_egress.ENV_MANIFEST}[{slot}]")
    _executor_change_alert("追加执行体行", True)
    return jsonify({"ok": True, "slot": slot, "type": rtype,
                    "egress": m.yb_egress.describe(value),
                    "name": name or None,
                    "note": "已写入配置；下一轮定时任务或容器重启后生效"})


def api_scheduler_executor_row_update(slot):
    """**改一行的类型/出口**（仅主管理员；CSRF 由 before_request 统一校验）。

    请求体：`{"type"?, "proxy"?, "name"?}`——字段缺席=不改；`proxy` 空串/null=直连；
    `name` 空串/null=清掉自定义名（回到后端标签）。三个键都不给 → 400（不给"什么都不改"
    的歧义）。改成 `disabled` 即"停用"：**出口保留**、不参与分配、不拉起、不计入建议值，
    且仍占槽位（不被复用）。槽位不存在 → 400。**其余行逐字保留**（与单段出口写接口同一
    纪律：只动目标行）。
    **口令门**：`type` 或 `proxy` **真的会变**时才要求 `confirm_password`（只改名或提交
    同值不要求——改名不改行为，日常保存不该多一道口令）；失败 403 + 审计，配置不动。
    响应：`{"ok": true, "slot": <int>, "type": ..., "egress": "<脱敏描述串>",
    "name": <自定义名或 null>}`。
    """
    m = _appmod()
    if not m._is_builtin_admin_session():
        return jsonify({"error": "仅主管理员可修改执行体设置"}), 403
    data = m._json_body()
    if "type" not in data and "proxy" not in data and "name" not in data:
        return jsonify({"error": "没有可更新的字段（type / proxy / name 至少给一个）"}), 400
    rtype = str(data["type"] or "").strip() if "type" in data else None
    value = None
    if "proxy" in data:
        value, err = m._validated_proxy_value(data["proxy"])
        if err:
            return jsonify({"error": err}), 400
    name = None
    if "name" in data:
        name, err = m._validated_name(data["name"])
        if err:
            return jsonify({"error": err}), 400
    # 现值（用于"真的会变吗"的判断）：读一次清单，找不到该行由写路径给 400。
    _cur = m.yb_egress.row_by_slot(m._executor_rows(), slot)
    _changed = _cur is None or (
        (rtype is not None and rtype != _cur["type"])
        or (value is not None and value != str(_cur.get("proxy") or ""))
    )
    denied = _executor_write_guard(data, f"{m.yb_egress.ENV_MANIFEST}[{slot}] 改行", _changed)
    if denied:
        return denied

    def _apply(rows):
        new_rows = m.yb_egress.update_row(rows, slot, rtype=rtype, proxy=value, name=name)
        return new_rows, m.yb_egress.row_by_slot(new_rows, slot)

    try:
        row = m._mutate_executor_rows(_apply)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    m.db.audit(m._audit_actor(), "settings", "executors", f"{m.yb_egress.ENV_MANIFEST}[{slot}]")
    _executor_change_alert(f"{m.yb_egress.ENV_MANIFEST}[{slot}] 改行", _changed)
    return jsonify({"ok": True, "slot": slot, "type": row["type"],
                    "egress": m.yb_egress.describe(row["proxy"]),
                    "name": row.get("name") or None,
                    "note": "已写入配置；下一轮定时任务或容器重启后生效"})


def api_scheduler_executor_row_delete(slot):
    """**删一行执行体**（仅主管理员；CSRF 由 before_request 统一校验）。

    删行**不重排**其余槽位（删中间行后新建的行拿到 `现有最大 + 1`，并跳过保留期内
    用过的号——见上面 POST 的说明）。要"留个位置以后可能还要用"就改成 `disabled`
    而不是删除。槽位不存在 → 400。
    **删行一定会改配置，故必须带 `confirm_password`**（与其他执行体写端点同一道门：
    只比对不计数、失败 403 + 审计，且此时行不会被删）。
    响应：`{"ok": true, "slot": <int>, "type": "<被删行的类型>", "deleted": true}`。
    """
    m = _appmod()
    if not m._is_builtin_admin_session():
        return jsonify({"error": "仅主管理员可修改执行体设置"}), 403
    data = m._json_body()
    denied = _executor_write_guard(data, f"{m.yb_egress.ENV_MANIFEST}[{slot}] 删行", True)
    if denied:
        return denied

    def _apply(rows):
        row = m.yb_egress.row_by_slot(rows, slot)
        if row is None:
            raise ValueError(f"槽位 {slot} 不在执行体清单里")
        return m.yb_egress.delete_row(rows, slot), row["type"]

    try:
        rtype = m._mutate_executor_rows(_apply)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    m.db.audit(m._audit_actor(), "settings", "executors", f"{m.yb_egress.ENV_MANIFEST}[{slot}]")
    _executor_change_alert(f"{m.yb_egress.ENV_MANIFEST}[{slot}] 删行", True)
    return jsonify({"ok": True, "slot": slot, "type": rtype, "deleted": True,
                    "note": "已写入配置；下一轮定时任务或容器重启后生效"})


def api_scheduler_executors_measure():
    """现场实测单账号耗时（**仅主管理员**；CSRF 由 before_request 统一校验）。

    **它会用真实账号访问易班一次**：等价于登录 + 拉取签到任务（只读路径，复用探针
    的 `verify_account`），**不提交签到**——不写当日签到状态、不动领取池、不写
    签到态事件。页面文案必须如实这么写。三重约束一个都不能省：

    1. **全局冷却**（`YIBAN_MEASURE_COOLDOWN`，默认 600s，落单条状态文件、跨进程
       有效）：冷却未到 → 429 + 剩余秒数。不按会话/IP 计——多管理员叠加点击就绕开了；
    2. **签到窗口内拒绝**（409）：避免抢当前轮次的资源与风控面；
    3. **脱敏**：响应与审计只出现打码手机号，绝不记完整号；实测结果**不落 `.env`**
       （不自动保存），前端只拿数字填输入框，用户确认后再提交。

    请求体：`{}` = 自动挑一个可签账号（**默认是列表里第一个**，见
    `_pick_measure_account`）；`{"phone": "<手机号>"}` = 指定账号（不可用/不存在 → 404）。
    响应结构与错误码见 `docs/dev/api-executors.md`。

    ⚠ **两次约束叠加后有个结构性偏差，页面与文档都必须说清**：
    本端点走的是"登录 + 拉任务"这条**只读链路**（5 次请求：登录 4 + 拉任务 1），
    而真实签到还要往下走**定位计算 + 提交签到**（6 次请求 + 一段 CPU 计算）；
    又因为窗口内被拒（409），**它永远只测得到窗口外的最小链路**——服务端在窗口外
    本来就没有可提交的任务。所以这里量出的秒数**天然偏小、据此换算的容量偏乐观**。
    本数与测试机基准（`scripts/loadtest/capacity_probe.py` 用假易班跑**完整链路**）
    **不可混用、不可比**：页面要提示"现场量的是窗口外粗值，正式容量请以测试机基准为准"。
    """
    m = _appmod()
    if not m._is_builtin_admin_session():
        return jsonify({"error": "仅主管理员可做耗时实测"}), 403
    bounds = m._executors_window()
    if m._in_sign_window(bounds):
        return jsonify({"error": "签到窗口内不做实测（避免抢占本轮资源）"}), 409
    data = m._json_body()
    acc = m._pick_measure_account(m.load_accounts(), data.get("phone"))
    if acc is None:
        return jsonify({"error": "没有可用于实测的账号（不存在、未过审、已删除或已被暂停）"}), 404

    sample = m._mask_phone(acc.get("phone", ""))
    cooldown_sec = m.load_env_int(m.ENV_FILE, "YIBAN_MEASURE_COOLDOWN", m.MEASURE_COOLDOWN_SEC)
    state_path = m._measure_state_path()
    # 冷却**先占位再联网**：连点/多管理员同时点也只有一次真实登录（占位在锁内
    # 判定并写入，跨进程串行）。代价是登录失败也消耗一次冷却——但这正是想要的：
    # 真实登录已经发生过，风控暴露已经产生。
    with m.signin._state_file_lock(state_path):
        remaining = m._measure_cooldown_remaining(m._read_measure_state(state_path),
                                                cooldown_sec)
        if remaining > 0:
            return jsonify({"error": "实测冷却中", "next_allowed_in": remaining}), 429
        started_at = m.clock.now()
        m._write_measure_state(state_path, {
            "at": started_at.strftime("%Y-%m-%d %H:%M:%S"),
            "seconds": None, "sample": sample,
        })

    started = time.monotonic()
    ok, message = m.signin.verify_account(m._as_signin_account(acc))
    seconds = time.monotonic() - started
    with m.signin._state_file_lock(state_path):
        m._write_measure_state(state_path, {
            "at": started_at.strftime("%Y-%m-%d %H:%M:%S"),
            "seconds": round(seconds, 2), "sample": sample,
        })
    if not ok:
        # 已脱敏（verify_account 的 message 过 sanitize_text）；换行折平防日志/页面注入
        reason = str(message).replace(chr(10), " ").replace(chr(13), " ")
        return jsonify({"error": f"实测失败：{reason}"}), 502

    # 容量**复用既有口径**：有效窗口用 _executors_window（= 页面显示的 window.effective_sec
    # 的那一份），单账号周期用实测秒数，间隔用 YIBAN_ACCOUNT_GAP_MAX。
    # k=1 钉住"实测**单执行体**容量"的字面语义：本接口量的是"这台机器一个执行体能签几个"，
    # 不是全站总容量（v3 下总容量 ≈ 该值 × 出口数）。公式按开关分派（缺省关时逐字同旧值）。
    per_exec = m.signin.capacity_of(
        bounds.full_sec(),
        gap=m.load_env_int(m.ENV_FILE, "YIBAN_ACCOUNT_GAP_MAX", m.DEFAULT_ACCOUNT_GAP_MAX),
        avg=seconds, k=1)
    # 建议值保留 ×2/3 余量：实测值是这台机器这一刻的成绩，留余量才对得上
    # "换机器/换网络都要重新量"的现实。
    recommended = max(1, int(per_exec * 2 / 3))
    m.db.audit(m._audit_actor(), "executors_measure", sample,
             f"实测单账号耗时 {seconds:.2f}s（单执行体容量 {per_exec}）")
    return jsonify({
        "ok": True,
        "seconds": round(seconds, 2),
        "sample": sample,
        "per_executor_capacity": per_exec,
        "recommended_per_executor": recommended,
        "cooldown_sec": cooldown_sec,
        "next_allowed_in": 0,
        # note 是给人看的说明（前端直接显示）。按前端答复：暂不加 scope 等
        # 机器可判字段——目前只有"窗口外粗量"这一种口径；将来出现第二种口径不同的实测
        # （如容器形态、带尾延迟的档位）时再加，届时前端会主动来要。
        "note": ("实测单账号耗时 × 有效窗口的容量估算；建议值含余量（×2/3）。"
                 "注意：本次只覆盖「窗口外的最小链路」（登录 + 拉任务，5 次请求），"
                 "真实签到还要加定位计算与提交（6 次请求），所以这里的秒数偏小、据此换算的"
                 "容量偏乐观；正式定档请以测试机基准为准，两种数字不要混用。"),
    })


def api_announcement():
    """公告读取：对外只给**已发布**文本；管理员会话额外看到待发布草稿三键。

    公开响应的形状是契约（登录页与全站顶横幅都取它，且未登录也要能读）：
    `{"ok": true, "text": <已发布>}` 两键一字不动。草稿刻意不进 `_announcement_cache`
    ——它按会话现读，缓存它等于把"尚不成立的事实"广播给所有匿名请求。
    """
    m = _appmod()
    env = None
    if m._announcement_cache[0] is None:
        env = m.read_env(m.ENV_FILE)
        m._announcement_cache[0] = env.get(m.ANNOUNCEMENT_KEY, "").strip()
    body = {"ok": True, "text": m._announcement_cache[0]}
    if m._current_role() == "admin":
        if env is None:
            env = m.read_env(m.ENV_FILE)
        draft_by, draft_at = m._parse_announcement_meta(
            env.get(m.ANNOUNCEMENT_DRAFT_META_KEY, ""))
        pub_by, pub_at = m._parse_announcement_meta(
            env.get(m.ANNOUNCEMENT_PUBLISHED_META_KEY, ""))
        body["draft"] = env.get(m.ANNOUNCEMENT_DRAFT_KEY, "").strip()
        body["draft_by"] = draft_by
        body["draft_at"] = draft_at
        body["published_by"] = pub_by
        body["published_at"] = pub_at
    return jsonify(body)


def api_registration_paused():
    """注册暂停状态（公开）：供登录页决定是否禁用注册入口。

    仅暴露一个布尔——暂停注册本就是面向访客的全局状态（提示文案公开显示），
    无敏感信息，无需鉴权。
    """
    m = _appmod()
    return jsonify({"paused": m._registration_paused()})


def api_announcement_save():
    """任意管理员写**草稿**（双人发布的前半程，不改变任何对外可见内容）。

    落点从 `YIBAN_ANNOUNCEMENT` 换成草稿两键：改前每一次 PUT 都立即对全体学生生效，
    一个只拿到注册管理员 Cookie 的人就能把伪造通知发成官方公告。校验逐条保留
    （200 字、行分隔符）——草稿与正式同格式单行存进 .env，注入面一分没小。
    """
    m = _appmod()
    data = m._json_body()
    text = str(data.get("text", "")).strip()
    if len(text) > 200:  # 后端长度限制（与前端 maxlength=200 一致）
        return jsonify({"error": "公告内容过长（最多 200 字）"}), 400
    if m._has_line_break(text):
        # 公告存入 .env 单行键值，换行会注入新配置行（如
        # YIBAN_ADMIN_PASSWORD_HASH），普通管理员即可借此提权为主管理员。
        # 前端为 textarea 但展示端换行本就折叠，直接拒绝而非剥掉（write_env_batch
        # 另有兜底）。判据单源在 _has_line_break：与 write_env_batch
        # 读写用的 splitlines() 同字符集，故 \v \f \x1c \x1d \x1e \x85 \u2028 \u2029
        # 一并拒——它们会作为"潜伏分隔符"被下一次读-改-写实体化成新配置行。
        return jsonify({"error": "公告内容不能包含换行或行分隔符（单行存储）"}), 400
    updates = {m.ANNOUNCEMENT_DRAFT_KEY: text}
    if text:
        author = str(session.get("username") or "?").strip().lower()[:64]
        meta = (f"{author}{m.ANNOUNCEMENT_DRAFT_META_SEP}"
                f"{m.clock.now().strftime(m.ANNOUNCEMENT_DRAFT_META_FMT)}")
        if m._has_line_break(meta):
            # 用户名走登录侧校验后不该带分隔符，这里是兜底：宁可拒绝保存，也不留
            # 一条作者被截断/污染的草稿——双人发布的全部价值就在于发布人看得见作者。
            return jsonify({"error": "草稿作者标识非法，未能保存"}), 400
        updates[m.ANNOUNCEMENT_DRAFT_META_KEY] = meta
    else:
        updates[m.ANNOUNCEMENT_DRAFT_META_KEY] = ""  # 空草稿 = 两键一起删
    # 一次批量写：正文与作者/时刻要么同时生效、要么都不生效（不存在"有草稿无作者"）
    m.write_env_batch(m.ENV_FILE, updates)
    m.db.audit(
        session.get("username") or "?",
        "announcement_draft_save",
        "announcement",
        f"草稿待发布｜{m._nl_safe(text[:150])}" if text else "草稿已清除",
    )
    m.logger.info("公告草稿已更新: %s", text[:50] or "（已清除）")
    # 草稿不改变对外可见内容，故沿用非紧急告警（紧急账每天只有几条，得留给真发布）
    m.send_notification(
        "公告变更告警",
        m._change_mail(
            f"公告草稿{'已更新' if text else '已清除'}"
            + ("，待主管理员发布。" if text else "。"),
            detail=([("公告内容", m._nl_safe(text[:80]))] if text else []),
            level="warn",
        ),
    )
    return jsonify({"ok": True,
                    "msg": ("草稿已保存，待主管理员发布" if text else
                            "草稿已清除（线上公告未变；清空草稿后点「发布」即可下线）")})


def api_announcement_publish():
    """双人发布的后半程：有草稿即发布，草稿为空则**下线**线上公告。

    仅主管理员 + **当次口令**（`always_required=True`，短时豁免不适用）：这一键改动
    会即时出现在全体学生的顶横幅与登录页上，是全站影响面最大的对外写操作。
    下线刻意复用本端点（用户裁决）：一收一发走同一条通道、权限档一致，避免"发布了
    错的却撤不下来，只能 SSH 改 .env 重启"。草稿与线上都为空时报 400 而不是静默成功
    ——那种点击多半是误按，报错比按钮亮一下什么也没发生更有信息量。
    响应：`{"ok": true, "msg": …, "text": <刚发布/清空后的文本>}`，前端据此刷新横幅。
    """
    m = _appmod()
    if not m._is_builtin_admin_session():
        return jsonify({"error": "仅主管理员可发布公告"}), 403
    denied = sensitive_password_gate()(m._json_body(), "发布全站公告", always_required=True)
    if denied is not None:
        return denied
    env = m.read_env(m.ENV_FILE)
    draft = env.get(m.ANNOUNCEMENT_DRAFT_KEY, "").strip()
    before = env.get(m.ANNOUNCEMENT_KEY, "").strip()
    who = str(session.get("username") or "?")
    now_ts = m.clock.now().strftime(m.ANNOUNCEMENT_DRAFT_META_FMT)
    if not draft:
        if not before:
            # 无事可做。刻意不"静默成功"：空草稿 + 空线上时点发布多半是误按，
            # 让它报错比让按钮亮一下什么也没发生更有信息量。
            return jsonify({"error": "当前既没有待发布草稿，线上也没有公告"}), 400
        # 空草稿 + 线上有内容 = 下线（用户裁决：不另设 clear 端点，一收一发走同一条
        # 双人发布的后半程，权限档完全一致——主管理员 + 当次口令）
        m.write_env_batch(m.ENV_FILE, {
            m.ANNOUNCEMENT_KEY: "",
            m.ANNOUNCEMENT_PUBLISHED_META_KEY: "",
            m.ANNOUNCEMENT_DRAFT_KEY: "",
            m.ANNOUNCEMENT_DRAFT_META_KEY: "",
        })
        m._announcement_cache[0] = ""
        m.db.audit(who, "announcement_publish", "announcement",
                 f"下线｜原: {m._nl_safe(before[:60])} → 新: （空）")
        m.logger.info("公告已下线: %s", before[:50])
        m.send_notification(
            "公告发布告警",
            m._change_mail(
                "全站公告已由主管理员下线。",
                detail=[("下线前", m._nl_safe(before[:80]))],
                operator=m._nl_safe(who),
                advice=["如非本人操作，请立即改主管理员口令并按"
                        "README「主管理员权限追回」处理"],
            ),
            urgent=True, force=True,
        )
        return jsonify({"ok": True, "msg": "线上公告已下线", "text": ""})
    if m._has_line_break(draft):
        # 解析器按行切，正常读不回带分隔符的值；会走到这里只可能是 .env 被手改成
        # 多行。那种内容没被任何人在 UI 上看过，一律拒绝发布而非照抄进正式键。
        return jsonify({"error": "草稿含行分隔符（.env 疑似被手工改动），未能发布"}), 400
    author, _at = m._parse_announcement_meta(
        env.get(m.ANNOUNCEMENT_DRAFT_META_KEY, ""))
    # 单批写入 = 一次读-改-写 + 一次原子替换：不存在"正式已被清、草稿还没落"的中间态
    m.write_env_batch(m.ENV_FILE, {
        m.ANNOUNCEMENT_KEY: draft,
        m.ANNOUNCEMENT_PUBLISHED_META_KEY: f"{who.strip().lower()}{m.ANNOUNCEMENT_DRAFT_META_SEP}{now_ts}",
        m.ANNOUNCEMENT_DRAFT_KEY: "",
        m.ANNOUNCEMENT_DRAFT_META_KEY: "",
    })
    m._announcement_cache[0] = draft  # 同步内存缓存（与 PUT 不同：这次真改了对外文本）
    change = "新发布" if not before else "覆盖发布"
    m.db.audit(
        who, "announcement_publish", "announcement",
        f"{change}｜原: {m._nl_safe(before[:60]) or '（空）'}"
        f" → 新: {m._nl_safe(draft[:60])}",
    )
    m.logger.info("公告已发布（%s）: %s", change, draft[:50])
    # 对外可见内容变了才走紧急 + force：被盗主管理员发布伪造公告是本系统最坏的
    # 单点，这条必须送达（同类节流会吞掉第二条，运维反而看不到）
    m.send_notification(
        "公告发布告警",
        m._change_mail(
            f"全站公告已由主管理员 {change}。",
            detail=[("草稿作者", m._nl_safe(author) or "（元数据不可用）"),
                    ("发布前", m._nl_safe(before[:80]) or "（空）"),
                    ("发布后", m._nl_safe(draft[:80]))],
            operator=m._nl_safe(who),
            advice=["如非本人操作，请立即改主管理员口令并按"
                    "README「主管理员权限追回」处理"],
        ),
        urgent=True, force=True,
    )
    return jsonify({"ok": True, "msg": "公告已发布", "text": draft})


def register(app):
    """在本域注册十五条设置/执行体/公告路由；endpoint 取函数名（url_for 依赖）。"""
    app.add_url_rule("/api/settings", view_func=api_settings)
    app.add_url_rule("/api/settings", view_func=api_settings_save, methods=["POST"])
    app.add_url_rule("/api/changelog", view_func=api_changelog)
    app.add_url_rule("/api/scheduler/executors", view_func=api_executors)
    app.add_url_rule("/api/scheduler/executors", view_func=api_scheduler_executors_save,
                     methods=["PUT"])
    app.add_url_rule("/api/scheduler/executors/workers/<int:index>",
                     view_func=api_scheduler_executor_worker_egress, methods=["PUT"])
    app.add_url_rule("/api/scheduler/executors/fallback",
                     view_func=api_scheduler_executor_fallback_egress, methods=["PUT"])
    app.add_url_rule("/api/scheduler/executors/rows",
                     view_func=api_scheduler_executor_row_add, methods=["POST"])
    app.add_url_rule("/api/scheduler/executors/rows/<int:slot>",
                     view_func=api_scheduler_executor_row_update, methods=["PUT"])
    app.add_url_rule("/api/scheduler/executors/rows/<int:slot>",
                     view_func=api_scheduler_executor_row_delete, methods=["DELETE"])
    app.add_url_rule("/api/scheduler/executors/measure",
                     view_func=api_scheduler_executors_measure, methods=["POST"])
    app.add_url_rule("/api/announcement", view_func=api_announcement)
    app.add_url_rule("/api/registration_paused", view_func=api_registration_paused)
    app.add_url_rule("/api/announcement", view_func=api_announcement_save, methods=["PUT"])
    app.add_url_rule("/api/announcement/publish", view_func=api_announcement_publish,
                     methods=["POST"])
