package photos_test

import (
	"bytes"
	"context"
	"errors"
	"fmt"
	"image"
	"image/color"
	"image/png"
	"io"
	"testing"
	"time"

	appphotos "github.com/frostpolaris1/yiban-auto-sign/server/internal/app/photos"
	coreclock "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/clock"
	"github.com/frostpolaris1/yiban-auto-sign/server/internal/core/errs"
	corephoto "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/photo"
	corescope "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/scope"
)

// ---- 测试替身 ----

type fakeStore struct {
	recs      map[string]corephoto.Record
	byKey     map[string]string
	retention map[string]int
	next      int
}

func newFakeStore() *fakeStore {
	return &fakeStore{
		recs:      map[string]corephoto.Record{},
		byKey:     map[string]string{},
		retention: map[string]int{},
	}
}

func keyOf(rec corephoto.Record) string { return fmt.Sprintf("%s/%x", rec.OwnerUserID, rec.SHA256) }

func (f *fakeStore) Create(_ context.Context, rec corephoto.Record) (corephoto.Record, error) {
	key := keyOf(rec)
	if id, ok := f.byKey[key]; ok {
		existing := f.recs[id]
		if existing.Deleted() {
			existing.DeletedAt = nil
			existing.CreatedAt = rec.CreatedAt
			f.recs[id] = existing
		}
		return existing, nil
	}
	f.next++
	rec.ID = "photo-" + string(rune('0'+f.next))
	f.recs[rec.ID] = rec
	f.byKey[key] = rec.ID
	return rec, nil
}

func (f *fakeStore) Get(_ context.Context, scope corescope.Scope, id string) (corephoto.Record, error) {
	if scope.Kind() != corescope.KindUser {
		return corephoto.Record{}, errs.ErrNotFound
	}
	rec, ok := f.recs[id]
	if !ok || rec.Deleted() || rec.OwnerUserID != scope.UserID() {
		return corephoto.Record{}, errs.ErrNotFound
	}
	return rec, nil
}

func (f *fakeStore) List(_ context.Context, scope corescope.Scope, _ string, _ int) ([]corephoto.Record, string, error) {
	if scope.Kind() != corescope.KindUser {
		return nil, "", errs.ErrNotFound
	}
	var out []corephoto.Record
	for _, rec := range f.recs {
		if rec.Deleted() || rec.OwnerUserID != scope.UserID() {
			continue
		}
		out = append(out, rec)
	}
	return out, "", nil
}

func (f *fakeStore) SoftDelete(_ context.Context, scope corescope.Scope, id string, now time.Time) (corephoto.Record, error) {
	if scope.Kind() != corescope.KindUser {
		return corephoto.Record{}, errs.ErrNotFound
	}
	rec, ok := f.recs[id]
	if !ok || rec.OwnerUserID != scope.UserID() {
		return corephoto.Record{}, errs.ErrNotFound
	}
	if !rec.Deleted() {
		rec.DeletedAt = &now
		f.recs[id] = rec
	}
	return rec, nil
}

func (f *fakeStore) Retention(_ context.Context, userID string) (int, error) {
	return f.retention[userID], nil
}

func (f *fakeStore) PutRetention(_ context.Context, userID string, days int, _ time.Time) error {
	f.retention[userID] = days
	return nil
}

func (f *fakeStore) PurgeExpired(_ context.Context, now time.Time, _ int) (int64, error) {
	var n int64
	for id, rec := range f.recs {
		days := f.retention[rec.OwnerUserID]
		if days == 0 {
			days = corephoto.DefaultRetentionDays
		}
		if rec.Expired(now, days) {
			delete(f.recs, id)
			n++
		}
	}
	return n, nil
}

type fakeBlobs struct {
	data map[[32]byte][]byte
	puts int
	fail error
}

func newFakeBlobs() *fakeBlobs { return &fakeBlobs{data: map[[32]byte][]byte{}} }

func (f *fakeBlobs) Put(_ context.Context, sha [32]byte, data []byte) error {
	f.puts++
	if f.fail != nil {
		return f.fail
	}
	f.data[sha] = append([]byte(nil), data...)
	return nil
}

func (f *fakeBlobs) Open(_ context.Context, sha [32]byte) (io.ReadCloser, error) {
	data, ok := f.data[sha]
	if !ok {
		return nil, errors.New("blob not found")
	}
	return io.NopCloser(bytes.NewReader(data)), nil
}

// ---- 辅助 ----

var fixedNow = time.Date(2026, 10, 27, 8, 0, 0, 0, time.UTC)

func pngBytes(t *testing.T, w, h int) []byte {
	t.Helper()
	img := image.NewNRGBA(image.Rect(0, 0, w, h))
	for y := 0; y < h; y++ {
		for x := 0; x < w; x++ {
			img.SetNRGBA(x, y, color.NRGBA{R: 10, G: 20, B: 30, A: 255})
		}
	}
	var buf bytes.Buffer
	if err := png.Encode(&buf, img); err != nil {
		t.Fatalf("encode png: %v", err)
	}
	return buf.Bytes()
}

