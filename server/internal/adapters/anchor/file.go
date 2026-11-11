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
// 每行解析时算 LineHash（整行字节的 SHA-256）：meta 用它检出截断与改写。
//
// # 归属
// adapters 层文件设施。app/audit 只依赖 AnchorReader 端口；
// CLI 的 `audit anchor` 与服务的巡检端点共用本实现。
//
// # 复用
// File、NewFile、ErrEmptyFile、ContainsLineHash。
//
// # 口径
// 文件不存在返回 (nil, nil)：未配置锚点不是错误，调用方按现行为处理。
// 文件存在但为空返回 core/audit.ErrAnchorEmpty（ErrEmptyFile 是它的别名）：
// 空文件可能是截断，不得静默当成"从未写锚点"。
// 任一行不可解析即返回错误：损坏的锚点文件不能继续追加，也不能被当成通过。
// 本包**不**持有 HMAC 密钥、**不**校验审计链、**不**判断锚点是否与链一致。
package anchor

import (
	"bytes"
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
// 与 core/audit.ErrAnchorEmpty 是同一个值：app 层只依赖 core 的哨兵，
// 不 import adapters；两个名字保留是为了调用点可读。
var ErrEmptyFile = coreaudit.ErrAnchorEmpty

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
	anchors, err := f.readAnchors(ctx)
	if err != nil || anchors == nil {
		return nil, err
	}
	best := anchors[0]
	for _, anchor := range anchors[1:] {
		if anchor.Seq >= best.Seq {
			best = anchor
		}
	}
	return best, nil
}

// Last 读取锚点文件的最后一条非空行（不按 seq 选最大）。
//
// 文件不存在返回 (nil, nil)；文件存在但为空返回 ErrEmptyFile。
// CLI 追加后用它确认"刚写的那一行"确实落在文件末尾。
func (f *File) Last(ctx context.Context) (*coreaudit.Anchor, error) {
	anchors, err := f.readAnchors(ctx)
	if err != nil || anchors == nil {
		return nil, err
	}
	return anchors[len(anchors)-1], nil
}

// ContainsLineHash 报告文件里是否存在 line_hash 相符的行。
// meta 记录的整行字节哈希在文件中找不到，即锚点文件被截断或该行被改写。
// 文件不存在/为空返回 false（找不到即截断）；解析错误原样上抛。
func (f *File) ContainsLineHash(ctx context.Context, lineHash []byte) (bool, error) {
	anchors, err := f.readAnchors(ctx)
	if err != nil {
		if errors.Is(err, ErrEmptyFile) {
			return false, nil
		}
		return false, err
	}
	if anchors == nil {
		return false, nil
	}
	for _, anchor := range anchors {
		if bytes.Equal(anchor.LineHash, lineHash) {
			return true, nil
		}
	}
	return false, nil
}

// readAnchors 按文件顺序解析全部非空行。
// 文件不存在返回 (nil, nil)；存在但为空返回 (nil, ErrEmptyFile)。
func (f *File) readAnchors(ctx context.Context) ([]*coreaudit.Anchor, error) {
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
	var anchors []*coreaudit.Anchor
	for i, raw := range strings.Split(string(data), "\n") {
		line := strings.TrimSpace(strings.TrimSuffix(raw, "\r"))
		if line == "" {
			continue
		}
		parsed, err := parseLine(line)
		if err != nil {
			return nil, fmt.Errorf("anchor: line %d: %w", i+1, err)
		}
		anchors = append(anchors, parsed)
	}
	if len(anchors) == 0 {
		return nil, ErrEmptyFile
	}
	return anchors, nil
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
	// 末行缺换行时先补一个：否则新行会拼在旧行尾部，两行一起不可解析。
	prefix := ""
	if data, err := os.ReadFile(f.path); err == nil && len(data) > 0 && data[len(data)-1] != '\n' {
		prefix = "\n"
	}
	if _, err := file.WriteString(prefix + line); err != nil {
		return fmt.Errorf("anchor: write %s: %w", f.path, err)
	}
	if err := file.Sync(); err != nil {
		return fmt.Errorf("anchor: sync %s: %w", f.path, err)
	}
	return nil
}

// parseLine 解析一行锚点。字段固定五个，时间戳不含空格（RFC3339Nano）。
// LineHash 是本行**去首尾空白后**全部字节的 SHA-256：CLI 与本包用同一
// 格式写行，两侧算出的值一致；文件里任何字节被改写（含 count）都会改变它。
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
	lineSum := sha256.Sum256([]byte(line))
	return &coreaudit.Anchor{Seq: seq, Count: count, Hash: hash, LineHash: lineSum[:]}, nil
}
