package worker_test

// P5-C 注入判据：定位策略与图片进入 SignRequest；
// 缺图与越界在提交前短路，不发起登录。

import (
	"context"
	"errors"
	"testing"

	appworker "github.com/frostpolaris1/yiban-auto-sign/server/internal/app/worker"
	coreclock "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/clock"
	corelocation "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/location"
	coreschedule "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/schedule"
	corestatus "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/status"
	"github.com/frostpolaris1/yiban-auto-sign/server/internal/signing/contract"
)

// ---- 注入替身 ----

// fakeLocations 按账号返回定位策略；err 模拟基础设施失败。
type fakeLocations struct {
	byAccount map[string]contract.Location
	err       error
	calls     int
}

func (f *fakeLocations) Resolve(_ context.Context, accountID string) (contract.Location, error) {
	f.calls++
	if f.err != nil {
		return contract.Location{}, f.err
	}
	if loc, ok := f.byAccount[accountID]; ok {
		return loc, nil
	}
	return contract.Location{Mode: contract.LocationAuto}, nil
}

// fakePhotos 按 attemptID 确定性返回图；picked=false 模拟空库。
type fakePhotos struct {
	pick   func(attemptID string) (contract.ImageRef, bool)
	err    error
	calls  []string
	owners []string
}

func (f *fakePhotos) PickForAttempt(_ context.Context, ownerUserID, attemptID string) (contract.ImageRef, bool, error) {
	f.calls = append(f.calls, attemptID)
	f.owners = append(f.owners, ownerUserID)
	if f.err != nil {
		return contract.ImageRef{}, false, f.err
	}
	if f.pick == nil {
		return contract.ImageRef{}, false, nil
	}
	ref, ok := f.pick(attemptID)
	return ref, ok, nil
}

func fixedLoc(lng, lat float64, reason string) contract.Location {
	return contract.Location{Mode: contract.LocationFixed, Lng: lng, Lat: lat, Reason: reason}
}

// 城市围栏：多边形 lng 100..110，lat 20..30。
func fencedTarget() contract.Target {
	return contract.Target{
		ID:      "pos-1",
		Polygon: []contract.Point{{Lng: 100, Lat: 20}, {Lng: 110, Lat: 20}, {Lng: 110, Lat: 30}, {Lng: 100, Lat: 30}},
	}
}

func injectService(t *testing.T, st *fakeStore, signer *fakeSigner, locs *fakeLocations, photos *fakePhotos) *appworker.Service {
	t.Helper()
	svc, err := appworker.NewService(appworker.Deps{
		Store: st, Creds: fakeCreds{}, Signer: signer, Egress: fakeEgress{},
		Limiter: fakeLimiter{allow: true}, Clock: coreclock.Fixed{T: testNow()}, Location: loc,
		Locations: locs, Photos: photos,
	})
	if err != nil {
		t.Fatalf("NewService: %v", err)
	}
	return svc
}

func taskWithOwner() coreschedule.TaskRow {
	task := baseTask()
	task.OwnerUserID = "user-1"
	return task
}

// 定位端口为 nil：保持现行为，Location 恒 LocationAuto。
func TestNilLocationSourceKeepsAuto(t *testing.T) {
	st := newStore(taskWithOwner())
	st.windows["st-1|2026-10-29"] = openWindow()
	signer := defaultSigner()
	svc, err := appworker.NewService(appworker.Deps{
		Store: st, Creds: fakeCreds{}, Signer: signer, Egress: fakeEgress{},
		Limiter: fakeLimiter{allow: true}, Clock: coreclock.Fixed{T: testNow()}, Location: loc,
	})
	if err != nil {
		t.Fatalf("NewService: %v", err)
	}
	if _, err := svc.Step(context.Background(), 10); err != nil {
		t.Fatalf("Step: %v", err)
	}
	if signer.requests[0].Location != (contract.Location{Mode: contract.LocationAuto}) {
		t.Fatalf("Location = %+v, want auto", signer.requests[0].Location)
	}
}

// 定位端口解析失败是基础设施错误：上抛，任务退回队列。
func TestLocationInfraErrorPropagates(t *testing.T) {
	st := newStore(taskWithOwner())
	st.windows["st-1|2026-10-29"] = openWindow()
	locs := &fakeLocations{err: errors.New("db down")}
	svc := injectService(t, st, defaultSigner(), locs, &fakePhotos{})
	if _, err := svc.Step(context.Background(), 10); err == nil {
		t.Fatal("定位基础设施错误应上抛")
	}
	if _, requeued := st.requeued["t1"]; !requeued {
		t.Fatal("任务应退回队列")
	}
}

