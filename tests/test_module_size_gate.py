# -*- coding: utf-8 -*-
"""模块化门禁：按类型设目标行数 + 超限必须写明工程理由。

`PROMPT.md` §5.2 第 4/7 条禁止"继续做超长文件"与"堆砌代码"。但**红线不是目的**：
真正的判据是"拆了是否更好维护"。若两个功能本就相似相通，硬拆会把原本一次函数调用
变成跨模块协议 + 注入/回调，反而更难读、更容易错——那种情况下**不拆才是对的**。

因此本门禁的规则是：

1. 未登记的文件 ≤ 该类型的目标上限（`LIMITS`：py 600 / js·css 800 / html·sh 400 行）；
2. 登记（`OVERSIZED`）的文件必须给出**工程理由**：为什么它现在这么大、为什么不拆
   （或不立刻拆）、下一步怎么处理。理由写不出 20 字以上就失败——**要么拆，要么说清楚**；
3. 登记项可以给硬上限（`limit`）也可以放弃（`None`，表示"按理由判断，允许增长"）；
4. 任何文件超过 `HARD_CAP` 一律失败（防真正的失控堆砌，与逐行较劲无关）；
5. 登记表不得指向已不存在的文件。

**判断某处该不该拆时，按顺序问自己**：
① 两部分是否服务于同一件事（同一状态机/同一表/同一协议）？是→不拆；
② 拆分是否要求把共享状态、回调或异常类型在模块间来回传递？是→**慎重**，通信成本可能
   高于收益；
③ 是否存在"独立变更轴"（一端改动不需要另一端改动）？是→拆；
④ 是否有第二个调用方（复用）？是→拆。
"""
import os
import tempfile
import unittest

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# 各类型目标上限：行数语义不同（py 源码、js/css 资源、html/sh 模板与脚本），
# 阈值按现值分布给足余量，又不放过真正的膨胀
LIMITS = {".py": 600, ".js": 800, ".css": 800, ".html": 400, ".sh": 400}
# 绝对上限：与逐行较劲无关，只拦"失控式堆砌"
HARD_CAP = 12000

