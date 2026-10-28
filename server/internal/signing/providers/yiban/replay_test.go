package yiban

// 本文件是录播链判据。
//
// # 功能
// 假上游按 testdata/recordings.json 登记的链回放协议夹具。
// 判据断言三件事：
//  1. 已签档（State=3）：DiscoverWindow 返回 already_done，且不发 signIn。
//  2. 成功档：Sign 返回 success，请求链为登录 4 跳＋signPosition＋getState＋signIn。
//  3. 未实拍档位：登记表每条都带「未实拍，未知」标记，且不提供响应体。
//
// # 归属
// providers/yiban 的测试面。生产代码不读本文件与 recordings.json。
//
// # 复用
// loadRecordings、assertRecordedChain、rawQuery（均为包内函数）。
// upstream 的 recordedHops 与 readProtocolFixture（login_test.go）。
//
// # 口径
// 夹具形状取自 2026-10-28 实拍（bd yiban-auto-sign-4sk 注释）。
// 夹具值是合成值，不是抓包原文。
// 响应体只取 protocol/testdata 的 captured 夹具；readProtocolFixture 运行时守卫。
// 未实拍档位不提供响应体，只在 recordings.json 的 unrecorded 段登记。

import (
	"context"
	"encoding/json"
	"errors"
	"net/url"
	"os"
	"reflect"
	"strings"
	"testing"

	"github.com/frostpolaris1/yiban-auto-sign/server/internal/signing/contract"
	"github.com/frostpolaris1/yiban-auto-sign/server/internal/signing/protocol"
)

// unrecordedMarker 标住未实拍档位。登记值必须以此为前缀。
const unrecordedMarker = "未实拍，未知"

// unrecordedKeys 是必须显式登记的未实拍档位。
var unrecordedKeys = []string{
	"signPosition.state=2",
	"signPosition.state=4",
	"signPosition.state=5",
	"signIn.code=999",
	"challenge.page",
}

// recordingScenario 是一条录播链。
// Hops 是期望的请求序列，格式为「METHOD /path」。
// Responses 按端点名指向 protocol/testdata 的 captured 夹具。
type recordingScenario struct {
	Note      string            `json:"note"`
	Driver    string            `json:"driver"`
	Hops      []string          `json:"hops"`
	Responses map[string]string `json:"responses"`
}

// recordingSet 是 testdata/recordings.json 的解析结果。
type recordingSet struct {
	Version    int                          `json:"version"`
	Note       string                       `json:"note"`
	Scenarios  map[string]recordingScenario `json:"scenarios"`
	Unrecorded map[string]string            `json:"unrecorded"`
}

// loadRecordings 读取链登记表。空表判红：本判据不得靠空登记通过。
func loadRecordings(t *testing.T) recordingSet {
	t.Helper()
	raw, err := os.ReadFile("testdata/recordings.json")
	if err != nil {
		t.Fatalf("read recordings: %v", err)
	}
	var set recordingSet
	if err := json.Unmarshal(raw, &set); err != nil {
		t.Fatalf("parse recordings: %v", err)
	}
	if len(set.Scenarios) == 0 {
		t.Fatal("recordings has no scenarios: refused to pass")
	}
	if len(set.Unrecorded) == 0 {
		t.Fatal("recordings has no unrecorded entries: refused to pass")
	}
	return set
}

