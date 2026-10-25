# config —— 配置项名册

## 放什么

`registry.json`：配置项的唯一定义点。每项给键名、类型、默认值、合法域、分组与敏感级别。
它是名册，不是配置值的存放处。

## 不放什么

不放用户配置值（那些在 `.env` 与 `secrets.env`）、运行期状态、代码与密钥。
不放按环境变化的覆盖值；覆盖只在部署侧文件里写。

## 入口

加载器：`yiban/adapters/config/loader.py`。
校验：`scripts/check-config-registry.py` 与 `tests/test_config_registry_gate.py`。
边界说明见 `docs/dev/target-tree.md` §5.3。

## 遗留与删除条件

目录名易被读成「配置值目录」，故本 README 纠正语义。目录不搬家：21 个文件提及原名。
删除条件：当加载器与全部读者改指新坐标，且有一条门钉住新坐标时。