// fixed 在围栏内：SignRequest 带固定点坐标。
func TestFixedLocationInjected(t *testing.T) {
	st := newStore(taskWithOwner())
	st.windows["st-1|2026-10-29"] = openWindow()
	signer := defaultSigner()
	signer.targets = []contract.Target{fencedTarget()}
	locs := &fakeLocations{byAccount: map[string]contract.Location{
		"acct-1": fixedLoc(105, 25, ""),
	}}
	svc := injectService(t, st, signer, locs, &fakePhotos{})
	if _, err := svc.Step(context.Background(), 10); err != nil {
		t.Fatalf("Step: %v", err)
	}
	want := fixedLoc(105, 25, "")
	if signer.requests[0].Location != want {
		t.Fatalf("Location = %+v, want %+v", signer.requests[0].Location, want)
	}
}

// fixed 在围栏外且无原因：skipped/location_out_of_range，不提交签到。
func TestOutOfRangeWithoutReasonSkips(t *testing.T) {
	st := newStore(taskWithOwner())
	st.windows["st-1|2026-10-29"] = openWindow()
	signer := defaultSigner()
	signer.targets = []contract.Target{fencedTarget()}
	locs := &fakeLocations{byAccount: map[string]contract.Location{
		"acct-1": fixedLoc(0, 0, ""),
	}}
	svc := injectService(t, st, signer, locs, &fakePhotos{})
	if _, err := svc.Step(context.Background(), 10); err != nil {
		t.Fatalf("Step: %v", err)
	}
	if st.finished["t1"] != corestatus.TaskSkipped || st.results["t1"] != "location_out_of_range" {
		t.Fatalf("终态 = %v/%q, want skipped/location_out_of_range", st.finished["t1"], st.results["t1"])
	}
	if signer.signs != 0 {
		t.Fatalf("越界无原因仍提交了 %d 次签到", signer.signs)
	}
}

// fixed 在围栏外但有原因：放行并携带原因进入 SignRequest。
func TestOutOfRangeWithReasonProceeds(t *testing.T) {
	st := newStore(taskWithOwner())
	st.windows["st-1|2026-10-29"] = openWindow()
	signer := defaultSigner()
	signer.targets = []contract.Target{fencedTarget()}
	locs := &fakeLocations{byAccount: map[string]contract.Location{
		"acct-1": fixedLoc(0, 0, "在校门口，定位漂移"),
	}}
	svc := injectService(t, st, signer, locs, &fakePhotos{})
	if _, err := svc.Step(context.Background(), 10); err != nil {
		t.Fatalf("Step: %v", err)
	}
	if signer.signs != 1 {
		t.Fatalf("带原因应放行, signs = %d", signer.signs)
	}
	if signer.requests[0].Location.Reason != "在校门口，定位漂移" {
		t.Fatalf("Reason = %q", signer.requests[0].Location.Reason)
	}
	if st.finished["t1"] != corestatus.TaskSuccess {
		t.Fatalf("终态 = %v", st.finished["t1"])
	}
}

// auto 定位不做围栏判定：任意目标都放行。
func TestAutoLocationSkipsFenceCheck(t *testing.T) {
	st := newStore(taskWithOwner())
	st.windows["st-1|2026-10-29"] = openWindow()
	signer := defaultSigner()
	signer.targets = []contract.Target{fencedTarget()}
	locs := &fakeLocations{} // 缺省 auto
	svc := injectService(t, st, signer, locs, &fakePhotos{})
	if _, err := svc.Step(context.Background(), 10); err != nil {
		t.Fatalf("Step: %v", err)
	}
	if signer.signs != 1 {
		t.Fatalf("auto 应直接放行, signs = %d", signer.signs)
	}
}