// TestRecordedAlreadyDoneChain 回放已签档全链。
// 2026-10-28 实拍证实 State=3 表示已签到（bd yiban-auto-sign-4sk 注释）。
// 本栈 DiscoverWindow 只发登录 4 跳＋signPosition，不读设备态。
func TestRecordedAlreadyDoneChain(t *testing.T) {
	set := loadRecordings(t)
	scenario, ok := set.Scenarios["already_done"]
	if !ok {
		t.Fatal("recordings lacks scenario already_done")
	}

	up := newUpstream(t)
	up.signPositionBody = readProtocolFixture(t, scenario.Responses["signPosition"])
	assertDoneFixtureShape(t, up.signPositionBody)

	p := NewProvider(New(Config{Endpoints: up.endpoints()}))
	_, err := p.DiscoverWindow(context.Background(), contract.DiscoverRequest{
		Account:    providerAccount(),
		Credential: providerCredential(),
	})
	var outcomeErr *contract.OutcomeError
	if !errors.As(err, &outcomeErr) || outcomeErr.Outcome != contract.OutcomeAlreadyDone {
		t.Fatalf("DiscoverWindow error = %v, want OutcomeError already_done", err)
	}

	assertRecordedChain(t, up.recordedHops(), scenario.Hops)
	if calls := up.count("/nightAttendance/student/index/signIn"); calls != 0 {
		t.Fatalf("signIn calls = %d, want 0", calls)
	}
}

// assertDoneFixtureShape 钉住已签档夹具的字段形状。
// 防止夹具被改成可签态后判据仍然绿。
func assertDoneFixtureShape(t *testing.T, body []byte) {
	t.Helper()
	envelope, err := protocol.ParseAPIEnvelope(body)
	if err != nil {
		t.Fatalf("parse already-done fixture: %v", err)
	}
	cfg, err := protocol.ParseSignPosition(envelope.Data)
	if err != nil {
		t.Fatalf("parse signPosition data: %v", err)
	}
	if cfg.State == nil {
		t.Fatal("fixture State is missing")
	}
	if *cfg.State != 3 {
		t.Fatalf("fixture State = %d, want 3", *cfg.State)
	}
	if cfg.Msg != "已签到" {
		t.Fatalf("fixture Msg = %q, want 已签到", cfg.Msg)
	}
	if len(cfg.Positions) != 0 {
		t.Fatalf("fixture Positions = %d, want 0", len(cfg.Positions))
	}
	if cfg.TimeWindow == nil {
		t.Fatal("fixture Range is missing")
	}
	if cfg.IsNeedPhoto == nil || *cfg.IsNeedPhoto != 2 {
		t.Fatalf("fixture IsNeedPhoto = %v, want 2", cfg.IsNeedPhoto)
	}
}

// TestRecordedSignOKChain 回放签到成功全链。
// 链形状：登录 4 跳、signPosition、getState、signIn，共 7 跳。
func TestRecordedSignOKChain(t *testing.T) {
	set := loadRecordings(t)
	scenario, ok := set.Scenarios["sign_ok"]
	if !ok {
		t.Fatal("recordings lacks scenario sign_ok")
	}

	up := newUpstream(t)
	up.signPositionBody = readProtocolFixture(t, scenario.Responses["signPosition"])
	up.deviceBody = readProtocolFixture(t, scenario.Responses["deviceState"])
	up.signInBody = readProtocolFixture(t, scenario.Responses["signIn"])

	p := NewProvider(New(Config{Endpoints: up.endpoints()}))
	req := providerSignRequest("attempt-recording-sign-ok")
	// 设备码留空：本链必须经过 getState 取设备码。
	req.Credential.DeviceCode = ""
	req.Credential.PhoneModel = ""
	req.Location = contract.Location{Mode: contract.LocationAuto}

	result, err := p.Sign(context.Background(), req)
	if err != nil {
		t.Fatalf("Sign: %v", err)
	}
	if result.Outcome != contract.OutcomeSuccess || result.Evidence.Receipt != "true" {
		t.Fatalf("result = %+v, want success receipt true", result)
	}

	hops := up.recordedHops()
	assertRecordedChain(t, hops, scenario.Hops)
	if len(hops) != 7 {
		t.Fatalf("hops = %d, want 7", len(hops))
	}
	assertSignChainShapes(t, up, hops)
}

// assertRecordedChain 比对实际请求序列与登记序列。
func assertRecordedChain(t *testing.T, got []recordedHop, want []string) {
	t.Helper()
	actual := make([]string, 0, len(got))
	for _, hop := range got {
		actual = append(actual, hop.Method+" "+hop.Path)
	}
	if !reflect.DeepEqual(actual, want) {
		t.Fatalf("request chain = %v, want %v", actual, want)
	}
}

