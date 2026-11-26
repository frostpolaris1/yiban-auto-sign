package yiban

import (
	"context"
	"encoding/json"
	"fmt"
	"testing"
	"time"

	"github.com/frostpolaris1/yiban-auto-sign/server/internal/signing/conformance"
	"github.com/frostpolaris1/yiban-auto-sign/server/internal/signing/contract"
)

// yibanHarness 是真实 Provider 的符合性夹具。
//
// 它只补 Provider 不该知道的两件事：每个结果的上游布种方式、
// 以及默认期望。**绝不覆写 Sign**：判据必须打在真实签到路径上，
// 否则夹具会把实现缺陷洗成通过。
type yibanHarness struct {
	*Provider
	t  *testing.T
	up *upstream

	// signInDelay 是复位后仍要保留的 /signIn 延迟：
	// sign.timeout 的在途取消要穿透真实客户端。
	signInDelay time.Duration
}

// seedRequestTime 是矩阵请求的唯一时间点，与符合性套件同值。
var seedRequestTime = time.Date(2026, 10, 26, 6, 0, 0, 0, time.UTC)

// newYibanHarness 为一个结果构造独立上游与独立客户端。
// **每个结果独立上游**：假上游状态与客户端缓存都不得跨结果串味。
func newYibanHarness(t *testing.T) *yibanHarness {
	t.Helper()
	const delay = 30 * time.Millisecond
	up := newUpstream(t)
	up.signInDelay = delay
	return &yibanHarness{
		// 显式打开测试钩子：假上游是 http://127.0.0.1 的 httptest。
		Provider:    NewProvider(New(Config{Endpoints: up.endpoints(), allowLoopbackUpload: true})),
		t:           t,
		up:          up,
		signInDelay: delay,
	}
}

// ExpectedOutcome 声明默认期望：夹具上游默认全部正常，签到结果应为 success。
// 布种后的实际期望由 checkSignResult 用 SeedOutcome 的返回值单独核对。
func (h *yibanHarness) ExpectedOutcome(contract.SignRequest) (contract.Outcome, bool) {
	return contract.OutcomeSuccess, true
}

// reset 把上游复位到夹具原件，避免上一个结果的改动串到下一个。
func (h *yibanHarness) reset() {
	h.up.mu.Lock()
	defer h.up.mu.Unlock()
	h.up.challenge = false
	h.up.usersureCode = ""
	h.up.signPositionBody = h.up.signPositionFixture
	h.up.deviceBody = h.up.deviceFixture
	h.up.signInBody = h.up.signInFixture
	h.up.signInStatus = 0
	h.up.uploadBody = nil
	h.up.putStatus = 0
	h.up.signInDelay = h.signInDelay
}

// SeedOutcome 按契约结果布置真实上游。
//
// 14 项可复现；photo_missing 因 Caps.ImageRequired=false 返回 false，
// 属于契约豁免（由符合性套件显性跳过，而不是整条判据消失）。
// 注意 invalid_target/photo_rejected 需要改请求：缺目标、缺图片字节
// 都是**请求侧**的场景，上游无从布置。
func (h *yibanHarness) SeedOutcome(req *contract.SignRequest, want contract.Outcome) bool {
	h.reset()
	switch want {
	case contract.OutcomeSuccess:
		// 夹具默认即成功路径。
	case contract.OutcomeAlreadyDone:
		h.up.setSignPositionState(h.t, 3)
	case contract.OutcomePendingUpstream:
		h.up.setSignPositionState(h.t, 5)
	case contract.OutcomeNoTarget:
		h.up.setSignPositionState(h.t, 2)
	case contract.OutcomeNoPosition:
		h.up.emptySignPosition(h.t)
	case contract.OutcomeInvalidTarget:
		if req != nil {
			req.Location = contract.Location{}
			req.Target.Center = contract.Point{}
		}
	case contract.OutcomeCredentialBad:
		// usersure 返回业务失败码：登录链在第二跳判 credential_invalid。
		h.up.mu.Lock()
		h.up.usersureCode = "e001"
		h.up.mu.Unlock()
	case contract.OutcomeDeviceUnbound:
		// 设备码为空 → 提供方回查设备态；设备态也无码 → device_unbound。
		if req != nil {
			req.Credential.DeviceCode = ""
		}
		h.clearDeviceCode()
	case contract.OutcomeChallenge:
		// 挑战页双 JS 特征：WAF 判定先于状态码。
		h.up.mu.Lock()
		h.up.signPositionBody = []byte(doubleJSSetup + doubleJSEval)
		h.up.mu.Unlock()
	case contract.OutcomeBlocked:
		h.up.mu.Lock()
		h.up.signInBody = []byte("<html><body>风险访问</body></html>")
		h.up.mu.Unlock()
	case contract.OutcomeTransient:
		h.up.mu.Lock()
		h.up.signInStatus = 500
		h.up.mu.Unlock()
	case contract.OutcomeRejected:
		h.up.mu.Lock()
		h.up.signInBody = []byte(`{"code":-1,"msg":"不在签到时间"}`)
		h.up.mu.Unlock()
	case contract.OutcomePhotoRejected:
		if req != nil && req.Image != nil {
			req.Image.Bytes = nil
		}
	case contract.OutcomePhotoMissing:
		// 能力豁免：ImageRequired=false 时不存在无图分支。
		return false
	case contract.OutcomeUnknown:
		// 截断的 JSON：结构坏了但不像 WAF，只能判 unknown。
		h.up.mu.Lock()
		h.up.signInBody = []byte(`{"code":0`)
		h.up.mu.Unlock()
	default:
		return false
	}
	return true
}