// 目标要求图片且库空：skipped/photo_missing，不提交签到。
func TestPhotoMissingSkipsBeforeSign(t *testing.T) {
	st := newStore(taskWithOwner())
	st.windows["st-1|2026-10-29"] = openWindow()
	signer := defaultSigner()
	target := fencedTarget()
	target.RequiresPhoto = true
	signer.targets = []contract.Target{target}
	svc := injectService(t, st, signer, &fakeLocations{}, &fakePhotos{})
	if _, err := svc.Step(context.Background(), 10); err != nil {
		t.Fatalf("Step: %v", err)
	}
	if st.finished["t1"] != corestatus.TaskSkipped || st.results["t1"] != string(contract.OutcomePhotoMissing) {
		t.Fatalf("终态 = %v/%q, want skipped/photo_missing", st.finished["t1"], st.results["t1"])
	}
	if signer.signs != 0 {
		t.Fatalf("缺图仍提交了 %d 次签到", signer.signs)
	}
}

// 目标要求图片且选图成功：SignRequest.Image 带完整元数据。
func TestPhotoInjected(t *testing.T) {
	st := newStore(taskWithOwner())
	st.windows["st-1|2026-10-29"] = openWindow()
	signer := defaultSigner()
	target := fencedTarget()
	target.RequiresPhoto = true
	signer.targets = []contract.Target{target}
	photos := &fakePhotos{pick: func(string) (contract.ImageRef, bool) {
		return contract.ImageRef{ID: "photo-9", Mime: "image/png", Width: 12, Height: 8,
			SHA256: "abcd", Bytes: []byte("png-bytes")}, true
	}}
	svc := injectService(t, st, signer, &fakeLocations{}, photos)
	if _, err := svc.Step(context.Background(), 10); err != nil {
		t.Fatalf("Step: %v", err)
	}
	if signer.signs != 1 {
		t.Fatalf("有图应提交签到, signs = %d", signer.signs)
	}
	img := signer.requests[0].Image
	if img == nil || img.ID != "photo-9" || string(img.Bytes) != "png-bytes" {
		t.Fatalf("Image = %+v", img)
	}
	// 选图必须按属主取：OwnerUserID 来自杀到行。
	if len(photos.owners) != 1 || photos.owners[0] != "user-1" {
		t.Fatalf("owners = %v, want [user-1]", photos.owners)
	}
	if len(photos.calls) != 1 || photos.calls[0] != "t1#0" {
		t.Fatalf("attemptIDs = %v", photos.calls)
	}
}

// 选图基础设施失败：上抛并退回队列，不提交签到。
func TestPhotoInfraErrorPropagates(t *testing.T) {
	st := newStore(taskWithOwner())
	st.windows["st-1|2026-10-29"] = openWindow()
	signer := defaultSigner()
	target := fencedTarget()
	target.RequiresPhoto = true
	signer.targets = []contract.Target{target}
	svc := injectService(t, st, signer, &fakeLocations{}, &fakePhotos{err: errors.New("blob gone")})
	if _, err := svc.Step(context.Background(), 10); err == nil {
		t.Fatal("选图基础设施错误应上抛")
	}
	if signer.signs != 0 {
		t.Fatal("选图失败不应提交签到")
	}
	if _, requeued := st.requeued["t1"]; !requeued {
		t.Fatal("任务应退回队列")
	}
}

// 目标不要求图片：不调用选图，Image 为 nil（现行为）。
func TestNoPhotoRequiredKeepsNilImage(t *testing.T) {
	st := newStore(taskWithOwner())
	st.windows["st-1|2026-10-29"] = openWindow()
	signer := defaultSigner()
	photos := &fakePhotos{pick: func(string) (contract.ImageRef, bool) {
		return contract.ImageRef{ID: "photo-9"}, true
	}}
	svc := injectService(t, st, signer, &fakeLocations{}, photos)
	if _, err := svc.Step(context.Background(), 10); err != nil {
		t.Fatalf("Step: %v", err)
	}
	if signer.requests[0].Image != nil {
		t.Fatalf("不要求图片却带了图: %+v", signer.requests[0].Image)
	}
	if len(photos.calls) != 0 {
		t.Fatalf("不要求图片却调用了选图: %v", photos.calls)
	}
}

// 围栏判定本身经 core/location 复算一次（半径恰好按在内）。
func TestFenceBoundaryStaysInside(t *testing.T) {
	target := contract.Target{Center: contract.Point{Lng: 116.4, Lat: 39.9}, RadiusM: 500}
	loc := fixedLoc(116.4, 39.9+500.0/111194.93, "")
	if !corelocation.Resolve(target, loc) {
		t.Fatal("半径恰好的点应判在内")
	}
}
