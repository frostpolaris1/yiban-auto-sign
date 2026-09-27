# -*- coding: utf-8 -*-
"""测试专用：用户单条操作的**不透明 id** 定位（MF-49 出口面 · Task 2-9b）。

`/api/users/<email>/role|password|delete` 已收紧为 `/api/users/<int:id>/…`——
明文邮箱不再编进 URL path（path 进 nginx `combined` 的 `$request`、经同源
Referrer 外送）。各用例如实构造「先建用户、再操作」的链路，这里把邮箱解析回
行主键 id 并拼出请求路径；用户不存在即前置失败（测"不存在用户 404"的用例请
直接传一个不存在的数字 id，别走本助手）。
"""


def user_path(db, email, tail):
    """按邮箱查活跃用户行，返回 `/api/users/<id><tail>`。

    tail 以 / 开头，如 "/role"、"/delete"。
    """
    row = db.find_user(email)
    assert row is not None, f"测试前置失败：用户不存在 {email}"
    return f"/api/users/{row['id']}{tail}"
