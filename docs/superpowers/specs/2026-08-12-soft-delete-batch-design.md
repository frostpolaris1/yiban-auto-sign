# 软删除（撤销）+ 批量多选 + 设置页置顶 设计

日期：2026-08-12
状态：方案已确认（待删除分组+7天自动清；双页面全批量；设置页开关持久化）

## 功能 A：账号软删除（撤销能力）

**行为**：管理员删除账号 → 软删除（`deleted:true` + `deleted_at`）→ 管理端「待删除」分组可见（可恢复/彻底删除）→ 7 天后自动彻底清除；签到跳过；用户端显示「已删除」状态且可重新提交。

**数据**：accounts.json 账号新增字段 `deleted`（bool）、`deleted_at`（ISO 时间）；常量 `DELETED_RETENTION_DAYS = 7`。

**后端（web/app.py）**：
- `load_accounts()` 惰性清理：读取后过滤超期 deleted 项并写回（锁内）
- `api_account_delete`（管理员）：改为软删除（置 deleted/deleted_at）
- 新 API：`POST /api/accounts/<idx>/restore`（恢复）、`POST /api/accounts/<idx>/purge`（彻底删除）
- `mask_account` 加 deleted/deleted_at 字段
- `_my_account_indices_of`：普通用户仍可见自己的 deleted 账号（用户端显示已删除状态）
- `api_my_account_add` 单账号限制：排除 deleted（已删除的可重新提交）
- 用户端 `_my_account_view`：deleted 账号状态徽章「已删除」+ 文案，不显示编辑/删除操作
- `api_my_account_delete`（用户删自己）：保持物理删除（用户自主删除无需撤销）

**signin.py**：`_load_accounts_from_file` 过滤 `deleted`（与 pending 同处加一行）；`_load_accounts_from_json_env` 同。

**前端 index.html**：账号管理第三组「待删除账号」（标题+人数+搜索+限高，与待处理组同风格），行操作：恢复/彻底删除；renderAccounts 分组加 deleted 组。
**前端 user.html**：deleted 账号卡片显示「已删除」状态徽章 + 说明，隐藏编辑/删除按钮，表单可重新提交。

## 功能 B：批量多选（开关控制）

**开关**：系统设置新增「批量操作」开关，持久化 `.env` `YIBAN_BATCH_MODE=on`；`api_settings` GET 返回 `batch_mode`，POST 支持保存；默认关闭。

**批量 API（web/app.py，锁内）**：
- `POST /api/accounts/batch`：`{action: approve|reject|purge, ids:[...], reason?}`（reject 需 reason 共同理由；purge=彻底删除）
- `POST /api/users/batch`：`{action: set_admin|unset_admin|reset_password|delete, emails:[...], password?}`

**前端 index.html**：
- 开关开启时：5 个表格加复选框列（表头全选 + 行复选框），选中后出现批量操作条
- 账号表批量：通过/拒绝（prompt 理由）/彻底删除（confirm）；用户表批量：设为管理员/取消/重置密码（prompt 一次密码）/删除（confirm）
- 批量操作后重新 load；复选框状态随渲染重置

## 功能 C：设置页置顶

「服务器时间与签到窗口」卡片移到系统设置页第一张（随机延迟前）。

## 版本

0.11.1 → **0.12.0**（功能）；双份 CHANGELOG；spec 存档。

## 验证

- node --check 三模板；ruff
- 浏览器实测：软删除流程（删除→待删除组→恢复/彻底删除→用户端已删除状态→重新提交）、批量（开关开→复选→批量操作→结果）、设置页置顶、签到过滤（signin.py 单元级断言）
