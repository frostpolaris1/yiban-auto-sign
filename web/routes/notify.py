# -*- coding: utf-8 -*-
# SPDX-License-Identifier: AGPL-3.0-only
"""邮件通知配置域路由：邮件/SMTP 配置读写与推送通道配置读写、推送自测。

**功能**
`GET` / `PUT /api/mail-config` 读写邮件告警开关、SMTP 发信条目列表（主备 failover）与
告警收件人；`GET` / `PUT /api/notify-config` 读写消息推送（Server酱/自定义 URL）配置、
密钥、节流与两本每日额度；`POST /api/notify-test` 发一条测试消息验证推送配置。

**归属**
`web.app.create_app` 的"告警通道配置面"。工厂骨架、跨域中间件（前置限速、登录守卫、
CSRF、同源校验、安全响应头）与共享安全门实现仍留在 `web/app.py`，本模块只提供五条视图。

**复用**
`register(app)` 供 `web.routes.register_all` 装配。`web.routes.high_risk_gate()` /
`reconfirm_admin_password()` 取回留在 `web.app.create_app` 里的门禁闭包（工厂局部不可从
模块侧面取到，create_app 按 app 实例登记在 extensions）。

**通信**
视图体不直接读 web.app 的模块级名字，一律经 `web.routes.appmod()` 按属性取——测试用
`mock.patch.object(web.app, …)` 打桩（ENV_FILE / mailer / notify / mail_config /
account_crypto / write_env_batch / send_notification / db / _json_body 等）必须继续生效。
配置落盘走 `write_env_batch` 一次原子写；变更告警经 `send_notification` 发出。
"""
import json

from flask import jsonify, session

from web.routes import appmod as _appmod
from web.routes import high_risk_gate as _high_risk_gate
from web.routes import reconfirm_admin_password as _reconfirm_admin_password


def api_mail_config():
    """邮件通知配置状态（脱敏：授权码不回显，地址打码），供管理后台显示。

    smtps：SMTP 发信条目列表（mailer.smtp_list 解密结果；pass 绝不回显，
    仅以 has_pass 标记该条是否已有授权码；user 同顶层字段口径经
    mail_config._mask_addr 打码——发件账号也属敏感地址，编辑时留空即沿用）。
    条目级 admin_to 不参与序列化：发送路径只读顶层旧键 ADMIN_TO，条目
    携带的收件人从不生效，不再序列化/落盘。顶层 admin_to 是活字段（A 线告警
    收件算法的唯一来源，见 mail_config.admin_recipients），保留序列化与状态行
    展示——管理员必须始终可见告警发往何处。
    与 notify-config 的字段分层刻意不对称：邮件侧没有额度可勘察——发送路径
    不受每日条数上限与同类节流约束（额度只有推送那两本账，见 notify/ledger），
    本接口返回的其余键全是"通道开没开、发往何处"，属普通管理员运维必读信息。
    """
    m = _appmod()
    cfg = m.mailer.get_config()
    enabled = str(cfg.get("enable", "")).strip().lower() in ("1", "true", "on", "yes")
    return jsonify({
        "ok": True,
        "enabled": enabled,
        "admin_notify": bool(cfg.get("admin_notify", True)),
        "smtp_host": cfg.get("host", ""),
        "smtp_port": cfg.get("port", 465),
        "user": cfg.get("user", ""),
        "admin_to": cfg.get("admin_to", ""),
        "smtps": [
            {
                "host": str(e.get("host", "")),
                "port": e.get("port", 465),
                "user": m.mail_config._mask_addr(e.get("user")),
                "has_pass": bool(e.get("pass")),
            }
            for e in m.mailer.smtp_list()
        ],
    })


