// 自检的文件系统端口与 OS 实现。
//
// # 归属
// updatecheck 包内的端口定义（app 层不依赖 adapters）。
// 测试注入内存替身；生产用 OSFileSystem。
//
// # 口径
// ProbeWritable **要求目录已存在**：不创建目录，避免自检留下副作用。
// WriteFileAtomic 用同目录临时文件 + chmod + rename：
// 文件从创建那一刻就是 0600；父目录不存在时按 0700 创建，
// 已存在的父目录不改权限（/var/lib/yiban 还承载照片与页面目录）。
package updatecheck

import (
	"errors"
	"io"
	"os"
	"path/filepath"
)

// FileSystem 是自检的文件系统端口。
type FileSystem interface {
	Stat(name string) (os.FileInfo, error)
	ReadFile(name string) ([]byte, error)
	ReadDir(name string) ([]os.DirEntry, error)
	// ProbeReadable 打开目录并读一个条目；空目录也算可读。
	// 照片目录可能有大量文件，不整目录列举。
	ProbeReadable(dir string) error
	// ProbeWritable 在 dir 里创建并删除一个临时文件；目录必须已存在。
	ProbeWritable(dir string) error
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

// ProbeReadable 打开目录并读一个条目。空目录返回 io.EOF，按可读处理。
func (OSFileSystem) ProbeReadable(dir string) error {
	f, err := os.Open(dir)
	if err != nil {
		return err
	}
	defer func() { _ = f.Close() }()
	if _, err := f.Readdirnames(1); err != nil && !errors.Is(err, io.EOF) {
		return err
	}
	return nil
}

// ProbeWritable 建临时文件、写一个字节、关闭并删除。
// 删除失败也算失败：留下探针文件不是「可写」的证据。
func (OSFileSystem) ProbeWritable(dir string) error {
	f, err := os.CreateTemp(dir, ".selfcheck-probe-*")
	if err != nil {
		return err
	}
	name := f.Name()
	_, werr := f.WriteString("probe")
	cerr := f.Close()
	rerr := os.Remove(name)
	switch {
	case werr != nil:
		return werr
	case cerr != nil:
		return cerr
	default:
		return rerr
	}
}

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
