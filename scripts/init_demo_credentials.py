# -*- coding: utf-8 -*-
"""Demo 凭据初始化：确保普通演示用户 user / user123456 存在（幂等）。
管理员凭据在 .env（YIBAN_ADMIN_*），此处只处理 SQLite 用户表。"""
import sys
from datetime import datetime

sys.path.insert(0, "scripts")
from werkzeug.security import generate_password_hash

import db

DB = sys.argv[1] if len(sys.argv) > 1 else "yiban.db"

conn = db.init_db(DB)
demo_email = "user"          # 登录页用户名即填 user（登录时 lower() 匹配）
demo_hash = generate_password_hash("user123456", method="scrypt:65536:8:1")

u = db.find_user_any(demo_email)
if u is None:
    ok = db.create_user(demo_email, demo_hash, role="user",
                        created_at=datetime.now().strftime("%Y-%m-%dT%H:%M:%S"))
    print(f"user 账号不存在 -> 新建：{'成功' if ok else '失败'}")
elif u.get("deleted"):
    db.update_user_any_restore = None  # 占位，防误用
    # 已注销的演示账号直接复活并重置密码
    with db._conn_lock, conn:
        conn.execute(
            "UPDATE users SET deleted=0, deleted_at='', password_hash=?, role='user' WHERE email=?",
            (demo_hash, demo_email),
        )
    print("user 账号曾注销 -> 已恢复并重置密码")
else:
    with db._conn_lock, conn:
        conn.execute(
            "UPDATE users SET password_hash=?, role='user' WHERE email=? AND deleted=0",
            (demo_hash, demo_email),
        )
    print("user 账号已存在 -> 密码已重置为 demo 值，角色 user")

row = db.find_user(demo_email)
print("校验:", {k: row[k] for k in ("email", "role", "deleted")} if row else "未找到!")
