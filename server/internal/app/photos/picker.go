package photos

// P5-C 选图：为一次签到尝试从属主图片库确定性取图。
//
// 判据：未删图片按 (created_at, id) 升序排定一个稳定序列，
// 索引取 sha256(AttemptID) 前 8 字节对库大小的模。
// 同一 AttemptID 恒取同一张；图库增删后同 AttemptID 可换图
// （模数变了），这属于允许的轮换，不是缺陷。

import (
	"context"
	"crypto/sha256"
	"encoding/binary"
	"encoding/hex"
	"errors"
	"io"

	"github.com/frostpolaris1/yiban-auto-sign/server/internal/core/errs"
	corephoto "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/photo"
	"github.com/frostpolaris1/yiban-auto-sign/server/internal/signing/contract"
)

// PickForAttempt 为一次尝试选图。picked=false 表示库空。
// 返回的 ImageRef 携带完整元数据与字节，直接进 SignRequest。
func (s *Service) PickForAttempt(ctx context.Context, ownerUserID, attemptID string) (contract.ImageRef, bool, error) {
	if ownerUserID == "" {
		return contract.ImageRef{}, false, errs.New(errs.CodeValidation, "owner user is required")
	}
	if attemptID == "" {
		return contract.ImageRef{}, false, errs.New(errs.CodeValidation, "attempt id is required")
	}
	total, err := s.deps.Store.CountLibrary(ctx, ownerUserID)
	if err != nil {
		return contract.ImageRef{}, false, err
	}
	if total == 0 {
		return contract.ImageRef{}, false, nil
	}
	// 取前 8 字节拼 uint64 再取模：首字节只有 256 个值，
	// 图片库超过 256 张时高位索引永不命中，轮换退化（P5-C 审查 P3-3）。
	sum := sha256.Sum256([]byte(attemptID))
	idx := int(binary.BigEndian.Uint64(sum[:8]) % uint64(total))
	rec, err := s.deps.Store.LibraryAt(ctx, ownerUserID, idx)
	if err != nil {
		return contract.ImageRef{}, false, err
	}
	data, err := s.readBlob(ctx, rec)
	if err != nil {
		return contract.ImageRef{}, false, err
	}
	return contract.ImageRef{
		ID:     rec.ID,
		Mime:   rec.Mime,
		Width:  rec.Width,
		Height: rec.Height,
		SHA256: hex.EncodeToString(rec.SHA256[:]),
		Bytes:  data,
	}, true, nil
}

// readBlob 读出整张图并核对记录口径。
// 记录的 bytes 与实际长度不一致说明存储与库漂移：
// 签到链只信核对过的字节，漂移按内部错误失败关闭。
func (s *Service) readBlob(ctx context.Context, rec corephoto.Record) ([]byte, error) {
	body, err := s.deps.Blobs.Open(ctx, rec.SHA256)
	if err != nil {
		return nil, err
	}
	defer body.Close()
	// 上限取上传口径：库里只可能存过 MaxBytes 以内的预处理结果。
	data, err := io.ReadAll(io.LimitReader(body, corephoto.MaxBytes+1))
	if err != nil {
		return nil, err
	}
	if len(data) > corephoto.MaxBytes {
		return nil, errs.New(errs.CodeInternal, "stored photo exceeds the size limit")
	}
	if int64(len(data)) != rec.Bytes {
		return nil, errors.New("photos: blob size does not match the record")
	}
	return data, nil
}