def api_mail_config_save():
    """主管理员：切换邮件配置开关 / 保存 SMTP 发信条目列表（写 .env）。

    支持：enabled（全局 YIBAN_MAIL_ENABLE）/ admin_notify（主管理员个人
    接收 YIBAN_MAIL_ADMIN_NOTIFY）。两者可单独或同时提交，均为 bool。
    admin_to：告警收件人（顶层 YIBAN_MAIL_ADMIN_TO，逗号分隔多地址）——
    A 线告警收件算法的唯一来源（见 mailer.admin_recipients /
    db.admin_mail_recipients）。**键存在即以提交值为准**：空串 = 显式清空
    （删键）、键缺失 = 不改动；前端输入框留空不提交（GET 已打码、不回显完整
    地址，避免误清），清空走单独的「清空」按钮。不再需要收 ADMIN_TO 时优先用
    同卡「接收发给我自己的邮件提醒」开关，那只是停止本人接收、不影响其他管理员。
    smtps：SMTP 发信条目列表（主备 failover），每条
    {host, port=465, user, pass}；pass 留空且该索引旧条目已有
    授权码 → 保留旧 pass（不改授权码时无需重输），user 留空同理按索引
    沿用旧值（GET 打码后前端不回显完整地址），落盘前 AES-GCM 加密为
    YIBAN_MAIL_SMTPS_ENC。条目级 admin_to 不接受也不写入（发送路径从不读该键）。

    邮件通道是全部安全告警的最后一条送达路径——"先关通知再作案"
    是活体复现的攻击链首步（拿到内置主管理员 Cookie 后一个 PUT 就能让所有
    告警静默）。故**关闭**类改动纳入高危门禁：与三处高危删除同口径，
    统一走 _high_risk_gate()（二次鉴权 + 复用同一份高危限速计数；顺序为
    "先验口令，通过了才占用额度"）；
    纯开启、以及不带开关的改动不要求口令（不得给正常成功路径加摩擦）。
    smtps/admin_to 变更与开关关闭是两套并存的高危门禁（不合并）：两者
    同属"改告警送达路径"，共用一次 _reconfirm_admin_password，不占高危
    限速额度；同时提交时只验一次口令。
    """
    m = _appmod()
    if not m._is_builtin_admin_session():
        return jsonify({"error": "仅主管理员可操作"}), 403
    data = m._json_body()
    # 全量校验通过才统一落盘：边校验边 write_env_key 会在后一个字段非法时
    # 留下"一半已生效"的写入
    flags = {}
    for field, env_key in (("enabled", "YIBAN_MAIL_ENABLE"),
                           ("admin_notify", "YIBAN_MAIL_ADMIN_NOTIFY")):
        if field not in data:
            continue
        v = data[field]
        if not isinstance(v, bool):
            return jsonify({"error": "取值无效"}), 400
        flags[env_key] = v
    # ---- 告警收件人（admin_to）：校验通过后才做口令二次确认 ----
    # 键存在 = 本次以提交值为准（空串 = 显式清空）；键缺失 = 不改动。
    # 前端输入框留空按"不改动"处理（不回显完整地址，避免误清），清空走单独按钮。
    admin_to_val = None
    if "admin_to" in data:
        raw_to = str(data.get("admin_to") or "").strip()
        if raw_to:
            # 逗号分隔多地址：逐条校验格式与长度（EMAIL_RE 与注册同源，
            # 上限 64 与 users.email 列口径一致），任一非法即整请求拒绝
            addrs = [a.strip() for a in raw_to.split(",") if a.strip()]
            if not addrs:
                return jsonify({"error": "告警收件人格式无效"}), 400
            if len(addrs) > m.MAIL_ADMIN_TO_MAX:
                return jsonify({"error": f"告警收件人最多 {m.MAIL_ADMIN_TO_MAX} 个"}), 400
            for a in addrs:
                if not m.EMAIL_RE.match(a) or len(a) > 64:
                    return jsonify({"error": f"告警收件人格式无效：{a[:32]}"}), 400
            admin_to_val = ",".join(addrs)
        else:
            admin_to_val = ""  # 显式清空（write_env_batch 空值 = 删键）
    # ---- SMTP 发信条目列表（smtps）：全量校验通过后才做口令二次确认 ----
    smtps_list = None
    if "smtps" in data:
        raw_list = data["smtps"]
        if not isinstance(raw_list, list):
            return jsonify({"error": "smtps 应为列表"}), 400
        if len(raw_list) > m.MAIL_SMTPS_MAX:
            return jsonify({"error": f"SMTP 发信条目最多 {m.MAIL_SMTPS_MAX} 条"}), 400
        # 旧列表取自改动前的解密结果：pass 留空且该索引旧条目已有授权码 → 保留旧值
        old_entries = m.mailer.smtp_list()
        smtps_list = []
        for i, e in enumerate(raw_list):
            if not isinstance(e, dict):
                return jsonify({"error": f"smtps 第 {i + 1} 条格式无效"}), 400
            # or "" 兜底：JSON null（键存在值为 null 时 get 的默认值不生效）不得
            # 经 str(None) 落盘为 "None"（与下方 pass 同口径）
            host = str(e.get("host") or "").strip()
            user = str(e.get("user") or "").strip()
            if not host:
                return jsonify({"error": f"smtps 第 {i + 1} 条 host 不能为空"}), 400
            # 目标地址判据（写侧硬拦）：拿被窃主管理员会话改 SMTP 目标就能把发信失败
            # 日志当成内网端口扫描器用（连接被拒/超时/无路由互不相同）。默认拒内网
            # 与不可路由段；确需本地/内网 MTA 的部署用 YIBAN_MAIL_ALLOW_PRIVATE_HOST
            # 显式开启——存量配置不受影响（不改 smtps 就不经过这里）。
            _host_reason = m.mail_config.check_smtp_host(host)
            if _host_reason:
                return jsonify({"error": f"smtps 第 {i + 1} 条：{_host_reason}"}), 400
            # user 留空 = 沿用该索引旧条目的 user（与 pass 的按索引保留一致：
            # GET 已打码，前端不回显完整发件账号，留空提交才不会误清空）；
            # 无旧值可沿用时存空串（同 pass 口径）
            if not user and i < len(old_entries) and old_entries[i].get("user"):
                user = str(old_entries[i]["user"])
            try:
                port = int(e.get("port", 465))
            except (TypeError, ValueError):
                return jsonify({"error": f"smtps 第 {i + 1} 条端口无效"}), 400
            if not 1 <= port <= 65535:
                return jsonify({"error": f"smtps 第 {i + 1} 条端口应为 1~65535"}), 400
            pwd = str(e.get("pass", "") or "")
            if not pwd and i < len(old_entries) and old_entries[i].get("pass"):
                pwd = str(old_entries[i]["pass"])  # 留空 = 不修改该条授权码
            smtps_list.append({
                "host": host,
                "port": port,
                "user": user,
                "pass": pwd,
            })
    # smtps 与 admin_to 同属"改告警送达路径"，合并为一次口令确认
    # （同时提交只验一次；两者都不涉及则不做口令校验）
    if smtps_list is not None or admin_to_val is not None:
        # _reconfirm_admin_password 约定：None=通过，否则 (jsonify, status) 元组；
        # 第一个参数是整个请求体（门禁还要看 confirm_delay_ack 之类的同请求字段）
        denied = _reconfirm_admin_password()(data, "修改邮件 SMTP 配置")
        if denied is not None:
            return denied
    if not flags and smtps_list is None and admin_to_val is None:
        return jsonify({"error": "缺少有效配置项"}), 400
    # 高危判定：任一开关被置为"关"即为关闭通道（admin_notify=false 只关主管理员
    # 本人的 ADMIN_TO 收件，同样是给报警器拔线）
    closing = [k for k, v in flags.items() if v is False]
    if closing:
        # 动作标签按字段区分——"全站告警邮件停发"与"只拔主管理员本人的
        # ADMIN_TO 收件"危害面完全不同，二次鉴权失败告警里必须看得见对方当时
        # 想关的是哪一路（键名来自代码常量，无注入面）
        label = "关闭邮件告警通道（" + "、".join(m._MAIL_FLAG_NAMES[k] for k in closing) + "）"
        # 限速与鉴权的顺序由统一门禁保证——先验口令，通过了才占额度
        gate = _high_risk_gate()(data, label)
        if gate:
            return gate
    # 加密排在口令确认之后（同 notify-config：失败请求零写盘痕迹）
    smtps_enc = None
    if smtps_list is not None:
        try:
            enc = m.account_crypto.encrypt_text(
                json.dumps(smtps_list, ensure_ascii=False),
                m.account_crypto.load_key(m.ENV_FILE),
            )
        except ValueError as e:
            return jsonify({"error": f"加密失败：{e}"}), 500
        smtps_enc = json.dumps(enc, ensure_ascii=False)
    # 密文与开关合成**一次** write_env_batch 落盘：拆成两次独立写，中间崩溃会
    # 留下"密文新/开关旧"的中间态（write_env_key 单键形态是本函数的一半，
    # 此处不再经由它）。
    # 改收件人前先记下旧地址：落盘后 _alert_mail_recipients() 读到的已是新值，
    # 若不额外通知旧地址，被盗会话只要一次 PUT 就能把告警悄悄改投他人信箱，
    # 而真正的管理员收不到任何"收件人被改了"的提示（与"关开关"同族的拔线动作）。
    old_admin_to = m.mailer.admin_recipients()
    updates = {k: ("1" if v else "0") for k, v in flags.items()}
    if smtps_enc is not None:
        updates["YIBAN_MAIL_SMTPS_ENC"] = smtps_enc
    if admin_to_val is not None:
        updates["YIBAN_MAIL_ADMIN_TO"] = admin_to_val
    m.write_env_batch(m.ENV_FILE, updates)
    # 变更告警在写入**成功之后**发出：先发会让加密/写盘失败（500）时运营者已收到
    # 一条描述从未生效变更的通知；force=True 本就绕过两侧节流，故不必担心刚写入的
    # 参数把这条告警吞掉（它正是"通道被人动了"的信号）。
    # urgent=True——设置页开着「仅推送重要告警」时非紧急通知不推手机。
    if flags:
        m.send_notification(
            "邮件配置变更告警",
            m._change_mail("邮件通知配置已变更。",
                           detail=[("变更内容", m._mail_flags_desc(flags))]),
            urgent=True,
            force=True,
        )
    if smtps_list is not None:
        # SMTP 发信条目是告警邮件的送达路径，被人改动必须让管理员知情
        m.send_notification(
            "邮件 SMTP 配置变更告警",
            m._change_mail("邮件 SMTP 配置已变更。",
                           detail=[("发信 SMTP 条目", f"{len(smtps_list)} 条")]),
            urgent=True,
            force=True,
        )
    if admin_to_val is not None:
        new_addrs = {a.strip() for a in admin_to_val.split(",") if a.strip()}
        if new_addrs != set(old_admin_to):
            # 变更后的收件人：走正常通道（落盘后 _alert_mail_recipients 已含新值）。
            # 正文写新值但打码——告警正文不得回显完整邮箱（与 GET 同口径）。
            shown = m.mail_config._mask_addr(admin_to_val) if admin_to_val else "（已清空）"
            m.send_notification(
                "邮件告警收件人变更告警",
                m._change_mail("告警收件人已变更。", detail=[("新收件人", shown)]),
                urgent=True,
                force=True,
            )
            # 被摘掉的旧地址：绕过 send_notification 的收件人合成（此刻已解析
            # 不到旧值），直接发给改动前的收件人。这是防"改收件人即致盲"的关键一封。
            stale = [a for a in old_admin_to if a not in new_addrs]
            if stale:
                m.mailer.send_admin_alert(
                    "邮件告警收件人变更告警",
                    m.mail_layout.Mail(
                        summary="你已不再是本系统的告警邮件收件人。",
                        fields=[("操作者", m._nl_safe(session.get("username", "?")))],
                        advice=["如非本人操作，请立即检查管理后台"],
                        level="urgent",
                    ),
                    to=",".join(stale),
                )
    detail = {
        "enabled" if k == "YIBAN_MAIL_ENABLE" else "admin_notify": v
        for k, v in flags.items()
    }
    if smtps_list is not None:
        detail["smtps_count"] = len(smtps_list)
    if admin_to_val is not None:
        # 审计记打码值：留痕要能回答"收件人被谁改到哪个域名"，但不落完整地址
        detail["admin_to"] = m.mail_config._mask_addr(admin_to_val)
    resp = {"ok": True}
    resp.update(detail)
    m.db.audit(
        session.get("username") or "?",
        "mail_config", "mail_config",
        json.dumps(detail, ensure_ascii=False),
    )
    return jsonify(resp)


