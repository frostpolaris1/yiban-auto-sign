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
	"log/slog"
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
	// purgeCalls 记录 PurgeExpired 调用次数：用例必须循环到 0 行。
	purgeCalls int
	// purgeFailures 是前若干次清理的失败次数，用来钉「失败后继续」。
	purgeFailures int
	// purged 在首次真实删行后关闭，供循环用例等待。
	purged chan struct{}
	// refsEntered/refsRelease 让并发用例卡在引用查询里。
	refsEntered chan struct{}
	refsRelease chan struct{}
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

func (f *fakeStore) PurgeExpired(_ context.Context, now time.Time, limit int) (int64, error) {
	f.purgeCalls++
	if f.purgeFailures > 0 {
		f.purgeFailures--
		return 0, errors.New("purge failed")
	}
	var ids []string
	for id, rec := range f.recs {
		days := f.retention[rec.OwnerUserID]
		if days == 0 {
			days = corephoto.DefaultRetentionDays
		}
		if rec.Expired(now, days) {
			ids = append(ids, id)
		}
	}
	if limit > 0 && len(ids) > limit {
		ids = ids[:limit]
	}
	for _, id := range ids {
		delete(f.recs, id)
	}
	if len(ids) > 0 && f.purged != nil {
		select {
		case <-f.purged:
		default:
			close(f.purged)
		}
	}
	return int64(len(ids)), nil
}

// ReferencedDigests 返回全部行（含软删）仍引用的摘要。
// 设置了 refsEntered/refsRelease 时，先在通道上握手，供并发用例制造交错。
func (f *fakeStore) ReferencedDigests(context.Context) (map[[32]byte]struct{}, error) {
	if f.refsEntered != nil {
		close(f.refsEntered)
		<-f.refsRelease
	}
	out := make(map[[32]byte]struct{}, len(f.recs))
	for _, rec := range f.recs {
		out[rec.SHA256] = struct{}{}
	}
	return out, nil
}

// CountLibrary/LibraryAt 实现选图端口：本文件不测选图，
// 这里给最小实现（计数与按偏移取行）保持编译对齐。
func (f *fakeStore) CountLibrary(_ context.Context, ownerUserID string) (int64, error) {
	var n int64
	for _, rec := range f.recs {
		if !rec.Deleted() && rec.OwnerUserID == ownerUserID {
			n++
		}
	}
	return n, nil
}

func (f *fakeStore) LibraryAt(_ context.Context, ownerUserID string, offset int) (corephoto.Record, error) {
	var out []corephoto.Record
	for _, rec := range f.recs {
		if !rec.Deleted() && rec.OwnerUserID == ownerUserID {
			out = append(out, rec)
		}
	}
	if offset < 0 || offset >= len(out) {
		return corephoto.Record{}, errs.ErrNotFound
	}
	return out[offset], nil
}

type fakeBlobs struct {
	data map[[32]byte][]byte
	// mods 是每份 blob 的修改时间；缺省为 fixedNow。
	mods map[[32]byte]time.Time
	puts int
	fail error
	// removed 记录真实删除顺序，供断言。
	removed [][32]byte
	// removeMiss 模拟「清单里有、删除时已不在」：Remove 返回 false。
	removeMiss bool
	// touchErr 模拟 Touch 失败。
	touchErr error
}

func newFakeBlobs() *fakeBlobs {
	return &fakeBlobs{data: map[[32]byte][]byte{}, mods: map[[32]byte]time.Time{}}
}

func (f *fakeBlobs) Put(_ context.Context, sha [32]byte, data []byte) error {
	f.puts++
	if f.fail != nil {
		return f.fail
	}
	// 与 FS 同形：同一摘要重复写不重写文件，修改时间保持旧值。
	// 刷新只能由 Touch 完成，否则回收竞态用例没有判别力。
	if _, ok := f.data[sha]; ok {
		return nil
	}
	f.data[sha] = append([]byte(nil), data...)
	f.mods[sha] = fixedNow
	return nil
}

