// Package anchor 读写库外审计链锚点文件。
//
// # 功能
// 锚点文件是 append-only 的纯文本，每行记一次链头快照：
//
//	v1 <RFC3339Nano UTC> <seq> <count> <hash>
//
// seq 是锚点时刻最后一行（最大 id）的行 id，count 是该行及之前的行数，
// hash 是该行存储哈希的 64 位小写十六进制。
// Latest 取 seq 最大的行作为最强锚点；Append 追加一行并把文件权限收紧到 0600。
//
// # 归属
// adapters 层文件设施。app/audit 只依赖 AnchorReader 端口；
// CLI 的 `audit anchor` 与服务的巡检端点共用本实现。
//
// # 复用
// File、NewFile、ErrEmptyFile。
//
// # 口径
// 文件不存在返回 (nil, nil)：未配置锚点不是错误，调用方按现行为处理。
// 文件存在但为空返回 ErrEmptyFile：空文件可能是截断，不得静默当成"从未写锚点"。
// 任一行不可解析即返回错误：损坏的锚点文件不能继续追加，也不能被当成通过。
// 本包**不**持有 HMAC 密钥、**不**校验审计链、**不**判断锚点是否与链一致。
package anchor

import (
	"context"
	"crypto/sha256"
	"encoding/hex"
	"errors"
	"fmt"
	"os"
	"path/filepath"
	"strconv"
	"strings"
	"time"

	coreaudit "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/audit"
)

// lineVersion 是当前锚点行格式版本。
const lineVersion = "v1"

// ErrEmptyFile 表示锚点文件存在但没有任何非空行。
var ErrEmptyFile = errors.New("anchor: file is empty")

// File 是一个锚点文件路径。
type File struct {
	path string
}

// NewFile 构造锚点文件句柄。路径为空时 Latest 返回 (nil, nil)，
// Append 返回错误：调用方必须先配置路径。
func NewFile(path string) *File { return &File{path: path} }

// Path 返回构造时的路径。
func (f *File) Path() string { return f.path }

// Latest 读取锚点文件并返回 seq 最大的那一行。
//
// 文件不存在返回 (nil, nil)。文件存在但为空返回 ErrEmptyFile。
// 任一行不可解析返回错误（含行号）。并列 seq 取靠后的行：
// 合法重锚会追加同 seq 的新行，最新一行描述库内现状。
func (f *File) Latest(ctx context.Context) (*coreaudit.Anchor, error) {
	if err := ctx.Err(); err != nil {
		return nil, err
	}
	if strings.TrimSpace(f.path) == "" {
		return nil, nil
	}
	data, err := os.ReadFile(f.path)
	if errors.Is(err, os.ErrNotExist) {
		return nil, nil
	}
	if err != nil {
		return nil, fmt.Errorf("anchor: read %s: %w", f.path, err)
	}
	var best *coreaudit.Anchor
	for i, raw := range strings.Split(string(data), "\n") {
		line := strings.TrimSpace(strings.TrimSuffix(raw, "\r"))
		if line == "" {
			continue
		}
		parsed, err := parseLine(line)
		if err != nil {
			return nil, fmt.Errorf("anchor: line %d: %w", i+1, err)
		}
		if best == nil || parsed.Seq >= best.Seq {
			best = parsed
		}
	}
	if best == nil {
		return nil, ErrEmptyFile
	}
	return best, nil
}

// Append 追加一行锚点并 fsync。文件不存在时创建，权限 0600。
//
// 追加前先读一遍现有文件：不可解析时拒绝追加，避免把新行接在损坏前缀之后。
// 空文件允许追加（首次写入）。父目录不存在时按 0700 创建。
func (f *File) Append(ctx context.Context, a coreaudit.Anchor, ts time.Time) error {
	if err := ctx.Err(); err != nil {
		return err
	}
	if strings.TrimSpace(f.path) == "" {
		return errors.New("anchor: path is required")
	}
	if a.Seq < 1 || a.Count < 1 || a.Count > a.Seq || len(a.Hash) != sha256.Size {
		return errors.New("anchor: invalid anchor value")
	}
	if _, err := f.Latest(ctx); err != nil && !errors.Is(err, ErrEmptyFile) {
		return fmt.Errorf("anchor: refuse to append to an invalid file: %w", err)
	}
	if dir := filepath.Dir(f.path); dir != "" && dir != "." {
		if err := os.MkdirAll(dir, 0o700); err != nil {
			return fmt.Errorf("anchor: create directory: %w", err)
		}
	}
	file, err := os.OpenFile(f.path, os.O_WRONLY|os.O_CREATE|os.O_APPEND, 0o600)
	if err != nil {
		return fmt.Errorf("anchor: open %s: %w", f.path, err)
	}
	defer func() { _ = file.Close() }()
	if err := file.Chmod(0o600); err != nil {
		return fmt.Errorf("anchor: chmod %s: %w", f.path, err)
	}
	line := fmt.Sprintf("%s %s %d %d %s\n",
		lineVersion, ts.UTC().Format(time.RFC3339Nano), a.Seq, a.Count,
		hex.EncodeToString(a.Hash))
	if _, err := file.WriteString(line); err != nil {
		return fmt.Errorf("anchor: write %s: %w", f.path, err)
	}
	if err := file.Sync(); err != nil {
		return fmt.Errorf("anchor: sync %s: %w", f.path, err)
	}
	return nil
}

// parseLine 解析一行锚点。字段固定五个，时间戳不含空格（RFC3339Nano）。
func parseLine(line string) (*coreaudit.Anchor, error) {
	parts := strings.Fields(line)
	if len(parts) != 5 {
		return nil, fmt.Errorf("want %q <ts> <seq> <count> <hash>", lineVersion)
	}
	if parts[0] != lineVersion {
		return nil, fmt.Errorf("unknown anchor version %q", parts[0])
	}
	if _, err := time.Parse(time.RFC3339Nano, parts[1]); err != nil {
		return nil, fmt.Errorf("invalid timestamp %q", parts[1])
	}
	seq, err := strconv.ParseInt(parts[2], 10, 64)
	if err != nil || seq < 1 {
		return nil, fmt.Errorf("invalid seq %q", parts[2])
	}
	count, err := strconv.ParseInt(parts[3], 10, 64)
	if err != nil || count < 1 || count > seq {
		return nil, fmt.Errorf("invalid count %q", parts[3])
	}
	if parts[4] != strings.ToLower(parts[4]) {
		return nil, fmt.Errorf("hash must be lowercase hex")
	}
	hash, err := hex.DecodeString(parts[4])
	if err != nil || len(hash) != sha256.Size {
		return nil, fmt.Errorf("invalid hash %q", parts[4])
	}
	return &coreaudit.Anchor{Seq: seq, Count: count, Hash: hash}, nil
}
