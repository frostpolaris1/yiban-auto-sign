package assembly

// workerDeps 与 appworker.Deps 的两道镜像判据（§28，OCR C2 三轮 P3）。
//
// 1. 字段镜像：Depst 新增字段时，workerDeps 必须有同名同类型的字段。
// 2. 搬运镜像：buildWorkerDeps 必须把每个字段原样带进 Deps。
// 只做第 1 条不够——删掉一行赋值时字段仍在，编译通过且无人会红；
// 漏传可选端口是静默降级（选图缺失 → photo_missing 跳过，通知缺失 → 事件不发）。
// Limiter 由 buildWorkerDeps 派生，故两处都豁免。

import (
	"io"
	"log/slog"
	"reflect"
	"strings"
	"testing"
	"time"

	"github.com/frostpolaris1/yiban-auto-sign/server/internal/adapters/config"
	appworker "github.com/frostpolaris1/yiban-auto-sign/server/internal/app/worker"
)

func TestWorkerDepsMirrorsDeps(t *testing.T) {
	depsT := reflect.TypeOf(appworker.Deps{})
	wdT := reflect.TypeOf(workerDeps{})
	mirrored := make(map[string]bool, wdT.NumField())

	for i := 0; i < depsT.NumField(); i++ {
		f := depsT.Field(i)
		if f.Name == "Limiter" {
			continue
		}
		want := strings.ToLower(f.Name[:1]) + f.Name[1:]
		got, ok := wdT.FieldByName(want)
		if !ok {
			t.Errorf("workerDeps 缺字段 %s（appworker.Deps.%s）", want, f.Name)
			continue
		}
		mirrored[f.Name] = true
		if got.Type != f.Type {
			t.Errorf("workerDeps.%s 类型 = %v, want %v", want, got.Type, f.Type)
		}
	}
	for i := 0; i < wdT.NumField(); i++ {
		name := wdT.Field(i).Name
		exported := strings.ToUpper(name[:1]) + name[1:]
		if !mirrored[exported] {
			t.Errorf("workerDeps.%s 在 appworker.Deps 里没有对应字段", name)
		}
	}
}

// 搬运镜像：每个字段用可比较的哨兵值传入，必须原样出现在 Deps 里。
func TestWorkerDepsPassesEveryFieldThrough(t *testing.T) {
	loc := time.FixedZone("CST", 8*3600)
	logger := slog.New(slog.NewTextHandler(io.Discard, nil))
	want := workerDeps{
		store:     &fakeStoreSentinel{},
		creds:     &fakeCredsSentinel{},
		signer:    &fakeSignerSentinel{},
		egress:    &fakeEgressSentinel{},
		clock:     fixedClock{},
		location:  loc,
		locations: &fakeLocationsSentinel{},
		photos:    &fakePhotosSentinel{},
		notifier:  &fakeNotifierSentinel{},
		logger:    logger,
	}
	got := buildWorkerDeps(&config.Config{}, want)

	if got.Store != want.store {
		t.Error("Store 未原样搬运")
	}
	if got.Creds != want.creds {
		t.Error("Creds 未原样搬运")
	}
	if got.Signer != want.signer {
		t.Error("Signer 未原样搬运")
	}
	if got.Egress != want.egress {
		t.Error("Egress 未原样搬运")
	}
	if got.Clock != want.clock {
		t.Error("Clock 未原样搬运")
	}
	if got.Location != want.location {
		t.Error("Location 未原样搬运")
	}
	if got.Locations != want.locations {
		t.Error("Locations 未原样搬运")
	}
	if got.Photos != want.photos {
		t.Error("Photos 未原样搬运")
	}
	if got.Notifier != want.notifier {
		t.Error("Notifier 未原样搬运")
	}
	if got.Logger != want.logger {
		t.Error("Logger 未原样搬运")
	}
	if got.Limiter == nil {
		t.Error("Limiter 未派生")
	}
	// 覆盖率判据：want 必须为 workerDeps 的每个字段设哨兵，
	// 且每个哨兵必须原样出现在 Deps 里。新增字段漏进本判据即红——
	// 上面 10 条是手写枚举，只覆盖写明的那几个字段（OCR C2 四轮 P3）。
	// 用 IsZero 而不是 Interface()：workerDeps 字段未导出，取接口值会 panic。
	gotV, wantV := reflect.ValueOf(got), reflect.ValueOf(want)
	wt := reflect.TypeOf(want)
	for i := 0; i < wt.NumField(); i++ {
		name := wt.Field(i).Name
		if wantV.Field(i).IsZero() {
			t.Fatalf("want.%s 未设哨兵：workerDeps 新增字段必须纳入本判据", name)
		}
		gf := gotV.FieldByName(strings.ToUpper(name[:1]) + name[1:])
		if !gf.IsValid() || gf.IsZero() {
			t.Errorf("%s 未原样搬运", name)
		}
	}
}

// ---- 哨兵：只用于指针同一性比较，方法一律 panic ----

type fakeStoreSentinel struct{ appworker.Store }

type fakeCredsSentinel struct{ appworker.Credentials }

type fakeSignerSentinel struct{ appworker.Signer }

type fakeEgressSentinel struct{ appworker.Egress }

type fakeLocationsSentinel struct{ appworker.LocationSource }

type fakePhotosSentinel struct{ appworker.PhotoSource }

type fakeNotifierSentinel struct{ appworker.Notifier }

// fixedClock 是零值可比较的时钟哨兵。
type fixedClock struct{}

func (fixedClock) Now() time.Time { return time.Unix(0, 0) }