func (f *fakeBlobs) Open(_ context.Context, sha [32]byte) (io.ReadCloser, error) {
	data, ok := f.data[sha]
	if !ok {
		return nil, errors.New("blob not found")
	}
	return io.NopCloser(bytes.NewReader(data)), nil
}

func (f *fakeBlobs) List(context.Context) ([]corephoto.Blob, error) {
	out := make([]corephoto.Blob, 0, len(f.data))
	for sha := range f.data {
		mod := f.mods[sha]
		if mod.IsZero() {
			mod = fixedNow
		}
		out = append(out, corephoto.Blob{Digest: sha, ModTime: mod})
	}
	return out, nil
}

func (f *fakeBlobs) Touch(_ context.Context, sha [32]byte, at time.Time) error {
	if f.touchErr != nil {
		return f.touchErr
	}
	if _, ok := f.data[sha]; !ok {
		return errors.New("blob not found")
	}
	f.mods[sha] = at
	return nil
}

func (f *fakeBlobs) Remove(_ context.Context, sha [32]byte) (bool, error) {
	if f.removeMiss {
		return false, nil
	}
	if _, ok := f.data[sha]; !ok {
		return false, nil
	}
	delete(f.data, sha)
	delete(f.mods, sha)
	f.removed = append(f.removed, sha)
	return true, nil
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

// 保留期：缺省取 core/photo 的单一定义；闭集之外拒绝。
func TestRetention(t *testing.T) {
	svc := newService(t, newFakeStore(), newFakeBlobs())
	days, err := svc.Retention(context.Background(), owner("u1"))
	if err != nil || days != corephoto.DefaultRetentionDays {
		t.Fatalf("缺省保留期 = %d, %v", days, err)
	}
	if _, err := svc.SetRetention(context.Background(), owner("u1"), 45); err == nil {
		t.Fatal("非闭集值应被拒绝")
	}
	for _, want := range corephoto.RetentionDays {
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

// Purge 循环调用仓储直到 0 行：单次大事务会被批大小限制。
func TestPurgeLoopsUntilEmpty(t *testing.T) {
	store, blobs := newFakeStore(), newFakeBlobs()
	svc := newService(t, store, blobs)
	old := fixedNow.Add(-100 * 24 * time.Hour)
	for i := 0; i < 5; i++ {
		id := fmt.Sprintf("photo-%d", i)
		store.recs[id] = corephoto.Record{ID: id, OwnerUserID: "u1", CreatedAt: old}
	}
	n, err := svc.Purge(context.Background(), 2)
	if err != nil {
		t.Fatalf("purge: %v", err)
	}
	if n != 5 {
		t.Fatalf("删除行数 = %d, want 5", n)
	}
	if len(store.recs) != 0 {
		t.Fatalf("残留行 = %d", len(store.recs))
	}
	// 2+2+1 后必须再问一次拿 0，否则「删满一批」会被当成清空。
	if store.purgeCalls != 4 {
		t.Fatalf("清理调用次数 = %d, want 4", store.purgeCalls)
	}
}

// blob 回收只删无引用且超过安全期的文件。
// 引用集合含软删行；未满 1h 的新 blob 必须保留（上传事务未提交窗口）。
func TestCollectBlobsRespectsReferencesAndSafetyWindow(t *testing.T) {
	store, blobs := newFakeStore(), newFakeBlobs()
	svc := newService(t, store, blobs)

	live := digestOf(1)
	deleted := digestOf(2)
	fresh := digestOf(3)
	stale := digestOf(4)
	store.recs["photo-live"] = corephoto.Record{ID: "photo-live", OwnerUserID: "u1", SHA256: live}
	store.recs["photo-deleted"] = corephoto.Record{
		ID: "photo-deleted", OwnerUserID: "u1", SHA256: deleted,
		DeletedAt: &fixedNow,
	}
	for _, sha := range [][32]byte{live, deleted, fresh, stale} {
		blobs.data[sha] = []byte("blob")
	}
	// 被引用的 blob 也做旧：否则安全期会掩盖「漏查引用集合」的缺陷。
	blobs.mods[live] = fixedNow.Add(-2 * time.Hour)
	blobs.mods[deleted] = fixedNow.Add(-2 * time.Hour)
	blobs.mods[fresh] = fixedNow.Add(-30 * time.Minute)
	blobs.mods[stale] = fixedNow.Add(-2 * time.Hour)

	removed, err := svc.CollectBlobs(context.Background())
	if err != nil {
		t.Fatalf("collect: %v", err)
	}
	if removed != 1 || len(blobs.removed) != 1 || blobs.removed[0] != stale {
		t.Fatalf("删除数 = %d, 顺序 = %x", removed, blobs.removed)
	}
	for name, sha := range map[string][32]byte{"live": live, "deleted": deleted, "fresh": fresh} {
		if _, ok := blobs.data[sha]; !ok {
			t.Fatalf("%s blob 被误删", name)
		}
	}
}

// 去重上传必须刷新 blob 修改时间。
// 否则「删后重传」的文件仍是旧时间，回收方会在新行提交前删掉它。
func TestUploadRefreshesBlobTimestampOnDedup(t *testing.T) {
	store, blobs := newFakeStore(), newFakeBlobs()
	svc := newService(t, store, blobs)
	raw := pngBytes(t, 20, 20)

	first, err := svc.Upload(context.Background(), owner("u1"), raw)
	if err != nil {
		t.Fatalf("first upload: %v", err)
	}
	// Put 对已存在摘要不重写文件；把时间做旧，模拟很久以前落盘的那一份。
	blobs.mods[first.SHA256] = fixedNow.Add(-2 * time.Hour)

	if _, err := svc.Upload(context.Background(), owner("u1"), raw); err != nil {
		t.Fatalf("dedup upload: %v", err)
	}
	if got := blobs.mods[first.SHA256]; !got.Equal(fixedNow) {
		t.Fatalf("去重上传后修改时间 = %v, want %v（未刷新会被回收）", got, fixedNow)
	}
}

// 回收扫描与上传必须互斥。
// 没有互斥时，去重 Touch 可以落在 List 快照之后；
// 回收按过期 mtime 删文件，随后行提交，留下悬空行。
func TestCollectBlobsExcludesConcurrentUpload(t *testing.T) {
	store, blobs := newFakeStore(), newFakeBlobs()
	svc := newService(t, store, blobs)
	raw := pngBytes(t, 20, 20)

	rec, err := svc.Upload(context.Background(), owner("u1"), raw)
	if err != nil {
		t.Fatalf("seed upload: %v", err)
	}
	// 去掉行引用并把文件做旧：回收会把它当无引用旧件。
	delete(store.recs, rec.ID)
	delete(store.byKey, keyOf(rec))
	blobs.mods[rec.SHA256] = fixedNow.Add(-2 * time.Hour)

	store.refsEntered = make(chan struct{})
	store.refsRelease = make(chan struct{})
	gcDone := make(chan struct{})
	var gcRemoved int
	var gcErr error
	go func() {
		defer close(gcDone)
		gcRemoved, gcErr = svc.CollectBlobs(context.Background())
	}()
	<-store.refsEntered

	uploadDone := make(chan error, 1)
	go func() {
		_, err := svc.Upload(context.Background(), owner("u1"), raw)
		uploadDone <- err
	}()
	select {
	case err := <-uploadDone:
		t.Fatalf("上传未被回收锁挡住，交错窗口仍开着: %v", err)
	case <-time.After(100 * time.Millisecond):
	}

	close(store.refsRelease)
	<-gcDone
	if gcErr != nil {
		t.Fatalf("collect: %v", gcErr)
	}
	if gcRemoved != 1 {
		t.Fatalf("回收删除数 = %d, want 1", gcRemoved)
	}
	if err := <-uploadDone; err != nil {
		t.Fatalf("upload after collect: %v", err)
	}
	if _, ok := blobs.data[rec.SHA256]; !ok {
		t.Fatal("行已提交但 blob 被删：悬空行")
	}
	found := false
	for _, r := range store.recs {
		if r.SHA256 == rec.SHA256 {
			found = true
		}
	}
	if !found {
		t.Fatal("上传未落行")
	}
}

// Touch 失败时上传报错且不落行；已落盘的 blob 留给回收。
func TestUploadFailsWhenTouchFails(t *testing.T) {
	store, blobs := newFakeStore(), newFakeBlobs()
	svc := newService(t, store, blobs)
	blobs.touchErr = errors.New("chtimes failed")

	if _, err := svc.Upload(context.Background(), owner("u1"), pngBytes(t, 20, 20)); err == nil {
		t.Fatal("Touch 失败时上传必须报错")
	}
	if len(store.recs) != 0 {
		t.Fatalf("Touch 失败仍落了 %d 行", len(store.recs))
	}
}

// 回收计数只认真实删除：清单里有、删除时已不在的文件不计数。
func TestCollectBlobsCountsOnlyRealRemovals(t *testing.T) {
	store, blobs := newFakeStore(), newFakeBlobs()
	svc := newService(t, store, blobs)
	gone := digestOf(9)
	blobs.data[gone] = []byte("blob")
	blobs.mods[gone] = fixedNow.Add(-2 * time.Hour)
	blobs.removeMiss = true

	removed, err := svc.CollectBlobs(context.Background())
	if err != nil {
		t.Fatalf("collect: %v", err)
	}
	if removed != 0 {
		t.Fatalf("删除数 = %d, want 0（文件已不在，不得计入）", removed)
	}
}

// Sweep 先删行再回收 blob；结果分别计数。
func TestSweepReportsRowsAndBlobs(t *testing.T) {
	store, blobs := newFakeStore(), newFakeBlobs()
	svc := newService(t, store, blobs)
	old := digestOf(5)
	store.recs["photo-old"] = corephoto.Record{ID: "photo-old", OwnerUserID: "u1", SHA256: old, CreatedAt: fixedNow.Add(-100 * 24 * time.Hour)}
	blobs.data[old] = []byte("blob")
	blobs.mods[old] = fixedNow.Add(-2 * time.Hour)

	res, err := svc.Sweep(context.Background(), 10)
	if err != nil {
		t.Fatalf("sweep: %v", err)
	}
	if res.RowsDeleted != 1 || res.BlobsDeleted != 1 {
		t.Fatalf("结果 = %+v, want 1 row 1 blob", res)
	}
}

// 循环启动即清理；一次失败不终止循环；取消后 1s 内返回。
func TestRetentionLoopRetriesAfterErrorAndStops(t *testing.T) {
	store, blobs := newFakeStore(), newFakeBlobs()
	svc := newService(t, store, blobs)
	store.recs["photo-old"] = corephoto.Record{
		ID: "photo-old", OwnerUserID: "u1",
		CreatedAt: fixedNow.Add(-100 * 24 * time.Hour),
	}
	store.purgeFailures = 1
	store.purged = make(chan struct{})
	loop, err := appphotos.NewRetentionLoop(appphotos.RetentionOptions{
		Service:  svc,
		Logger:   slog.New(slog.NewTextHandler(io.Discard, nil)),
		Interval: 10 * time.Millisecond,
		Batch:    10,
	})
	if err != nil {
		t.Fatalf("NewRetentionLoop: %v", err)
	}

	ctx, cancel := context.WithCancel(context.Background())
	done := make(chan struct{})
	go func() { defer close(done); loop.Run(ctx) }()

	select {
	case <-store.purged:
	case <-time.After(2 * time.Second):
		cancel()
		t.Fatal("失败后循环没有继续下一轮")
	}
	cancel()
	select {
	case <-done:
	case <-time.After(time.Second):
		t.Fatal("取消后 Run 未在 1s 内返回")
	}
	if _, ok := store.recs["photo-old"]; ok {
		t.Fatal("到期行仍在")
	}
	if store.purgeCalls < 2 {
		t.Fatalf("清理调用次数 = %d, want >= 2", store.purgeCalls)
	}
}

// digestOf 构造形状合法的测试摘要。
func digestOf(seed byte) [32]byte {
	var sha [32]byte
	for i := range sha {
		sha[i] = seed
	}
	return sha
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