// clearDeviceCode 把设备态夹具里的 Code 置空（其余字段不动）。
func (h *yibanHarness) clearDeviceCode() {
	h.t.Helper()
	var obj map[string]any
	if err := json.Unmarshal(h.up.deviceFixture, &obj); err != nil {
		h.t.Fatalf("unmarshal device fixture: %v", err)
	}
	data, ok := obj["data"].(map[string]any)
	if !ok {
		h.t.Fatal("device fixture data is not an object")
	}
	data["Code"] = ""
	body, err := json.Marshal(obj)
	if err != nil {
		h.t.Fatalf("marshal device fixture: %v", err)
	}
	h.up.mu.Lock()
	h.up.deviceBody = body
	h.up.mu.Unlock()
}

// yibanSeedRequest 返回矩阵用的固定签到请求。
// 形状与符合性套件的 signRequest 一致，且在测试侧显式可见，
// 便于断言 invalid_target/photo_rejected 这类请求侧布种。
func yibanSeedRequest(attemptID string) contract.SignRequest {
	return contract.SignRequest{
		AttemptID: attemptID,
		Account: contract.AccountRef{
			ID:       "acct-conformance",
			SchoolID: "school-conformance",
			Platform: "conformance",
		},
		Credential: contract.Credential{
			Phone:      testPhone,
			Password:   testPassword,
			DeviceCode: "device-conformance",
			PhoneModel: "model-conformance",
		},
		Window: contract.Window{
			Start:  seedRequestTime,
			End:    seedRequestTime.Add(30 * time.Minute),
			Source: contract.WindowPlatform,
		},
		Target: contract.Target{
			ID:      "target-conformance",
			Title:   "判据目标",
			Center:  contract.Point{Lng: 121.4705, Lat: 31.2305},
			RadiusM: 100,
		},
		Location: contract.Location{Mode: contract.LocationFixed, Lng: 121.4705, Lat: 31.2305},
		Image: &contract.ImageRef{
			ID:     "image-conformance",
			Mime:   "image/jpeg",
			Width:  640,
			Height: 480,
			SHA256: "0000000000000000000000000000000000000000000000000000000000000000",
			Bytes:  []byte{0xff, 0xd8, 0xff},
		},
	}
}

// TestYibanSeedOutcomeMatrix 证明 yiban 真的能复现 14 项契约结果，
// 且 photo_missing 是**显性**跳过（能力豁免），不是整条判据静默消失。
func TestYibanSeedOutcomeMatrix(t *testing.T) {
	hits, exempt := 0, 0
	for _, want := range contract.AllOutcomes() {
		want := want
		t.Run(string(want), func(t *testing.T) {
			harness := newYibanHarness(t)
			req := yibanSeedRequest("attempt-seed-" + string(want))
			supported := harness.SeedOutcome(&req, want)
			if !supported {
				if want != contract.OutcomePhotoMissing {
					t.Fatalf("结果 %q 布种返回 false，但不属于能力豁免", want)
				}
				if harness.Capabilities().ImageRequired {
					t.Fatalf("结果 %q 布种返回 false，但声明了 ImageRequired", want)
				}
				exempt++
				return
			}
			result, err := harness.Sign(context.Background(), req)
			if err != nil {
				t.Fatalf("结果 %q 布种后 Sign 返回错误: %v", want, err)
			}
			if result.Outcome != want {
				t.Fatalf("结果 %q 布种得到 %q", want, result.Outcome)
			}
			hits++
		})
	}
	if hits != 14 {
		t.Fatalf("可复现结果 %d 项，want 14", hits)
	}
	if exempt != 1 {
		t.Fatalf("显性豁免 %d 项，want 1（photo_missing）", exempt)
	}

	// photo_missing 必须由 checkPhotoMissing 显性跳过，而不是被测试过滤掉。
	check := findConformanceCheck(t, conformance.CheckPhotoMissing)
	err := check.Run(func() contract.Provider { return newYibanHarness(t) })
	if !conformance.NotApplicable(err) {
		t.Fatalf("sign.photo_missing = %v, want 显性 notApplicable", err)
	}
}

// findConformanceCheck 按名称取判据。
func findConformanceCheck(t *testing.T, name string) conformance.Check {
	t.Helper()
	for _, check := range conformance.Checks() {
		if check.Name == name {
			return check
		}
	}
	t.Fatalf("判据 %s 不存在", name)
	return conformance.Check{}
}

// TestProviderConformanceApplicable 跑真实 Provider 的全部判据。
//
// 计数是验收的一部分：19 条通过 + 1 条**显性**跳过（sign.photo_missing，
// 因 Caps.ImageRequired=false）。跳过必须能数出来，不允许整条判据消失。
func TestProviderConformanceApplicable(t *testing.T) {
	checks := conformance.Checks()
	if len(checks) != 20 {
		t.Fatalf("判据 %d 条, want 20", len(checks))
	}
	passed, skipped := 0, 0
	for _, check := range checks {
		check := check
		t.Run(check.Name, func(t *testing.T) {
			err := check.Run(func() contract.Provider { return newYibanHarness(t) })
			switch {
			case err == nil:
				passed++
			case conformance.NotApplicable(err):
				skipped++
				fmt.Printf("conformance: %s: 显性跳过：%v\n", check.Name, err)
			default:
				t.Fatalf("%v", err)
			}
		})
	}
	if skipped != 1 {
		t.Fatalf("显性跳过 %d 条, want 1（sign.photo_missing）", skipped)
	}
	if passed != len(checks)-skipped {
		t.Fatalf("通过 %d 条, want %d", passed, len(checks)-skipped)
	}
}