// assertSignChainShapes 断言 7 跳的关键形状：头、体、query。
// 值全部来自合成夹具或测试常量。
func assertSignChainShapes(t *testing.T, up *upstream, hops []recordedHop) {
	t.Helper()

	up.mu.Lock()
	usersureForm := up.usersureForm
	usersureRaw := string(up.usersureRaw)
	signForm := up.signForm
	signRaw := string(up.signRaw)
	deviceBody := append([]byte(nil), up.deviceBody...)
	up.mu.Unlock()

	// 每跳带同一个 csrf_token；登录第一跳生成。
	csrf := hops[0].Cookie["csrf_token"]
	if csrf == "" {
		t.Fatal("login chain did not carry csrf_token")
	}
	for i, hop := range hops {
		if got := hop.Cookie["csrf_token"]; got != csrf {
			t.Fatalf("hop %d (%s) csrf cookie = %q, want %q", i+1, hop.Path, got, csrf)
		}
	}

	// 1. 授权页：客户端参数与登录头。
	code := rawQuery(t, hops[0])
	if code.Get("client_id") != oauthClientID || code.Get("redirect_uri") != redirectURI {
		t.Fatalf("code/html query = %q", hops[0].RawQuery)
	}
	if got := hops[0].Header.Get("Origin"); got != loginOrigin {
		t.Fatalf("code/html Origin = %q, want %q", got, loginOrigin)
	}
	if got := hops[0].Header.Get("AppVersion"); got != appVersion {
		t.Fatalf("code/html AppVersion = %q, want %q", got, appVersion)
	}

	// 2. usersure：ajax_sign、字段序、空 scope 收尾、无 Origin/Referer。
	if got := rawQuery(t, hops[1]).Get("ajax_sign"); got != testPageUse {
		t.Fatalf("usersure ajax_sign = %q, want %q", got, testPageUse)
	}
	if got := usersureForm.Get("oauth_uname"); got != testPhone {
		t.Fatalf("usersure oauth_uname = %q, want %q", got, testPhone)
	}
	if got := usersureForm.Get("client_id"); got != oauthClientID {
		t.Fatalf("usersure client_id = %q, want %q", got, oauthClientID)
	}
	if !usersureForm.Has("scope") || usersureForm.Get("scope") != "" {
		t.Fatalf("usersure scope = %v, want empty value", usersureForm)
	}
	if !strings.HasPrefix(usersureRaw, "oauth_uname=") || !strings.HasSuffix(usersureRaw, "&scope=") {
		t.Fatalf("usersure body shape = %q", usersureRaw)
	}
	if got := hops[1].Header.Get("Origin"); got != "" {
		t.Fatalf("usersure Origin = %q, want empty", got)
	}
	if got := hops[1].Header.Get("Referer"); got != "" {
		t.Fatalf("usersure Referer = %q, want empty", got)
	}

	// 3. iframe：act 参数。
	if got := rawQuery(t, hops[2]).Get("act"); got != iframeAct {
		t.Fatalf("iframe act = %q, want %q", got, iframeAct)
	}

	// 4. 认证：verify_request 在前、CSRF 在后；cookie 与 query 同值。
	if !strings.HasPrefix(hops[3].RawQuery, "verifyRequest="+testVerify+"&CSRF=") {
		t.Fatalf("auth query order = %q", hops[3].RawQuery)
	}
	if got := rawQuery(t, hops[3]).Get("CSRF"); got != csrf {
		t.Fatalf("auth CSRF = %q, want %q", got, csrf)
	}

	// 5-6. 应用链：App 域头，CSRF 在 query。
	for _, index := range []int{4, 5} {
		if got := hops[index].Header.Get("Origin"); got != appOrigin {
			t.Fatalf("hop %d (%s) Origin = %q, want %q", index+1, hops[index].Path, got, appOrigin)
		}
		if got := rawQuery(t, hops[index]).Get("CSRF"); got != csrf {
			t.Fatalf("hop %d (%s) CSRF = %q, want %q", index+1, hops[index].Path, got, csrf)
		}
	}

	// 7. 签到：表单五字段、设备码来自 getState、SignInfo 形状。
	if got := signForm.Get("OutState"); got != "1" {
		t.Fatalf("signIn OutState = %q, want 1", got)
	}
	if got := signForm.Get("AttachmentFileName"); got != "" {
		t.Fatalf("signIn AttachmentFileName = %q, want empty", got)
	}
	device := parseDeviceFixture(t, deviceBody)
	if device.Code == "" || device.PhoneModel == "" {
		t.Fatalf("device fixture = %+v, want code and model", device)
	}
	if got := signForm.Get("Code"); got != device.Code {
		t.Fatalf("signIn Code = %q, want %q", got, device.Code)
	}
	if got := signForm.Get("PhoneModel"); got != device.PhoneModel {
		t.Fatalf("signIn PhoneModel = %q, want %q", got, device.PhoneModel)
	}
	var info struct {
		Reason             string
		AttachmentFileName string
		LngLat             string
		Address            string
	}
	if err := json.Unmarshal([]byte(signForm.Get("SignInfo")), &info); err != nil {
		t.Fatalf("signIn SignInfo = %q: %v", signForm.Get("SignInfo"), err)
	}
	if info.Reason != "" || info.AttachmentFileName != "" {
		t.Fatalf("SignInfo optional fields = %+v, want empty", info)
	}
	if info.LngLat != "0.101837,1.003954" {
		t.Fatalf("SignInfo LngLat = %q", info.LngLat)
	}
	if info.Address != providerTarget().Address {
		t.Fatalf("SignInfo Address = %q, want %q", info.Address, providerTarget().Address)
	}
	if !strings.HasPrefix(signRaw, "AttachmentFileName=&OutState=1&Code=") {
		t.Fatalf("signIn body field order = %q", signRaw)
	}
	if !strings.Contains(signRaw, "&PhoneModel=") || !strings.Contains(signRaw, "&SignInfo=") {
		t.Fatalf("signIn body fields = %q", signRaw)
	}
}