# 超限但**经工程判断暂不拆/无法简单拆**的文件：(行数上限 or None, 理由)
# 理由需包含：它是什么、为什么现在这样、下一步。
OVERSIZED = {
    "web/app.py": (None, (
        "Flask 工厂 + 跨域中间件 + 模块级辅助（路由已入 web/routes/）。当前未拆的工程原因："
        "create_app 内仍有大量跨域闭包状态（_file_lock 保护的读改写序列、按会话的限速表、"
        "按 app 实例登记的高危门禁闭包），先拆会把共享状态改成跨模块注入，收益低于风险；"
        "既定拆法（services/security/render 三层）按依赖自然切分，届时一次到位。"
    )),
    "web/routes/accounts_api.py": (1000, (
        "账号管理域路由：十条管理员视图（列表/详情/增删改/批量/审核/排序）与 register()。"
        "规模来自注释契约（四问头 + 每处安全语义的「为什么」，约占四分之一）与真被依赖的"
        "顺序约束（二次鉴权先于占额度、审计先于外发、容量闸门、idx 错位守卫）——十条视图"
        "共用同一份「_file_lock 下读-改-写」语义，按端点切两半只会把这份序列与守卫在模块间"
        "来回传（通信成本高于收益）。下一步：若超过 1000 行，按「读（列表/详情）」与"
        "「写（增删改/批量/审核/排序）」切成两个模块，共享的 idx 守卫与掩码助手留在读侧。"
    )),
    "web/routes/my.py": (1000, (
        "个人自助族路由：十三条普通用户视图（我的账号提交/列表/编辑/软删/撤销/自暂停、"
        "自选时间片读写与统计、在线校验任务查询/取消、我的月历/日志）＋ 七个本人视图助手"
        "（_my_account_indices_of/_my_account_view/_pref_slots 等）与 register()。规模来自"
        "注释契约（四问头 + 每处「信息分层/为什么」说明，约占四分之一）与真被依赖的顺序"
        "约束（_file_lock 下读-改-写、idx 错位守卫、cooldown 与配额先后）。整族服务于同一份"
        "「本人身份 → 本人数据」收窄语义，助手被多条视图共用（判据①同一件事、②拆开要把"
        "同一份收窄口径在模块间来回传、③无独立变更轴）。下一步：若超过 1000 行，可按"
        "「账号自助（my-accounts 族）」与「选片/历史（time-pref、calendar、logs、verify-jobs）」"
        "一分为二，共享的本人视图助手留在账号侧。"
    )),
    "web/routes/settings_api.py": (1600, (
        "设置 / 执行体 / 公告域路由：十五条管理员视图（系统开关读写、执行体清单读写与单段"
        "出口改写、清单行增删改、现场实测、公告草稿/读取/双人发布、注册暂停状态、更新日志）"
        "＋ 两条落点助手（_executor_write_guard / _reply_slot_egress）与 register()。规模来自"
        "注释契约（四问头 + 每处口径与口令门次序的「为什么」，约占四分之一）与真被依赖的"
        "顺序约束（口令复核先于占额度、执行体写回要与清单同步、公告发布必须当次口令）。"
        "下一步：若超过 1600 行，按「系统设置 + 公告」与「执行体清单（含现场实测）」切成两个"
        "模块——两组之间没有共享状态，口令门都经 web.routes 取回，切分不需跨模块传状态。"
    )),
    "yiban/store/db.py": (None, (
        "SQLite 数据访问层的门面与尚未按域拆出的表访问。已拆出并"
        "再导出：连接（connection）、迁移（migrations）、审计链（audit_chain）、事件（events）、"
        "用户与注销（users）、每日清理（cleanup）。剩余部分不是'没拆'而是'还没拆'：accounts 表 "
        "CRUD 与加解密、time_prefs、session_cache、时钟守卫与 app_meta、追踪盐哈希五个域，"
        "外加跨域粘合（写事务入口、连带清理 _cascade_phone_owned、清理留痕 _record_purge_event/"
        "_table_min_max/_clock_jump_guard）——粘合函数被拆出模块反向依赖（events/cleanup/users "
        "都经门面取），拆走就得改成跨模块传递。下一步：按域继续迁出，accounts CRUD 迁入现有 "
        "accounts.py，time_prefs / session_cache / clock+meta 各立模块；粘合函数留在门面。"
    )),
    "yiban/store/users.py": (800, (
        "用户与注销域：users / user_delete_requests 两表的读写、"
        "最后管理员守卫、软注销与反悔恢复、到期物理清除、注销请求冷却计数，行数含注释契约要求"
        "的四问头与函数级说明（占约四分之一）。整块服务同一条状态机（软注销→宽限→恢复/物理"
        "清除）与同一对表，拆开就得把'最后管理员守卫'与'连带清理'在模块间来回传（门禁判据①"
        "成立、②不成立）。上限 800：超过则按「用户读写 + 角色守卫」与「注销/恢复/到期清除」"
        "切成两个模块，守卫用局部导入共享。"
    )),
    "yiban/store/audit_chain.py": (None, (
        "审计链域：HMAC 哈希链写入/校验、全表重链留痕、"
        "库外锚点族、审计密钥来源与缓存。四块服务于同一条协议与同一份取证状态——锚点校验"
        "要读清理留痕、体检要汇总链/锚点/留痕全部信号，拆开就得把这份状态改成跨模块传递"
        "（门禁判据①「同一件事」成立、②不成立）。下一步：若继续膨胀需要再拆，按「密钥来源"
        "与缓存（_audit_key 族，只依赖 connection/env_io/env_lock）」与「锚点文件（record/"
        "verify/anchor 族）」切成两个模块；当前不拆，避免为搬家再动 db 门面与打桩面"
        "（db._audit_hash = 替身 / db._AUDIT_KEY_CACHE = None 必须落在真定义点）。"
    )),
    "yiban/store/migrations.py": (None, (
        "schema 版本迁移域：基线建表、migrate_v1..v17、"
        "版本编排 `_run_migrations` 与迁移助手 `_table_columns`/`_ensure_column`/`_ensure_index`。"
        "整块是**一份按版本号冻结的时间序列**——已发布的迁移函数不可再改，拆开就得把冻结的"
        "迁移登记表与「核心/可选、失败是否阻断启动」的编排判据在模块间来回传递（门禁判据①"
        "「同一件事」成立、②通信成本高）。下一步：版本只增不改，行数会持续增长；若超过 1000 "
        "行，按「迁移项（v1..vN，纯 DDL/数据修复）」与「编排 + 助手」切成两个模块，迁移登记表"
        "留在编排侧作唯一登记点。当前不拆，避免为搬家再动 db 门面与 `db._MIGRATIONS` 读写转发面。"
    )),
    # scripts/signin.py 已按"执行一轮"的边界切分为 yiban/engine/*（最大 round.py 507 行），
    # 旧路径只剩兼容壳（约 130 行），故不再登记。
    "yiban/egress.py": (None, (
        "出口分配 + 执行体清单模型（纳入 `YIBAN_EXECUTORS` 出口清单后规模上升）。"
        "两半**互相咬合**：清单的兜底/回退读取要用 `resolve`（清单优先、旧三键回退），"
        "而 `resolve` 又要读清单——按「拆开」办就得让两个模块来回传「当前清单」，"
        "通信成本高于收益（门禁判据②）。已定好的拆法：把清单模型（parse/dump/行增删改/"
        "迁移/name）整体搬到新模块 `yiban/executors.py`，`resolve` 侧用局部导入绕开循环"
        "（与 `engine/schedule.py` 里 `alerts` 的局部导入同法）；**清单模型下次变更时一起做**，"
        "不为了十几行做一次纯搬家（搬动会同时动 web/runner/测试几十处调用点）。"
    )),
    # yiban/cli.py：命令行统一入口（argparse 装配 + 七个子命令实现 + 单行 JSON 字段契约）。
    # 整块服务于同一件事——命令行面与它的退出码契约。拆开只增加跨文件对偶：新增子命令
    # 必须同时改"解析器"与"handler"，两个文件互为唯一调用方，没有独立变更轴（门禁判据②③）。
    # 当前 700 余行，其中约 45 行是给 agent 看的字段契约表。下一步：若超过 900 行，按
    # 「透传 sign/probe」与「只读运维子命令（config/capacity/state/db/version）」一分为二。
    "yiban/cli.py": (None, (
        "命令行统一入口：argparse 装配 + 七个子命令实现 + 单行 JSON 字段契约。整块服务于"
        "同一件事——命令行面与其退出码（每加一个子命令都要同时改解析与分发，拆成两个"
        "文件只增加跨文件对偶，没有独立变更轴）。当前 700 余行（含约 45 行字段契约表与"
        "逐条硬约定说明）；若增长到 900 行以上，按「透传 sign/probe」与「只读运维子命令"
        "（config/capacity/state/db/version）」切成两个模块。"
    )),
    "web/static/js/components/settings-executors.js": (780, (
        "执行体分区组件（规模 KPI + 清单表 + 行内设置弹窗 + 每个写操作的口令门 + 写明细口径的注释）。"
        "三块服务于同一个屏与**同一份接口响应**：lastData 被 KPI、清单渲染、行弹窗三处读，"
        "banner/focusAfterPaint/rowName/putRow 等助手三处共用——拆开等于把这份共享状态改成跨模块协议"
        "（门禁判据②），而任何接口字段变动仍要同时改多处（判据③不成立）。"
        "容量实测与建议已抽到 settings-quota.js；三种写操作（追加行/删行/改行）各带口令门，"
        "另有「只改名不打门」的分支，故上限设在 780。"
        "再涨就先切行内设置弹窗：openRow 及其独有助手（infoTip/linkBtn/ROW_HELP），"
        "届时要把它依赖的 lastData/putRow/banner 三样显式注入。"
    )),
    "web/static/js/core.js": (None, (
        "前端交互层核心（classic script，非 module）：全局 api/toast/modal/时钟/身份/导航行为，"
        "以及各页共用的 DOM 与脱敏助手，运行期只加载一次并挂在 window.YB。规模来自**共享本身**"
        "——所有页面模块都依赖这些全局符号，且加载顺序守卫钉住「core.js 先于组件与页面模块」，"
        "拆成多文件就得给同一份全局词法作用域加跨脚本加载协议（判据②通信成本高），并放大"
        "「拆文件撞名」的风险面。下一步：若继续增长，按「网络/身份」「UI 反馈」「助手函数」切分，"
        "切分前先重钉加载顺序守卫。"
    )),
    "web/static/css/app.css": (None, (
        "项目层唯一集中样式表：在 vendor 的 adminator.css 之后加载，只写设计系统没覆盖、"
        "或需按本项目语义扩展的部分。规模来自**单一样式上下文**——颜色/阴影/圆角一律取 "
        "Adminator 的主题 token，深浅两套主题才自动成立；字体栈是全站单一事实源，另有静态"
        "守卫对账收口。拆文件会破坏「加载顺序在 adminator.css 之后」与 token/字体收口的唯一"
        "来源性质（判据②）。下一步：若继续增长，把「token 与字体栈」（必须仍排最前）与"
        "「各节组件样式」切成两个文件，并同步收口守卫的对账清单。"
    )),
    "web/templates/pages/work_settings.html": (None, (
        "管理端系统设置页：全部系统级配置分区集中在一个模板里，含字段级权限矩阵（必须逐字段"
        "复刻后端判定）与模板素材复用说明。整页共用一套 tab 契约（[data-tab-group] + "
        "[data-tab-target] + [data-tab-id]）与一次「改动只标脏、点保存才提交」的语义，"
        "按分区拆文件会把权限矩阵与 tab 装配复制多份，并引入跨模板包含层（判据②）。"
        "下一步：若继续增长，把各分区移入 partials 由本页 include，tab 契约与权限说明"
        "留在本页作唯一登记点。"
    )),
    "scripts/backup.sh": (800, (
        "运维脚本：数据文件一致性快照 + 加密 + 保留策略 + --restore 恢复演练，"
        "头部使用/安装说明与各分支的防护校验占相当篇幅；非运行时模块，给硬上限防继续"
        "膨胀，超过上限则按「备份」与「恢复/校验」拆两个脚本。"
    )),
    # 工具脚本（非运行时模块，不参与模块化拆分），只设上限防继续膨胀
    "scripts/build_cjk_font_slices.py": (900, "构建期工具：字体分片生成脚本，一次性运行"),
    "scripts/rekey_accounts.py": (800, "运维工具：密钥轮换脚本，与本项目运行时解耦"),
    "scripts/loadtest/concurrency_probe.py": (800, "压测工具：并发探针，非运行时路径"),
}