func newService(t *testing.T, store *fakeStore, blobs *fakeBlobs) *appphotos.Service {
	t.Helper()
	svc, err := appphotos.NewService(appphotos.Deps{
		Store: store, Blobs: blobs, Clock: coreclock.Fixed{T: fixedNow},
		Options: appphotos.Options{MaxPixels: 4_000_000, MaxSidePx: 4096},
	})
	if err != nil {
		t.Fatalf("NewService: %v", err)
	}
	return svc
}

func owner(id string) corescope.Scope { return corescope.User(id) }

// ---- 用例 ----

// 上传：预处理后入库，所有者来自作用域，blob 先写。
func TestUploadStoresImage(t *testing.T) {
	store, blobs := newFakeStore(), newFakeBlobs()
	svc := newService(t, store, blobs)
	raw := pngBytes(t, 12, 8)
	rec, err := svc.Upload(context.Background(), owner("u1"), raw)
	if err != nil {
		t.Fatalf("Upload: %v", err)
	}
	if rec.OwnerUserID != "u1" || rec.Mime != "image/png" || rec.Width != 12 || rec.Height != 8 {
		t.Fatalf("record = %+v", rec)
	}
	if rec.Bytes <= 0 || !rec.CreatedAt.Equal(fixedNow) {
		t.Fatalf("record = %+v", rec)
	}
	if blobs.puts != 1 || len(blobs.data[rec.SHA256]) == 0 {
		t.Fatalf("blob 未写入: puts=%d", blobs.puts)
	}
}

// 无效字节与空输入必须报 validation，且不写 blob。
func TestUploadRejectsInvalid(t *testing.T) {
	for _, raw := range [][]byte{nil, []byte("not an image")} {
		store, blobs := newFakeStore(), newFakeBlobs()
		svc := newService(t, store, blobs)
		_, err := svc.Upload(context.Background(), owner("u1"), raw)
		if err == nil {
			t.Fatal("应报错")
		}
		if code := errs.From(err).Code; code != errs.CodeValidation {
			t.Fatalf("错误码 = %q", code)
		}
		if blobs.puts != 0 {
			t.Fatal("校验失败不得写 blob")
		}
	}
}

// 上传只给 user 角色；admin 与无效作用域都拒绝。
func TestUploadRoleGate(t *testing.T) {
	store, blobs := newFakeStore(), newFakeBlobs()
	svc := newService(t, store, blobs)
	raw := pngBytes(t, 4, 4)
	if _, err := svc.Upload(context.Background(), corescope.Admin([]string{"s1"}), raw); err == nil {
		t.Fatal("admin 上传应被拒绝")
	} else if code := errs.From(err).Code; code != errs.CodeForbidden {
		t.Fatalf("错误码 = %q, want forbidden", code)
	}
	if _, err := svc.Upload(context.Background(), corescope.Scope{}, raw); err == nil {
		t.Fatal("无效作用域应被拒绝")
	}
}

// 同一用户重复上传同一字节返回同一行；不同用户各自一行。
func TestUploadDedupesPerOwner(t *testing.T) {
	store, blobs := newFakeStore(), newFakeBlobs()
	svc := newService(t, store, blobs)
	raw := pngBytes(t, 6, 6)
	first, err := svc.Upload(context.Background(), owner("u1"), raw)
	if err != nil {
		t.Fatalf("first: %v", err)
	}
	second, err := svc.Upload(context.Background(), owner("u1"), raw)
	if err != nil {
		t.Fatalf("second: %v", err)
	}
	if first.ID != second.ID || len(store.recs) != 1 {
		t.Fatalf("未去重: %s vs %s, rows=%d", first.ID, second.ID, len(store.recs))
	}
	other, err := svc.Upload(context.Background(), owner("u2"), raw)
	if err != nil {
		t.Fatalf("other owner: %v", err)
	}
	if other.ID == first.ID {
		t.Fatal("不同所有者不应共用行")
	}
}

// 跨用户读取与已软删读取都返回 not_found。
func TestGetScopesAndDelete(t *testing.T) {
	store, blobs := newFakeStore(), newFakeBlobs()
	svc := newService(t, store, blobs)
	rec, err := svc.Upload(context.Background(), owner("u1"), pngBytes(t, 5, 5))
	if err != nil {
		t.Fatalf("upload: %v", err)
	}
	if _, err := svc.Get(context.Background(), owner("u2"), rec.ID); !errors.Is(err, errs.ErrNotFound) {
		t.Fatalf("跨用户错误 = %v", err)
	}
	deleted, err := svc.Delete(context.Background(), owner("u1"), rec.ID)
	if err != nil {
		t.Fatalf("delete: %v", err)
	}
	if !deleted.Deleted() {
		t.Fatal("删除后 DeletedAt 为空")
	}
	if _, err := svc.Get(context.Background(), owner("u1"), rec.ID); !errors.Is(err, errs.ErrNotFound) {
		t.Fatalf("删除后仍可读: %v", err)
	}
	again, err := svc.Delete(context.Background(), owner("u1"), rec.ID)
	if err != nil || again.ID != rec.ID {
		t.Fatalf("重复删除应幂等: %v, %+v", err, again)
	}
	// 重新上传同字节应恢复同一行。
	restored, err := svc.Upload(context.Background(), owner("u1"), pngBytes(t, 5, 5))
	if err != nil {
		t.Fatalf("restore: %v", err)
	}
	if restored.ID != rec.ID || restored.Deleted() {
		t.Fatalf("未恢复同一行: %+v", restored)
	}
}

