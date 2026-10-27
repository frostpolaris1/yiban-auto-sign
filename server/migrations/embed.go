// Package migrations 嵌入 PostgreSQL 迁移，使服务二进制自带 schema 版本。
//
// # 功能
// 把 postgres/*.sql 编进二进制，供 adapters/pg 的 Migrate 读取。
// 本包**不**执行迁移、**不**连库、**不**校验版本。
//
// # 归属
// 构建期载体。唯一读取方是 adapters/pg。
//
// # 复用
// Dir、Files。
//
// # 口径
// 文件名形如 <数字>_<名称>.sql；最大数字即本二进制版本。
// 生产回滚靠恢复备份；
// **改数据列的 Down 绝不对线上执行**。
// goose 对没有 Down 段的文件执行 Down 时打印 EMPTY、
// 删除版本行、**保留**全部 schema 对象。
// 故 00001–00009 禁止执行 Down；它们的文件头声明 forward-only。
package migrations

import "embed"

// Dir 是 Files 内放 SQL 文件的目录。
const Dir = "postgres"

// Files 持有全部迁移。
// 嵌入模式在构建期检查，故目录缺失是编译错误，
// 而不是空迁移集。
//
//go:embed postgres/*.sql
var Files embed.FS