def api_notify_config():
    """消息推送配置状态（脱敏：密钥打码），供管理后台显示。

    响应含 cooldown / urgent_only 与两本账每日额度（分账）：
    daily_max / daily_remaining = 非紧急账，urgent_daily_max /
    urgent_daily_remaining = 紧急账；上限为 0（不限）时对应 remaining 为 null。

    额度**余量**（daily_remaining / urgent_daily_remaining）仅主管理员可读——它是
    "拆报警器前还剩几条能发"的勘察面；`cooldown` 与两本账的 `*_max` 是规则配置，
    任何管理员都必须看得见，否则他无法判断"为什么没收到告警"。
    隐藏时对应键置 null 并**恒定**下发 `quota_visible` 布尔：remaining 的 null 原本
    表示"不限"，只靠 null 表达"无权查看"会让前端把两者读成同一个意思。
    """
    m = _appmod()
    cfg = m.notify.get_config()
    quota_visible = m._is_builtin_admin_session()
    if not quota_visible:
        for key in m._NOTIFY_QUOTA_HIDDEN_KEYS:
            cfg[key] = None
    cfg["quota_visible"] = quota_visible
    return jsonify(cfg)


def api_notify_config_save():
    """主管理员：保存消息推送配置（写 .env，密钥 AES-GCM 加密）。

    body: {"type": "serverchan"|"custom"|"", "secret": "..."|"", "cooldown": 秒|省略,
           "urgent_only": true|false|省略, "daily_max": 条数|省略,
           "urgent_daily_max": 条数|省略}
    type 为空 = 清除配置；secret 为空 = 清除密钥；cooldown 0 = 关闭同类型节流
    （0 显式落盘，不再"删键回落默认"）；urgent_only = 仅推送重要告警（非紧急仅走邮件）；
    daily_max / urgent_daily_max 0 = 不限（两本账分账）。

    推送通道与邮件通道是告警仅有的两条出口，"关闭推送 / 清空密钥 /
    换密钥"三类动作等同给报警器拔线，与三处高危删除同口径加二次鉴权 +
    限速（同窗口同上限，语义即"高危配置变更限速"，不新建第二套计数）。
    额度/节流参数（cooldown / urgent_only / daily_max /
    urgent_daily_max）同样纳入二次鉴权——它们决定告警推不推、何时推、推几条，
    调大 cooldown、打开 urgent_only、把 daily_max 压到 1 与"拔线"同效
    （给报警器装消音器），同口径收口。
    """
    m = _appmod()
    if not m._is_builtin_admin_session():
        return jsonify({"error": "仅主管理员可操作"}), 403
    data = m._json_body()
    ntype = str(data.get("type", "")).strip().lower()
    secret = str(data.get("secret", "")).strip()
    if ntype not in ("serverchan", "custom", ""):
        return jsonify({"error": "未知的通知类型"}), 400
    # 校验依据是**落盘后实际生效**的类型，不是请求里写了什么：`type` 置空但带密钥时，
    # 加载侧把空类型解析成 custom（兼容旧版只配密钥的写法），于是"不带 type、只提交
    # secret"就能绕开下面的地址白名单——空类型必须与 custom 同判。
    effective_type = ntype or ("custom" if secret else "")
    if effective_type == "serverchan" and secret:
        if not secret.startswith("SCT"):
            return jsonify({"error": "Server酱 SendKey 应以 SCT 开头"}), 400
        # 长度上限：SendKey 是"定长前缀 SCT + 固定宽度主体"（实测 35 字符），
        # 留一倍余量到 64；无上限时超长值会原样加密进 .env 并在每次推送时带出。
        if not m.NOTIFY_SENDKEY_MIN_LEN <= len(secret) <= m.NOTIFY_SENDKEY_MAX_LEN:
            return jsonify({"error": "Server酱 SendKey 长度不合法"}), 400
    elif effective_type == "custom" and secret:
        if len(secret) > m.NOTIFY_URL_MAX_LEN:
            return jsonify({"error": f"自定义地址过长（最多 {m.NOTIFY_URL_MAX_LEN} 字符）"}), 400
        if not m.notify.is_safe_url(secret):
            return jsonify({"error": "自定义地址仅允许 HTTPS 且非回环/内网地址"}), 400
    # ---- 高危判定：会"让推送通道失效、改密钥，
    # 或调整告警送达节奏/额度"的请求都要口令 ----
    # (a) type 置空 = 关闭推送；(b) 本次落盘后不再有密钥 = 清空密钥（含"只提交
    # type 却不带 secret"这条隐蔽路径——它同样会删掉旧密文）；(c) 携带新密钥 = 换钥；
    # (d)：出现任一额度/节流键 = 调整告警送达参数（同样致盲面）。
    touches_channel = ("type" in data) or ("secret" in data)
    close_channel = "type" in data and ntype == ""
    clear_secret = touches_channel and not secret
    swap_secret = bool(secret)
    weakens_alerting = any(
        k in data for k in ("cooldown", "urgent_only", "daily_max", "urgent_daily_max")
    )
    # 三类动作互斥，其并集恰好等于"触碰通道"的请求：带 type/secret 时要么有密钥
    # （换钥）要么没有（关闭或清钥）；(d) 与之可叠加（一次请求既换钥又调参数）
    need_reconfirm = close_channel or clear_secret or swap_secret or weakens_alerting
    # 支持部分更新：仅在请求体出现的字段才写入（如「仅重要告警」开关单独保存时
    # 不携带 type/secret，避免误清空已配置的推送通道）
    updates = {}
    numeric = {}
    if "type" in data:
        updates["YIBAN_NOTIFY_TYPE"] = ntype
        if not secret:
            # 空 = 删除键（关闭或换型不留旧钥）；带密钥时由闸门之后的加密段填
            updates["YIBAN_NOTIFY_SECRET_ENC"] = ""
    if "cooldown" in data:
        try:
            cd = max(0, int(data["cooldown"]))
        except (TypeError, ValueError):
            return jsonify({"error": "冷却参数无效"}), 400
        # 0 也必须显式落盘：0 → 删键 → 回落 DEFAULT_COOLDOWN=60，设置页
        # "关闭节流"就成了"被节流 60 秒"，与 .env.example 里"0=关闭"的承诺相反。
        updates["YIBAN_NOTIFY_COOLDOWN"] = str(cd)
        numeric["cooldown"] = cd
    if "urgent_only" in data:
        # 仅重要告警：true → 非紧急通知不推手机（邮件不受影响）；false → 全部推送
        updates["YIBAN_NOTIFY_URGENT_ONLY"] = "1" if data["urgent_only"] else ""
        numeric["urgent_only"] = bool(data["urgent_only"])
    if "daily_max" in data:
        try:
            dm = max(0, int(data["daily_max"]))
        except (TypeError, ValueError):
            return jsonify({"error": "每日上限参数无效"}), 400
        updates["YIBAN_NOTIFY_DAILY_MAX"] = "0" if dm == 0 else str(dm)  # 0 = 不限（显式写 0）
        numeric["daily_max"] = dm
    if "urgent_daily_max" in data:
        # 紧急账也要能从设置页管理：分账后两本账必须能同时管
        try:
            udm = max(0, int(data["urgent_daily_max"]))
        except (TypeError, ValueError):
            return jsonify({"error": "紧急每日上限参数无效"}), 400
        updates["YIBAN_NOTIFY_URGENT_DAILY_MAX"] = "0" if udm == 0 else str(udm)
        numeric["urgent_daily_max"] = udm
    if need_reconfirm:
        # 高危动作（含额度/节流参数调整）通过后才占用高危额度
        label = (
            "关闭消息推送通道" if close_channel
            else "更换消息推送密钥" if swap_secret
            else "清空消息推送密钥" if clear_secret
            else "调整推送限流/额度参数"
        )
        # 统一门禁——先验口令，通过了才占用额度（错口令尝试不得消耗预算）
        gate = _high_risk_gate()(data, label)
        if gate:
            return gate
    # 密钥加密刻意排在闸门**之后**：account_crypto.load_key 在既无
    # YIBAN_ACCOUNTS_KEY 环境变量、.env 里也没有该键时会"生成随机密钥并原子写回
    # .env"。放在闸门之前会让一次被拒绝的高危请求凭空留下写盘痕迹，与"鉴权失败
    # 零写入"的口径相反（加密失败仍返回 500 且不落任何配置）。
    if secret:
        try:
            enc = m.account_crypto.encrypt_text(secret, m.account_crypto.load_key(m.ENV_FILE))
            updates["YIBAN_NOTIFY_SECRET_ENC"] = json.dumps(enc, ensure_ascii=False)
        except ValueError as e:
            return jsonify({"error": f"加密失败：{e}"}), 500
    # 变更告警在写入**成功之后**发出（与 mail-config 同口径）：先发会让落盘失败
    # （500）时运营者已收到一条描述从未生效变更的通知；daily_max/urgent_daily_max/
    # cooldown/urgent_only 即按新值生效也不影响本条——force=True 本就绕过两侧节流
    # 与额度。urgent=True——本告警正是"通道被人拆了"的信号。
    m.write_env_batch(m.ENV_FILE, updates)
    m.send_notification(
        "消息推送配置变更告警",
        m._change_mail("消息推送配置已变更。",
                       detail=[("变更内容", m._notify_change_desc(
                           ntype, close_channel, clear_secret, swap_secret, numeric))]),
        urgent=True,
        force=True,
    )
    # 审计只记实际落盘的键：未提交 type 不得按 off 记录（把"没动通道"伪造成
    # "关过通道"）；密文真的写入时补记去向，事后才能还原完整动作。
    detail = dict(numeric)
    if "type" in data:
        detail["type"] = ntype or "off"
    if "YIBAN_NOTIFY_SECRET_ENC" in updates:
        detail["secret"] = "updated" if updates["YIBAN_NOTIFY_SECRET_ENC"] else "cleared"
    m.db.audit(
        session.get("username") or "?",
        "notify_config", "notify_config",
        json.dumps(detail, ensure_ascii=False),
    )
    return jsonify(m.notify.get_config())


def api_notify_test():
    """主管理员：发送一条测试消息验证推送配置。"""
    m = _appmod()
    if not m._is_builtin_admin_session():
        return jsonify({"error": "仅主管理员可操作"}), 403
    ok = m.notify.send_test()
    if not ok:
        return jsonify({"error": "测试消息发送失败（未配置或推送被拒，详见服务日志）"}), 400
    return jsonify({"ok": True, "msg": "测试消息已发送，请检查手机/接收端"})


def register(app):
    """在本域注册五条通知配置路由；endpoint 取函数名（url_for 依赖）。"""
    app.add_url_rule("/api/mail-config", view_func=api_mail_config)
    app.add_url_rule("/api/mail-config", view_func=api_mail_config_save, methods=["PUT"])
    app.add_url_rule("/api/notify-config", view_func=api_notify_config)
    app.add_url_rule("/api/notify-config", view_func=api_notify_config_save, methods=["PUT"])
    app.add_url_rule("/api/notify-test", view_func=api_notify_test, methods=["POST"])
