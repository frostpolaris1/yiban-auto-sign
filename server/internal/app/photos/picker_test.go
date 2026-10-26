package photos_test

// P5-C 选图判据：同 AttemptID 恒同图；索引按 sha256 前 8 字节取模；
// 空库返回 picked=false；字节与元数据完整进入 ImageRef。

import (
	"bytes"
	"context"
	"crypto/sha256"
	"encoding/binary"
	"encoding/hex"
	"errors"
	"io"
	"testing"
	"time"

	appphotos "github.com/frostpolaris1/yiban-auto-sign/server/internal/app/photos"
	coreclock "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/clock"
	"github.com/frostpolaris1/yiban-auto-sign/server/internal/core/errs"
	corephoto "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/photo"
)

// pickStore 只覆盖选图面：库是固定序列，记录每次取的偏移。
// 其余方法嵌入接口占位——选图路径不调用，误调用即 panic。
type pickStore struct {
	appphotos.Store
	// rows 是按 (created_at, id) 升序排好的库。
	rows []corephoto.Record
	// offsets 记录每次 LibraryAt 收到的偏移。
	offsets []int
	err     error
}

func (f *pickStore) CountLibrary(context.Context, string) (int64, error) {
	if f.err != nil {
		return 0, f.err
	}
	return int64(len(f.rows)), nil
}

func (f *pickStore) LibraryAt(_ context.Context, _ string, offset int) (corephoto.Record, error) {
	f.offsets = append(f.offsets, offset)
	if offset < 0 || offset >= len(f.rows) {
		return corephoto.Record{}, errs.ErrNotFound
	}
	return f.rows[offset], nil
}

type pickBlobs struct{ data map[[32]byte][]byte }

func (b pickBlobs) Put(context.Context, [32]byte, []byte) error { return errors.New("not used") }

func (b pickBlobs) Open(_ context.Context, sha [32]byte) (io.ReadCloser, error) {
	data, ok := b.data[sha]
	if !ok {
		return nil, errors.New("blob missing")
	}
	return io.NopCloser(bytes.NewReader(data)), nil
}

func pickService(t *testing.T, store *pickStore, blobs pickBlobs) *appphotos.Service {
	t.Helper()
	svc, err := appphotos.NewService(appphotos.Deps{
		Store: store, Blobs: blobs, Clock: coreclock.Fixed{},
	})
	if err != nil {
		t.Fatalf("NewService: %v", err)
	}
	return svc
}

func libraryRecord(id string, at time.Time, blob []byte) corephoto.Record {
	sha := sha256.Sum256(blob)
	return corephoto.Record{
		ID: id, OwnerUserID: "user-1", SHA256: sha, Mime: "image/png",
		Width: 10, Height: 10, Bytes: int64(len(blob)), CreatedAt: at,
	}
}

// 同一 AttemptID 恒取同一张，且偏移等于 sha256(AttemptID)[0] % n。
func TestPickForAttemptDeterministic(t *testing.T) {
	blobs := pickBlobs{data: map[[32]byte][]byte{}}
	store := &pickStore{}
	for i, id := range []string{"p1", "p2", "p3"} {
		blob := []byte{byte('a' + i), 'b', 'c', 'd'}
		rec := libraryRecord(id, time.Date(2026, 1, i+1, 0, 0, 0, 0, time.UTC), blob)
		store.rows = append(store.rows, rec)
		blobs.data[rec.SHA256] = blob
	}
	svc := pickService(t, store, blobs)
	ctx := context.Background()

	first, picked, err := svc.PickForAttempt(ctx, "user-1", "attempt-42")
	if err != nil || !picked {
		t.Fatalf("PickForAttempt = (%+v,%v)", picked, err)
	}
	second, picked, err := svc.PickForAttempt(ctx, "user-1", "attempt-42")
	if err != nil || !picked {
		t.Fatalf("second PickForAttempt = (%+v,%v)", picked, err)
	}
	if first.ID != second.ID {
		t.Fatalf("同 AttemptID 选到不同图: %q vs %q", first.ID, second.ID)
	}
	sum := sha256.Sum256([]byte("attempt-42"))
	want := int(binary.BigEndian.Uint64(sum[:8]) % 3)
	if store.offsets[0] != want {
		t.Fatalf("偏移 = %d, want sha256 前 8 字节取模 %d", store.offsets[0], want)
	}
}

// 选图携带完整元数据：字节、摘要 hex、尺寸与媒体类型。
func TestPickForAttemptCarriesImage(t *testing.T) {
	blob := []byte("png-pretended-bytes")
	rec := libraryRecord("p1", time.Date(2026, 1, 1, 0, 0, 0, 0, time.UTC), blob)
	store := &pickStore{rows: []corephoto.Record{rec}}
	blobs := pickBlobs{data: map[[32]byte][]byte{rec.SHA256: blob}}
	svc := pickService(t, store, blobs)

	img, picked, err := svc.PickForAttempt(context.Background(), "user-1", "a1")
	if err != nil || !picked {
		t.Fatalf("PickForAttempt = (%+v,%v)", picked, err)
	}
	if img.ID != "p1" || img.Mime != "image/png" || img.Width != 10 || img.Height != 10 {
		t.Fatalf("img = %+v", img)
	}
	if !bytes.Equal(img.Bytes, blob) {
		t.Fatalf("bytes = %q, want %q", img.Bytes, blob)
	}
	if img.SHA256 != hex.EncodeToString(rec.SHA256[:]) {
		t.Fatalf("sha = %q", img.SHA256)
	}
}

// 空库与选图偏移越界：picked=false / ErrNotFound。
func TestPickForAttemptEmptyLibrary(t *testing.T) {
	store := &pickStore{}
	svc := pickService(t, store, pickBlobs{data: map[[32]byte][]byte{}})
	_, picked, err := svc.PickForAttempt(context.Background(), "user-1", "a1")
	if err != nil || picked {
		t.Fatalf("空库 = (%+v,%v), want (false,nil)", picked, err)
	}
}

// blob 与记录不一致（尺寸漂移）按内部错误失败关闭。
func TestPickForAttemptRejectsBlobDrift(t *testing.T) {
	rec := libraryRecord("p1", time.Date(2026, 1, 1, 0, 0, 0, 0, time.UTC), []byte("four"))
	rec.Bytes = 999 // 记录与字节不一致
	store := &pickStore{rows: []corephoto.Record{rec}}
	blobs := pickBlobs{data: map[[32]byte][]byte{rec.SHA256: []byte("four")}}
	svc := pickService(t, store, blobs)
	if _, _, err := svc.PickForAttempt(context.Background(), "user-1", "a1"); err == nil {
		t.Fatal("尺寸漂移应报错")
	}
}

// 空属主与空 attemptID：校验错误。
func TestPickForAttemptValidation(t *testing.T) {
	store := &pickStore{}
	svc := pickService(t, store, pickBlobs{data: map[[32]byte][]byte{}})
	if _, _, err := svc.PickForAttempt(context.Background(), "", "a"); errs.From(err).Code != errs.CodeValidation {
		t.Fatalf("空属主 err = %v", err)
	}
	if _, _, err := svc.PickForAttempt(context.Background(), "user-1", ""); errs.From(err).Code != errs.CodeValidation {
		t.Fatalf("空 attemptID err = %v", err)
	}
}
