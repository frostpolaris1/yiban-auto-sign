// Package storage 提供内容寻址的本地 blob 存储。
//
// # 功能
// 按 SHA256 写入、读取图片字节；写入用临时文件加原子 rename。
// 本包**不**遍历目录、**不**做 GC、**不**生成 URL。
//
// # 归属
// adapters 层设施包。app/photos 的 Blobs 端口实现。
//
// # 复用
// FS、NewFS；布局 <root>/<sha 前两位>/<sha hex>。
//
// # 口径
// 路径只由摘要决定，不含用户输入，故无路径穿越面。
// Put 幂等：目标已存在直接返回成功。半写文件只在临时文件名下可见。
package storage

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"errors"
	"fmt"
	"io"
	"os"
	"path/filepath"
)

// FS 是本地内容寻址存储。
type FS struct {
	root string
}

// NewFS 构造存储并确保根目录存在。
func NewFS(root string) (*FS, error) {
	if root == "" {
		return nil, errors.New("storage: root is required")
	}
	abs, err := filepath.Abs(root)
	if err != nil {
		return nil, fmt.Errorf("storage: resolve root: %w", err)
	}
	if err := os.MkdirAll(abs, 0o700); err != nil {
		return nil, fmt.Errorf("storage: create root: %w", err)
	}
	return &FS{root: abs}, nil
}

// Put 写入一份字节。目标已存在时不重写。
func (f *FS) Put(ctx context.Context, sha [32]byte, data []byte) error {
	if err := ctx.Err(); err != nil {
		return err
	}
	if len(data) == 0 {
		return errors.New("storage: refusing to write empty blob")
	}
	target := f.path(sha)
	if _, err := os.Stat(target); err == nil {
		return nil
	} else if !errors.Is(err, os.ErrNotExist) {
		return fmt.Errorf("storage: stat blob: %w", err)
	}
	if err := os.MkdirAll(filepath.Dir(target), 0o700); err != nil {
		return fmt.Errorf("storage: create shard: %w", err)
	}
	tmp, err := os.CreateTemp(f.root, "put-*")
	if err != nil {
		return fmt.Errorf("storage: create temp: %w", err)
	}
	tmpName := tmp.Name()
	if _, err := tmp.Write(data); err != nil {
		tmp.Close()
		os.Remove(tmpName)
		return fmt.Errorf("storage: write temp: %w", err)
	}
	if err := tmp.Sync(); err != nil {
		tmp.Close()
		os.Remove(tmpName)
		return fmt.Errorf("storage: sync temp: %w", err)
	}
	if err := tmp.Close(); err != nil {
		os.Remove(tmpName)
		return fmt.Errorf("storage: close temp: %w", err)
	}
	if err := os.Rename(tmpName, target); err != nil {
		os.Remove(tmpName)
		// 并发写同一摘要：另一方已先 rename，视为成功。
		if _, statErr := os.Stat(target); statErr == nil {
			return nil
		}
		return fmt.Errorf("storage: publish blob: %w", err)
	}
	return nil
}

// Open 打开一份字节。缺失时返回错误。
func (f *FS) Open(ctx context.Context, sha [32]byte) (io.ReadCloser, error) {
	if err := ctx.Err(); err != nil {
		return nil, err
	}
	file, err := os.Open(f.path(sha))
	if err != nil {
		return nil, fmt.Errorf("storage: open blob: %w", err)
	}
	return file, nil
}

// path 返回摘要对应的绝对路径。
func (f *FS) path(sha [32]byte) string {
	name := hex.EncodeToString(sha[:])
	return filepath.Join(f.root, name[:2], name)
}

// Digest 计算字节的 SHA256，供调用方与摘要键对齐。
func Digest(data []byte) [32]byte { return sha256.Sum256(data) }
