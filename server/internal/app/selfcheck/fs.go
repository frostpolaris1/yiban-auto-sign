// 自检的文件系统端口与 OS 实现。
//
// # 归属
// selfcheck 包内的端口定义（app 层不依赖 adapters）。
// 测试注入内存替身；生产用 OSFileSystem。
//
// # 口径
// 自检不建临时文件：目录可写性由 core/fsperm 按服务用户的 POSIX 位推断。
// WriteFileAtomic 用同目录临时文件 + chmod + rename：
// 文件从创建那一刻就是 0600；父目录不存在时按 0700 创建，
// 已存在的父目录不改权限（/var/lib/yiban 还承载照片与页面目录）。
package selfcheck

import (
	"os"
	"path/filepath"
)

// FileSystem 是自检的文件系统端口。
type FileSystem interface {
	Stat(name string) (os.FileInfo, error)
	ReadFile(name string) ([]byte, error)
	ReadDir(name string) ([]os.DirEntry, error)
	// WriteFileAtomic 原子写文件，权限取 perm。
	WriteFileAtomic(name string, data []byte, perm os.FileMode) error
}

// OSFileSystem 是 FileSystem 的进程实现。
type OSFileSystem struct{}

// Stat 实现 FileSystem。
func (OSFileSystem) Stat(name string) (os.FileInfo, error) { return os.Stat(name) }

// ReadFile 实现 FileSystem。
func (OSFileSystem) ReadFile(name string) ([]byte, error) { return os.ReadFile(name) }

// ReadDir 实现 FileSystem。
func (OSFileSystem) ReadDir(name string) ([]os.DirEntry, error) { return os.ReadDir(name) }

// WriteFileAtomic 在同一目录写临时文件，chmod 后 rename 覆盖目标。
// 父目录不存在时按 0700 创建；已存在的父目录不动权限。
func (OSFileSystem) WriteFileAtomic(name string, data []byte, perm os.FileMode) error {
	dir := filepath.Dir(name)
	if err := os.MkdirAll(dir, 0o700); err != nil {
		return err
	}
	f, err := os.CreateTemp(dir, ".update-state-*")
	if err != nil {
		return err
	}
	tmp := f.Name()
	cleanup := func() {
		_ = f.Close()
		_ = os.Remove(tmp)
	}
	if _, err := f.Write(data); err != nil {
		cleanup()
		return err
	}
	if err := f.Chmod(perm); err != nil {
		cleanup()
		return err
	}
	if err := f.Sync(); err != nil {
		cleanup()
		return err
	}
	if err := f.Close(); err != nil {
		_ = os.Remove(tmp)
		return err
	}
	if err := os.Rename(tmp, name); err != nil {
		_ = os.Remove(tmp)
		return err
	}
	return nil
}
