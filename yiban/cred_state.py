# -*- coding: utf-8 -*-
"""**功能**
账密熔断状态文件（`cred-state.json`）：**唯一读写入口**。

内容：`{phone: {fail_days, last_fail, paused_since, probe_date}}`——"连续凭据失败
达到阈值则暂停签到"的事实源，属安全相关状态（写错了会拿错密码反复登录，加重风控）。
语义约定：**无任何暂停记录 = 文件不存在**。

**为什么读写必须集中**：三个写入方（签到全量轮、签到手动轮 `--only`、Web 端改密/
编辑后清除熔断）都做"读 → 改 → 写"。若读改写整体不原子，就会出现：签到进程从启动
起持有的**内存快照**在收尾时整体覆盖保存，把运行期间 Web 端刚清掉的暂停写回；
反过来 Web 端也可能抹掉签到进程并发写入的其他账号记录。

**做法**：整段读-改-写放进同一把跨进程文件锁（`yiban/infra/locks.py` 的统一原语），
并且**按手机号增量合并**而不是整表覆盖——调用方只声明"我改了哪些账号"，
其余账号永远由磁盘上的最新值决定。

**归属**
`yiban` 包根的安全状态模块（`yiban/cred_state.py`），服务引擎与 Web 两侧：它是"账密
连续失败即暂停"的事实源，被 `state_io` / `round` / `probe` 与 web 服务层共用。

**复用**
`path()` / `read()` / `write()` / 按手机号的增量合并接口是全项目**唯一**读写入口，
调用方都走它；路径解析复用 `yiban.infra.env_io.resolve_path`。

**通信**
输入：本进程要改动的账号 → `{fail_days, last_fail, paused_since, probe_date}` 记录。
输出：`cred-state.json`（`YIBAN_STATE_DIR` 下，原子写 + 跨进程锁）。
调用谁：`clock`、`yiban.infra.env_io`、`yiban.infra.locks`。
谁调用：`round` / `probe`（失败记账与暂停）、`state_io`、web 服务层（改密/编辑后清除熔断）。
前端调用点：`/api/accounts`（`web/static/js/components/account-ops.js`）与 `/api/my-accounts`
（`web/static/js/components/my-accounts.js`）的账号编辑，经 `web/services/logs.py` 的
`clear_fuse_on_cred_change` 清除暂停——合并/锁口径变化会让 Web 端刚解开的暂停被签到进程覆盖回来。
"""
import json
import logging
import os
import secrets

from . import clock
from .infra import env_io

logger = logging.getLogger("yiban.cred_state")

TMP_SUFFIX_LEN = 4


def path():
    """状态文件路径（YIBAN_STATE_DIR，与按日状态文件同目录）。"""
    state_dir = env_io.resolve_path("YIBAN_STATE_DIR", "/var/log/yiban")
    return os.path.join(state_dir, "cred-state.json")


def read():
    """读取全部记录；文件缺失/损坏返回 {}（容错：可选状态文件不得影响主流程）。

    utf-8-sig 容错 Windows 记事本/手工编辑写入的 BOM（会让 json.load 抛错）。
    """
    p = path()
    try:
        with open(p, encoding="utf-8-sig") as f:
            data = json.load(f)
    except FileNotFoundError:
        return {}  # 常态：从未熔断过（语义就是"文件不存在"），不告警
    except ValueError as e:
        # 损坏必须留痕：否则"改密后仍暂停"无从排查
        logger.warning("账密状态文件损坏，按空处理（相关账号可能仍处暂停）: %s [%s]", p, e)
        return {}
    except OSError as e:
        logger.warning("读取账密状态文件失败: %s [%s]", p, e)
        return {}
    return data if isinstance(data, dict) else {}


def update(mutate):
    """在**同一把跨进程锁内**读-改-写；`mutate(data)` 就地修改并返回是否要写盘。

    所有写入方都必须走这里（见模块文档）。返回 mutate 的返回值。
    """
    from yiban.infra import locks
    p = path()
    with locks.file_lock(p):
        data = read()
        changed = mutate(data)
        if changed is False:
            return False
        _write(p, data)
        return True


def merge(touched, entries):
    """把 `touched` 中的手机号按 `entries` 增量合并进磁盘（其余账号保持最新值）。

    `entries`：{phone: record}；`touched` 里不在 `entries` 的号码 = 本次要**删除**的记录
    （签到成功会清除该账号的熔断记录）。空结果按"无暂停 = 文件不存在"语义删除文件。
    """
    def _apply(data):
        for phone in touched:
            rec = entries.get(phone)
            if rec:
                data[phone] = rec
            else:
                data.pop(phone, None)
        return True

    return update(_apply)


def clear(phone):
    """删除某账号的熔断记录（改密/改绑后立即可签）。返回是否实际发生删除。"""
    def _apply(data):
        if phone not in data:
            return False
        del data[phone]
        return True

    return update(_apply)


def _write(p, data):
    """原子写（唯一临时名 + os.replace）；空数据删除文件（保持既有语义）。"""
    try:
        if not data:
            if os.path.exists(p):
                os.remove(p)
            return
        os.makedirs(os.path.dirname(p) or ".", exist_ok=True)
        tmp = f"{p}.tmp{secrets.token_hex(TMP_SUFFIX_LEN)}"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)
        os.replace(tmp, p)
    except OSError as e:
        # 与既有实现同口径：目录不可写只告警，不影响签到执行
        logger.warning("写入账密状态文件失败（%s）: %s", os.path.basename(p), e)


def now_day():
    """当前业务日期（北京时间，与状态文件的 day 口径一致）。"""
    return clock.today()