// 保留期：缺省 90；闭集之外拒绝。
func TestRetention(t *testing.T) {
	svc := newService(t, newFakeStore(), newFakeBlobs())
	days, err := svc.Retention(context.Background(), owner("u1"))
	if err != nil || days != corephoto.DefaultRetentionDays {
		t.Fatalf("缺省保留期 = %d, %v", days, err)
	}
	if _, err := svc.SetRetention(context.Background(), owner("u1"), 45); err == nil {
		t.Fatal("非闭集值应被拒绝")
	}
	for _, want := range []int{30, 90, 180, 365} {
		got, err := svc.SetRetention(context.Background(), owner("u1"), want)
		if err != nil || got != want {
			t.Fatalf("set %d = %d, %v", want, got, err)
		}
	}
	if _, err := svc.Retention(context.Background(), corescope.Admin([]string{"s1"})); err == nil {
		t.Fatal("admin 不应有保留期端点")
	}
}

// 到期清理按用户保留期执行；未到期的行保留。
func TestPurgeExpired(t *testing.T) {
	store, blobs := newFakeStore(), newFakeBlobs()
	svc := newService(t, store, blobs)
	old := fixedNow.Add(-100 * 24 * time.Hour)
	store.recs["photo-old"] = corephoto.Record{ID: "photo-old", OwnerUserID: "u1", CreatedAt: old}
	store.recs["photo-new"] = corephoto.Record{ID: "photo-new", OwnerUserID: "u1", CreatedAt: fixedNow}
	n, err := svc.Purge(context.Background(), 10)
	if err != nil {
		t.Fatalf("purge: %v", err)
	}
	if n != 1 {
		t.Fatalf("删除行数 = %d, want 1", n)
	}
	if _, ok := store.recs["photo-new"]; !ok {
		t.Fatal("未到期行被删")
	}
}

// Open 返回可读字节流；跨用户被拒绝。
func TestOpenStreamsBlob(t *testing.T) {
	store, blobs := newFakeStore(), newFakeBlobs()
	svc := newService(t, store, blobs)
	rec, err := svc.Upload(context.Background(), owner("u1"), pngBytes(t, 7, 7))
	if err != nil {
		t.Fatalf("upload: %v", err)
	}
	got, body, err := svc.Open(context.Background(), owner("u1"), rec.ID)
	if err != nil {
		t.Fatalf("open: %v", err)
	}
	defer body.Close()
	if got.ID != rec.ID {
		t.Fatalf("record = %+v", got)
	}
	raw, err := io.ReadAll(body)
	if err != nil || len(raw) == 0 {
		t.Fatalf("read = %d bytes, %v", len(raw), err)
	}
	if _, _, err := svc.Open(context.Background(), owner("u2"), rec.ID); !errors.Is(err, errs.ErrNotFound) {
		t.Fatalf("跨用户 open = %v", err)
	}
}

// admin 与 master 不得读、列、删用户的图片：本切片没有账号—图片关系。
// 这条钉同时是 P5-B pg 实现的规格：非 user 作用域返回 not_found。
func TestAdminCannotTouchPhotos(t *testing.T) {
	store, blobs := newFakeStore(), newFakeBlobs()
	svc := newService(t, store, blobs)
	rec, err := svc.Upload(context.Background(), owner("u1"), pngBytes(t, 5, 5))
	if err != nil {
		t.Fatalf("upload: %v", err)
	}
	for name, scope := range map[string]corescope.Scope{
		"admin":  corescope.Admin([]string{"s1"}),
		"master": corescope.Master(),
	} {
		if _, err := svc.Get(context.Background(), scope, rec.ID); !errors.Is(err, errs.ErrNotFound) {
			t.Fatalf("%s Get = %v, want not_found", name, err)
		}
		if _, _, err := svc.Open(context.Background(), scope, rec.ID); !errors.Is(err, errs.ErrNotFound) {
			t.Fatalf("%s Open = %v, want not_found", name, err)
		}
		if _, err := svc.Delete(context.Background(), scope, rec.ID); err == nil {
			t.Fatalf("%s Delete 应被拒绝", name)
		} else if code := errs.From(err).Code; code != errs.CodeForbidden {
			t.Fatalf("%s Delete 错误码 = %q, want forbidden", name, code)
		}
		if _, err := svc.List(context.Background(), scope, "", 10); err == nil {
			t.Fatalf("%s List 应被拒绝", name)
		} else if code := errs.From(err).Code; code != errs.CodeForbidden {
			t.Fatalf("%s List 错误码 = %q, want forbidden", name, code)
		}
	}
}
