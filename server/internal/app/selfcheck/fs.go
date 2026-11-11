// 自检的文件系统端口。
//
// # 归属
// selfcheck 包内的端口定义（app 层不依赖 adapters）。
// 测试注入内存替身；生产实现由 cmd/cli 装配注入（osFileSystem）。
// OS 实现刻意**不**留在 app 层：canonical 的 fs.os.impl 规则禁止
// app 下出现 CreateTemp，防止端口实现回流。
//
// # 口径
// 自检不建临时文件：目录可写性由 core/fsperm 按服务用户的 POSIX 位推断。
// WriteFileAtomic 的契约：同目录临时文件 + chmod + rename，
// 文件从创建那一刻就是 0600；父目录不存在时按 0700 创建，
// 已存在的父目录不改权限（/var/lib/yiban 还承载照片与页面目录）。
package selfcheck

import "os"

// FileSystem 是自检的文件系统端口。
type FileSystem interface {
	Stat(name string) (os.FileInfo, error)
	ReadFile(name string) ([]byte, error)
	ReadDir(name string) ([]os.DirEntry, error)
	// WriteFileAtomic 原子写文件，权限取 perm。
	WriteFileAtomic(name string, data []byte, perm os.FileMode) error
}