# 扫描范围：运行时与共享代码（不含测试、构建产物、第三方）
SCAN_DIRS = ["scripts", "web", "docker", "yiban"]
# 各扫描目录纳入的扩展名：py 全量递归；前端资源限 web 内的 js/css/html（vendor 由剪枝排除）
SCAN_EXT = {
    "scripts": (".py", ".sh"),
    "web": (".py", ".js", ".css", ".html"),
    "docker": (".py",),
    "yiban": (".py",),
}


def _iter_scanned():
    for d in SCAN_DIRS:
        exts = SCAN_EXT[d]
        root_dir = os.path.join(BASE, d)
        if not os.path.isdir(root_dir):
            continue
        for dirpath, dirnames, filenames in os.walk(root_dir):
            dirnames[:] = [x for x in dirnames
                           if x not in ("__pycache__", "vendor", "node_modules", ".venv")]
            for name in sorted(filenames):
                ext = os.path.splitext(name)[1]
                if ext not in exts:
                    continue
                full = os.path.join(dirpath, name)
                rel = os.path.relpath(full, BASE).replace(os.sep, "/")
                yield rel, full, LIMITS[ext]


def _count_lines(path):
    with open(path, encoding="utf-8") as f:
        return sum(1 for _ in f)


def _size_violations(entries):
    """未登记的超限文件清单；门禁与自检共用（entries 为 (rel, full, limit) 序列）。"""
    out = []
    for rel, full, limit in entries:
        n = _count_lines(full)
        if rel in OVERSIZED or n <= limit:
            continue
        out.append((rel, n, limit))
    return out