// TestRecordedChainRegistry 钉住未实拍档位的登记。
// 没有响应体的档位必须在 unrecorded 段显式标未知（AGENTS §30：不许静默）。
func TestRecordedChainRegistry(t *testing.T) {
	set := loadRecordings(t)
	for _, key := range unrecordedKeys {
		note, ok := set.Unrecorded[key]
		if !ok {
			t.Fatalf("unrecorded[%q] is missing: 未实拍档位必须显式登记", key)
		}
		if !strings.HasPrefix(note, unrecordedMarker) {
			t.Fatalf("unrecorded[%q] = %q, want prefix %q", key, note, unrecordedMarker)
		}
	}
	for name, scenario := range set.Scenarios {
		if scenario.Driver != "DiscoverWindow" && scenario.Driver != "Sign" {
			t.Fatalf("scenario %q driver = %q", name, scenario.Driver)
		}
		if len(scenario.Hops) == 0 {
			t.Fatalf("scenario %q has no hops", name)
		}
		for _, fixture := range scenario.Responses {
			// captured 守卫：非 captured 夹具在这里直接判红。
			readProtocolFixture(t, fixture)
		}
	}
}

// rawQuery 解析一次请求的原始 query。
func rawQuery(t *testing.T, hop recordedHop) url.Values {
	t.Helper()
	values, err := url.ParseQuery(hop.RawQuery)
	if err != nil {
		t.Fatalf("parse query %q: %v", hop.RawQuery, err)
	}
	return values
}

// parseDeviceFixture 解析 getState 夹具的 data 载荷。
func parseDeviceFixture(t *testing.T, body []byte) protocol.DeviceState {
	t.Helper()
	envelope, err := protocol.ParseAPIEnvelope(body)
	if err != nil {
		t.Fatalf("parse device fixture: %v", err)
	}
	state, err := protocol.ParseDeviceState(envelope.Data)
	if err != nil {
		t.Fatalf("parse device data: %v", err)
	}
	return state
}
