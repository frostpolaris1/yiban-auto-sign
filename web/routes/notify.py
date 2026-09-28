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
配置落盘走 `write_env_batch` 一次原子写；通道变更只留审计行（不外发告警——见视图内注释），
`POST /api/notify-test` 的测试消息经 `send_notification` 发出。
"""
import json
import re
import uuid

from flask import jsonify, session

from web.routes import appmod as _appmod
from web.routes import high_risk_gate as _high_risk_gate
from web.routes import reconfirm_admin_password as _reconfirm_admin_password
from yiban.infra.env_io import EnvWriteRefused as _EnvWriteRefused

# SMTP 条目的稳定 id：贯穿「前端行 ↔ 落盘条目 ↔ 凭据沿用」的唯一身份，位置不参与身份。
# 字符集/长度收紧（不透明短标识而非自由文本）：id 一旦能夹带任意内容，就成了绕脱敏
# 把值写进审计/配置的旁路通道。前端自产同形状（"smtp-" 前缀 + base36），两侧同口径。
_SMTP_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{7,63}$")


def _smtp_id_ok(value):
    return isinstance(value, str) and bool(_SMTP_ID_RE.match(value))


def _new_smtp_id():
    return "smtp-" + uuid.uuid4().hex[:12]


def _norm_smtp_port(value):
    """身份匹配用的端口读数：缺失/不可解析按 465（与发送路径的端口回退同口径）。"""
    try:
        return int(value)
    except (TypeError, ValueError):
        return 465


def _smtp_target(entry):
    """条目的「告警去向」判据：host + port。凭据只随目标走，目标变了就是换中继。"""
    return (str(entry.get("host") or "").strip(), _norm_smtp_port(entry.get("port")))


def _smtp_dest_view(entries):
    """条目的「告警去向」审计视图：每条一个紧凑串 `host:port`。

    审计 detail 的预算是 200 字符（含作用域标记，见 audit_chain._scope_detail），
    结构必须尽量小才可能把**整份旧清单**原样留下（可还原目标）。授权码/发件账号
    不进视图：前者是红线，后者属凭据而非去向；收件人口径由 admin_to(_from) 打码值
    单独覆盖。
    """
    return [f"{e.get('host') or ''}:{_norm_smtp_port(e.get('port'))}" for e in entries]


# db.audit 对 detail 的硬预算 200 字符，且 _scope_detail 会在 JSON 里注入
# `,"_req": "web-<8hex>-<8hex>"`（33 字符）。超预算时宁可显式裁剪并标注，也不能让
# audit 层把 JSON 截成非法串——截断的 JSON 下游还原不了，"可还原目标"就成了一句空话。
_AUDIT_DETAIL_BUDGET = 167


def _bounded_audit_json(detail):
    """把 detail 编进审计预算；放不下时逐条裁剪视图数组并留 `_cut` 标注。

    裁剪顺序刻意先新后旧：`smtps_to` 的新去向马上能在 GET /api/mail-config 与
    下一条审计里再看到，`smtps_from` 却是**唯一**的旧去向存证——预算不够时先牺牲
    可再生的那一份。
    """
    s = json.dumps(detail, ensure_ascii=False)
    if len(s) <= _AUDIT_DETAIL_BUDGET:
        return s
    d = dict(detail)
    for key in ("smtps_to", "smtps_from"):
        while d.get(key) and \
                len(json.dumps(d, ensure_ascii=False)) > _AUDIT_DETAIL_BUDGET:
            d[key] = d[key][:-1]
            d[key + "_cut"] = True
    # 上面的逐条裁剪只救 `smtps` 两个**数组视图**，极端
    # `admin_to`（多条长地址逐项打码后仍超预算）无人管——届时 `audit()` 层的
    # `_scope_detail` 走后缀退化截断，把整段 JSON 截烂（下游还原失败，"可还原
    # 目标"落空）。字符串字段同法限量：逐段收缩并打 `_cut`；旧收件人先牺牲
    # （"改到哪"比"从哪来"更常被追问，与 smtps 先新后旧的裁剪次序同理）。
    for key in ("admin_to_from", "admin_to"):
        while isinstance(d.get(key), str) and d[key] and \
                len(json.dumps(d, ensure_ascii=False)) > _AUDIT_DETAIL_BUDGET:
            d[key] = d[key][:-4]
            d[key + "_cut"] = True
    if len(json.dumps(d, ensure_ascii=False)) > _AUDIT_DETAIL_BUDGET:
        # 连逐条收缩后的最小形态都放不下（超长 host + 超长收件人的病态组合）：
        # 地址类视图（smtps 两清单与 admin_to 两串）整体让位给计数与显式标注，
        # 至少保住 enabled/admin_notify 开关键与 JSON 可解析性——宁可缺一面，
        # 不产一条"截烂的伪存证"。
        for key in ("smtps_from", "smtps_to", "smtps_from_cut", "smtps_to_cut",
                    "admin_to", "admin_to_from", "admin_to_cut", "admin_to_from_cut"):
            d.pop(key, None)
        d["smtps_view"] = "too_long"
    return json.dumps(d, ensure_ascii=False)


def api_mail_config():
    """邮件通知配置状态（脱敏：授权码不回显，地址打码），供管理后台显示。

    smtps：SMTP 发信条目列表（mailer.smtp_list 解密结果；pass 绝不回显，
    仅以 has_pass 标记该条是否已有授权码；user 同顶层字段口径经
    mail_config._mask_addr 打码——发件账号也属敏感地址，编辑时留空即沿用）。
    id 是条目的稳定身份（前端据此携带、后端按它取旧凭据）；旧格式落盘的条目
    无 id，序列化回 null——前端为其现生成、后端保存时经 (host,port) 唯一匹配
    认领旧凭据（迁移口径），下一次保存起持久化 id。
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
                # id：条目的稳定身份（旧格式条目无 id 时为 null，前端为其现造、
                # 保存时按迁移口径认领旧凭据并落定 id）
                "id": e.get("id") if _smtp_id_ok(e.get("id")) else None,
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
    {id?, host, port=465, user, pass}。条目身份是**稳定 id**（前端建行时自产、
    GET 原样带回；无 id 的新条目保存时补发），位置不参与身份。pass 留空时
    沿用「按 id 认领到旧条目、且 (host,port) 目标未变」的旧授权码，user 同理
    （GET 打码后前端不回显完整地址，留空提交才不会误清空）；改目标 = 换中继，
    **不**沿用旧凭据——旧授权码绝不随新域名一起发出，代价是改 host/port 后须
    重新输入授权码，这是刻意的取舍。旧格式存量条目与不带 id 的旧客户端提交走
    迁移口径：按 (host,port) **唯一**匹配认领，歧义即不猜（留空按空值落盘）。
    id 非法形状或同请求内重复 → 400。落盘前 AES-GCM 加密为
    YIBAN_MAIL_SMTPS_ENC。条目级 admin_to 不接受也不写入（发送路径从不读该键）。

    邮件通道是全部安全告警的最后一条送达路径。口令门收窄（缩减批 6a，用户拍板
    清单）后的口径：开关（enabled/admin_notify）与收件人（admin_to）都是**可逆
    改动**（设回即可）→ 免口令门免额度，留痕统一交给落盘后的审计行（谁、把哪路
    从哪改到哪）；SMTP 凭据变更（中继/授权码 = 换钥类）仍要当次口令（直连
    _reconfirm_admin_password，不占高危限速额度）。曾把"关闭"纳入高危门禁的理由
    （"先关通知再作案"）随威胁模型降级让位于"审计可回溯 + 操作可逆"——审计行
    仍逐次落盘，告警通道自身无法可靠通报自己的变更（见下方落盘处注释）。
    smtps 与 admin_to 同请求提交时口令只按 smtps 需要（admin_to 免门，不拖累）。
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
        # 旧列表取自改动前的解密结果。凭据沿用的身份匹配分两轮认领（见下方
        # smtps_list 组装处），任何一步都不看数组位置——位置一致只是巧合的来源，
        # 不是身份。
        old_entries = m.mailer.smtp_list()
        parsed = []
        seen_ids = set()
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
            try:
                port = int(e.get("port", 465))
            except (TypeError, ValueError):
                return jsonify({"error": f"smtps 第 {i + 1} 条端口无效"}), 400
            if not 1 <= port <= 65535:
                return jsonify({"error": f"smtps 第 {i + 1} 条端口应为 1~65535"}), 400
            # id 校验：形状非法直接 400（id 是不透明身份标识，不是自由文本——放任意
            # 内容就成了一条绕开脱敏写进配置/审计的旁路）；null/缺失/空串按"无 id"
            # 走迁移口径。同一请求内 id 重复也 400：两条条目认领同一旧身份必有一条错配。
            eid = e.get("id")
            if eid is None:
                eid = ""
            else:
                eid = str(eid).strip()
                if eid and not _SMTP_ID_RE.match(eid):
                    return jsonify({"error": f"smtps 第 {i + 1} 条 id 无效"}), 400
            if eid:
                if eid in seen_ids:
                    return jsonify({"error": f"smtps 第 {i + 1} 条 id 与前面条目重复"}), 400
                seen_ids.add(eid)
            parsed.append({
                "id": eid,
                "host": host,
                "port": port,
                "user": user,
                "pass": str(e.get("pass", "") or ""),
                "src": None,
            })
        # 第 1 轮：按 id 认领旧条目。id 认领**不看目标**——同 id 改 host 也仍认到
        # 同一条旧目，凭据是否沿用由第 3 步的"目标未变"判据单独把关。
        claimed = set()
        for p in parsed:
            if not p["id"]:
                continue
            for j, oe in enumerate(old_entries):
                if j not in claimed and _smtp_id_ok(oe.get("id")) and oe["id"] == p["id"]:
                    claimed.add(j)
                    p["src"] = oe
                    break
        # 第 2 轮（迁移口径）：无 id/未知 id 的提交按 (host,port) 在**未被认领**的旧条目里
        # 找唯一匹配；同目标多条旧目（或一条旧目对多条提交）时不猜——fail-closed 让
        # 留空按空值落盘。这一步同时兜住旧客户端（不带 id）与旧配置（条目无 id）：
        # 认领看的是**目标逐字相同**、不看数组位置，凭据因此只会随同一个中继走；
        # 删中间行/重排都不再产生凭据错配。
        for p in parsed:
            if p["src"] is not None:
                continue
            cands = [j for j, oe in enumerate(old_entries)
                     if j not in claimed and _smtp_target(oe) == (p["host"], p["port"])]
            if len(cands) == 1:
                claimed.add(cands[0])
                p["src"] = old_entries[cands[0]]
        # 第 3 步：凭据沿用。留空 user/pass 只在认领到的旧条目**目标未变**时回填——
        # 改 host/port = 换中继，旧授权码/旧发件账号绝不跟去新域名（risk 档此前
        # "零口令零确认"就漏在这一步，现在由后端硬判据封口，不依赖前端自觉）。
        smtps_list = []
        for p in parsed:
            src = p["src"]
            if src is not None and _smtp_target(src) == (p["host"], p["port"]):
                if not p["user"] and src.get("user"):
                    p["user"] = str(src["user"])
                if not p["pass"] and src.get("pass"):
                    p["pass"] = str(src["pass"])
            smtps_list.append({
                "id": p["id"] or _new_smtp_id(),
                "host": p["host"],
                "port": p["port"],
                "user": p["user"],
                "pass": p["pass"],
            })
    # 旧收件人值必须在落盘**之前**取——写后再读只会读到刚写进去的新值，
    # "从哪改到哪"就塌成"从哪改到哪自己"。
    admin_to_old = m.mail_config._get("ADMIN_TO") if admin_to_val is not None else None
    # SMTP 凭据变更（中继/授权码 = 换钥类）要当次口令；admin_to 是可逆路由改动，
    # 缩减批 6a 起免口令（不拖累同请求的 smtps 一起免——两字段分别判定）
    if smtps_list is not None:
        # _reconfirm_admin_password 约定：None=通过，否则 (jsonify, status) 元组；
        # 第一个参数是整个请求体（门禁还要看 confirm_delay_ack 之类的同请求字段）
        denied = _reconfirm_admin_password()(data, "修改邮件 SMTP 配置")
        if denied is not None:
            return denied
    if not flags and smtps_list is None and admin_to_val is None:
        return jsonify({"error": "缺少有效配置项"}), 400
    # 开关关闭曾走 _high_risk_gate（二次鉴权 + 高危额度）：缩减批 6a 收窄为免门
    # 免额度——开关可逆（设回即可），留痕靠落盘后的审计行（见下方 db.audit），
    # 高危额度只留给删除/清库/换钥/改凭据这类不可逆或凭据动作。
    # 加密排在口令确认之后（同 notify-config：失败请求零写盘痕迹）
    smtps_enc = None
    if smtps_list is not None:
        try:
            enc = m.account_crypto.encrypt_text(
                json.dumps(smtps_list, ensure_ascii=False),
                m.account_crypto.load_key(m.ENV_FILE),
            )
        except _EnvWriteRefused:
            # load_key 的"缺钥自动生成写回"被写入口 fail-closed 拒绝（脏 .env）：
            # 交 Flask 统一 409+清理指引，不得并入下面的 500 把"需人工清理配置"
            # 说成"加密失败"（与公告/执行体等写点的 except 顺序同族）
            raise
        except ValueError as e:
            return jsonify({"error": f"加密失败：{e}"}), 500
        smtps_enc = json.dumps(enc, ensure_ascii=False)
    # 密文与开关合成**一次** write_env_batch 落盘：拆成两次独立写，中间崩溃会
    # 留下"密文新/开关旧"的中间态（write_env_key 单键形态是本函数的一半，
    # 此处不再经由它）。
    updates = {k: ("1" if v else "0") for k, v in flags.items()}
    if smtps_enc is not None:
        updates["YIBAN_MAIL_SMTPS_ENC"] = smtps_enc
    if admin_to_val is not None:
        updates["YIBAN_MAIL_ADMIN_TO"] = admin_to_val
    m.write_env_batch(m.ENV_FILE, updates)
    # 通道变更不再外发告警（开关 / SMTP 条目 / 收件人三处都是）：改告警通道本身就
    # 是"把报警器拆掉"的动作，用它自己那条通道去通报"通道被改了"只在通道还活着时
    # 成立；留痕统一交给下面的审计行（开关新值 + 收件人/中继的打码"从哪→到哪"），
    # 运维按审计页即可回答"谁在什么时候把告警从哪一路改到了哪一路"。
    detail = {
        "enabled" if k == "YIBAN_MAIL_ENABLE" else "admin_notify": v
        for k, v in flags.items()
    }
    if smtps_list is not None:
        detail["smtps_count"] = len(smtps_list)
        # 改中继 = 改告警去向：审计必须能回答"从哪改到哪"并留下可还原目标
        # （新旧两条 host:port 清单；授权码永不入审计）。只记新值时，误改/被篡改
        # 后连"原来发往哪个中继"都无从查起。
        detail["smtps_from"] = _smtp_dest_view(old_entries)
        detail["smtps_to"] = _smtp_dest_view(smtps_list)
    if admin_to_val is not None:
        # 审计记打码值：留痕要能回答"收件人被谁改到哪个域名"，但不落完整地址；
        # 旧值同口径打码一并留下（"从哪改到哪"的"从哪"）。
        detail["admin_to"] = m.mail_config._mask_addr(admin_to_val)
        detail["admin_to_from"] = m.mail_config._mask_addr(admin_to_old)
    resp = {"ok": True}
    resp.update(detail)
    m.db.audit(
        session.get("username") or "?",
        "mail_config", "mail_config",
        _bounded_audit_json(detail),
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

    "关闭推送 / 清空密钥 / 换密钥"触碰**密钥本身**（判定 = type 或 secret 在场），
    仍过高危门禁（换钥/清钥属用户拍板门清单的"换钥"类），与高危删除同口径共用
    限速计数；调额度与节流参数（cooldown/urgent_only/daily_max/urgent_daily_max）
    是可逆改动，缩减批 6a 起免门免额度（留痕靠审计行）。
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
    # ---- 高危判定（缩减批 6a 收窄）----
    # 触碰**密钥**（type 或 secret 任一在场：换钥 / 清钥 / 关闭随钥清）→ 仍过高危
    # 门禁（换钥/清钥属用户拍板的"换钥"类，落点在凭据上，非 full 档还要倒计时确认的
    # 场景不受影响）；纯数值/节流参数（cooldown/urgent_only/daily_max/urgent_daily_max）
    # 是可逆改动 → 免门免额度，留痕靠落盘后的审计行。
    touches_channel = ("type" in data) or ("secret" in data)
    close_channel = "type" in data and ntype == ""  # (a) type 置空 = 关闭推送
    clear_secret = touches_channel and not secret  # (b) 本次落盘后不再有密钥 = 清空密钥
    swap_secret = bool(secret)  # (c) 携带新密钥 = 换钥
    # 门条件 = 触碰通道键（换钥/清钥/关闭清钥三类，互斥且并集恰为 touches_channel）；
    # 曾纳入的 (d) 额度/节流键不再进门——压额度、开 urgent_only 都可逆，免门。
    need_reconfirm = touches_channel
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
        # 仅重要告警：true → 非紧急通知不推手机（邮件不受影响）；false → 全部推送。
        # 两个状态都显式落盘（1 / 0）：该键的默认值是"开"，写空值等于删键、随即回落
        # 默认——设置页的"关闭"就成了"打开的"，与开关本身的意思相反。
        updates["YIBAN_NOTIFY_URGENT_ONLY"] = "1" if data["urgent_only"] else "0"
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
        # 统一门禁——先验口令，通过了才占用额度（错口令尝试不得消耗预算）；
        # 换钥/清钥属凭据改写类，占独立的凭据额度（缩批 6a 分流，不与删除互撞）
        gate = _high_risk_gate()(data, label, quota="creds")
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
        except _EnvWriteRefused:
            raise  # 同上：自动生成密钥的写回被拒走统一 409，不伪装成加密失败
        except ValueError as e:
            return jsonify({"error": f"加密失败：{e}"}), 500
    m.write_env_batch(m.ENV_FILE, updates)
    # 通道变更不再外发告警：改告警通道就是"把报警器拆掉"的动作，用它自己那条通道
    # 通报"通道被改了"只在通道还活着时成立；留痕统一交给下面的审计行（类型/密钥
    # 去向/各限额的新值逐键落盘），运维按审计页即可还原完整动作。
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
    # 每次外呼必留一条含操作者的审计：send(force=True) 跳过节流与两本每日额度，
    # 这是唯一一条管理员会话可直接驱动对外 HTTPS POST 的入口。失败结果同样落
    # 一行——对被拒/异常的尝试，请求往往已经发出，只记成功会漏掉真外呼。
    # 刻意不加会话配额：管理员自用排障按钮，强防护只会挡住日常（全站限速
    # + CSRF + 前端入口已把滥用面压在"会话被盗"这一前提上，那时审计更值钱）。
    m.db.audit(
        session.get("username") or "?", "notify_test", "notify_test",
        "测试推送已发送" if ok else "测试推送未送达（未配置或推送被拒，详见服务日志）",
    )
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