class ModuleSizeGateTest(unittest.TestCase):
    def test_unlisted_files_within_target(self):
        violations = [
            f"  {rel}: {n} 行 > {limit} 行"
            for rel, n, limit in _size_violations(_iter_scanned())
        ]
        if violations:
            self.fail(
                "文件超过目标规模且未登记（PROMPT.md §5.2 第 4/7 条）：\n"
                + "\n".join(violations)
                + "\n请二选一：按上文判据拆开，或在 OVERSIZED 里写明工程理由与下一步。"
            )

    def test_gate_rejects_unregistered_oversized(self):
        """自检：未登记的超限文件必须被拦下，已登记者放行（临时文件即用即删）。"""
        limit = LIMITS[".js"]
        with tempfile.TemporaryDirectory() as td:
            path = os.path.join(td, "oversized_sample.js")
            with open(path, "w", encoding="utf-8") as f:
                f.write("// x\n" * (limit + 1))
            rel = "tmp/oversized_sample.js"
            self.assertEqual(
                len(_size_violations([(rel, path, limit)])), 1,
                "未登记的越线文件未被门禁拦下")
            self.assertEqual(_size_violations([(rel, path, limit * 1000)]), [])
        # 已登记者即便越线也放行
        core = os.path.join(BASE, "web", "static", "js", "core.js")
        self.assertEqual(
            _size_violations([("web/static/js/core.js", core, LIMITS[".js"])]), [])

    def test_scan_covers_all_types(self):
        """扫描面自检：各扩展名都必须扫到文件，防 SCAN_EXT 掉项后门禁静默失明。"""
        seen = {os.path.splitext(rel)[1] for rel, _, _ in _iter_scanned()}
        for ext in LIMITS:
            self.assertIn(ext, seen, f"扫描面漏掉 {ext} 类型文件")

    def test_oversized_entries_explain_themselves(self):
        """允许增长（limit=None）的登记项必须给出实质理由——"要么拆，要么说清楚"。

        工具脚本那类只给数字上限的条目是"防继续膨胀"，不参与拆分，无需长理由。
        """
        thin = []
        for rel, (limit, reason) in OVERSIZED.items():
            need = 20 if limit is None else 1
            if len(str(reason).strip()) < need:
                thin.append(f"  {rel}: 理由过短（{reason!r}）")
        if thin:
            self.fail("超限登记缺少工程理由：\n" + "\n".join(thin))

    def test_oversized_hard_limits_are_kept(self):
        """给了硬上限的登记项不得越线。"""
        violations = []
        for rel, (limit, _) in OVERSIZED.items():
            full = os.path.join(BASE, rel)
            if limit is None or not os.path.exists(full):
                continue
            n = _count_lines(full)
            if n > limit:
                violations.append(f"  {rel}: {n} 行 > 登记上限 {limit}")
        if violations:
            self.fail("工具脚本超出登记上限：\n" + "\n".join(violations))

    def test_no_file_runs_away(self):
        """绝对上限：拦"失控式堆砌"，与逐行红线无关。"""
        violations = [
            f"  {rel}: {_count_lines(full)} 行 > 绝对上限 {HARD_CAP}"
            for rel, full, _ in _iter_scanned()
            if _count_lines(full) > HARD_CAP
        ]
        if violations:
            self.fail("单文件失控膨胀（请按判据拆分）：\n" + "\n".join(violations))

    def test_entries_exist(self):
        """登记表不得指向已不存在的文件（拆走后请删除条目）。"""
        stale = [rel for rel in OVERSIZED
                 if not os.path.exists(os.path.join(BASE, rel))]
        if stale:
            self.fail("登记表指向已不存在的文件（拆分完成后请删除条目）：" + ", ".join(stale))


if __name__ == "__main__":
    unittest.main(verbosity=2)
