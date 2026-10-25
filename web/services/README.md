# web/services —— Web 应用层

## 放什么

Web 用例编排与呈现辅助。18 个模块：账号数据、手动签到、执行体环境、日志、实测、
校验队列、环境读写、限流与锁等。服务可以碰 `adapters`。

## 不放什么

不放路由注册（在 `web/routes/`）、不放模板、不放 `yiban/` 的领域算法。
路由不直接查库：`routes → services → yiban.usecases / yiban.engine / yiban.adapters`。

## 入口

`web/app.py` 在 import 末尾调用各服务的 `configure(...)`，登记宿主状态。
路由直接 import 本目录模块，不再经 `web.app` 转发墙。

## 遗留与删除条件

18 个模块保持原位：60 个测试直接 import，搬家不产生用户可见变化。
路由直连 adapters 的 6 个文件由 B2 收敛；边界门随 B2 落地。
