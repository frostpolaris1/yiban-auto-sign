package yiban

import (
	"context"
	"errors"
	"reflect"
	"strings"
	"testing"
	"time"

	"github.com/frostpolaris1/yiban-auto-sign/server/internal/signing/contract"
)

func newTestProvider(t *testing.T) (*Provider, *upstream) {
	t.Helper()
	up := newUpstream(t)
	return NewProvider(New(Config{Endpoints: up.endpoints()})), up
}

func providerAccount() contract.AccountRef {
	return contract.AccountRef{ID: "acct-1", SchoolID: "school-1", Platform: "yiban"}
}

func providerCredential() contract.Credential {
	return contract.Credential{
		Phone:      testPhone,
		Password:   testPassword,
		DeviceCode: "device-code",
		PhoneModel: "Model-X",
	}
}

func providerTarget() contract.Target {
	return contract.Target{
		ID:      "27464ed31464763a6827816a0ac9c8ab",
		Title:   "示例校区",
		Address: "示例省示例市示例区示例大学示例校区-示例苑·公共教学组团",
		Center:  contract.Point{Lng: 0.101837, Lat: 1.003954},
	}
}

func providerSignRequest(attempt string) contract.SignRequest {
	return contract.SignRequest{
		AttemptID:  attempt,
		Account:    providerAccount(),
		Credential: providerCredential(),
		Target:     providerTarget(),
		Location:   contract.Location{Mode: contract.LocationFixed, Lng: 118.1, Lat: 32.0},
	}
}

func TestProviderIdentity(t *testing.T) {
	p, _ := newTestProvider(t)
	if p.ID() != "yiban" {
		t.Fatalf("ID = %q", p.ID())
	}
	caps := p.Capabilities()
	if !caps.WindowDiscovery || !caps.TargetDiscovery || !caps.DeviceBinding || !caps.FixedLocation {
		t.Fatalf("capabilities = %+v", caps)
	}
	if caps.ImageRequired || caps.ManualCaptcha || caps.Rank {
		t.Fatalf("unexpected capabilities = %+v", caps)
	}
}

func TestProviderDiscoverWindow(t *testing.T) {
	p, up := newTestProvider(t)
	req := contract.DiscoverRequest{Account: providerAccount(), Credential: providerCredential()}

	window, err := p.DiscoverWindow(context.Background(), req)
	if err != nil {
		t.Fatalf("DiscoverWindow: %v", err)
	}
	if !window.Start.Equal(time.Unix(1790980200, 0)) || !window.End.Equal(time.Unix(1790985000, 0)) {
		t.Fatalf("window = %+v", window)
	}
	if window.Source != contract.WindowPlatform {
		t.Fatalf("source = %q", window.Source)
	}

	up.setSignPositionState(t, 3)
	p.client.clearPositions("acct-1")
	_, err = p.DiscoverWindow(context.Background(), req)
	var outcomeErr *contract.OutcomeError
	if !errors.As(err, &outcomeErr) || outcomeErr.Outcome != contract.OutcomeAlreadyDone {
		t.Fatalf("state 3: error = %v, want AlreadyDone", err)
	}

	up.setSignPositionState(t, 5)
	p.client.clearPositions("acct-1")
	_, err = p.DiscoverWindow(context.Background(), req)
	if !errors.As(err, &outcomeErr) || outcomeErr.Outcome != contract.OutcomePendingUpstream {
		t.Fatalf("state 5: error = %v, want PendingUpstream", err)
	}

	up.removeSignPositionRange(t)
	p.client.clearPositions("acct-1")
	_, err = p.DiscoverWindow(context.Background(), req)
	if !errors.As(err, &outcomeErr) || outcomeErr.Outcome != contract.OutcomeNoTarget {
		t.Fatalf("missing range: error = %v, want NoTarget", err)
	}
}

func TestProviderDiscoverTargets(t *testing.T) {
	p, _ := newTestProvider(t)
	targets, err := p.DiscoverTargets(context.Background(),
		contract.DiscoverRequest{Account: providerAccount(), Credential: providerCredential()})
	if err != nil {
		t.Fatalf("DiscoverTargets: %v", err)
	}
	if len(targets) != 2 {
		t.Fatalf("targets = %d, want 2", len(targets))
	}
	first := targets[0]
	if first.ID != "27464ed31464763a6827816a0ac9c8ab" || first.RadiusM != 70 {
		t.Fatalf("first target = %+v", first)
	}
	if first.Center.Lng != 0.101837 || first.Center.Lat != 1.003954 {
		t.Fatalf("center = %+v", first.Center)
	}
	if len(first.Polygon) != 13 {
		t.Fatalf("polygon = %d", len(first.Polygon))
	}
	if first.RequiresPhoto {
		t.Fatal("IsNeedPhoto semantics are unresolved; RequiresPhoto must stay false")
	}
	if first.Raw["is_need_photo"] != 2 {
		t.Fatalf("raw = %#v", first.Raw)
	}
}

func TestProviderSignSuccess(t *testing.T) {
	p, up := newTestProvider(t)
	result, err := p.Sign(context.Background(), providerSignRequest("attempt-1"))
	if err != nil {
		t.Fatalf("Sign: %v", err)
	}
	if result.Outcome != contract.OutcomeSuccess || result.Evidence.Receipt != "true" {
		t.Fatalf("result = %+v", result)
	}

	up.mu.Lock()
	form, query, cookie := up.signForm, up.signQuery, up.signCookie
	headers := up.headers["/nightAttendance/student/index/signIn"]
	up.mu.Unlock()

	if form.Get("Code") != "device-code" || form.Get("PhoneModel") != "Model-X" {
		t.Fatalf("device fields = %v", form)
	}
	if form.Get("OutState") != "1" || form.Get("AttachmentFileName") != "" {
		t.Fatalf("sign form = %v", form)
	}
	if got := headers.Get("Origin"); got != appOrigin {
		t.Fatalf("Origin = %q, want %q", got, appOrigin)
	}
	if query.Get("CSRF") == "" || cookie != query.Get("CSRF") {
		t.Fatalf("csrf query=%q cookie=%q", query.Get("CSRF"), cookie)
	}
}

func TestProviderSignUsesTargetCenter(t *testing.T) {
	p, up := newTestProvider(t)
	req := providerSignRequest("attempt-center")
	req.Location = contract.Location{Mode: contract.LocationAuto}
	if _, err := p.Sign(context.Background(), req); err != nil {
		t.Fatalf("Sign: %v", err)
	}
	up.mu.Lock()
	form := up.signForm
	up.mu.Unlock()
	if !strings.Contains(form.Get("SignInfo"), "0.101837,1.003954") {
		t.Fatalf("SignInfo does not use the target center: %q", form.Get("SignInfo"))
	}
}

func TestProviderSignRequiresLocation(t *testing.T) {
	p, up := newTestProvider(t)
	req := providerSignRequest("attempt-nolocation")
	req.Location = contract.Location{}
	req.Target.Center = contract.Point{}
	result, err := p.Sign(context.Background(), req)
	if err != nil {
		t.Fatalf("Sign: %v", err)
	}
	if result.Outcome != contract.OutcomeInvalidTarget {
		t.Fatalf("outcome = %q", result.Outcome)
	}
	if got := up.count("/nightAttendance/student/index/signIn"); got != 0 {
		t.Fatalf("signIn calls = %d, want 0", got)
	}
}

func TestProviderSignReadsDevice(t *testing.T) {
	p, up := newTestProvider(t)
	req := providerSignRequest("attempt-device")
	req.Credential.DeviceCode = ""
	req.Credential.PhoneModel = ""
	result, err := p.Sign(context.Background(), req)
	if err != nil {
		t.Fatalf("Sign: %v", err)
	}
	if result.Outcome != contract.OutcomeSuccess {
		t.Fatalf("outcome = %q", result.Outcome)
	}
	if result.Message == "" {
		t.Fatal("unexpected empty warning for an unauthorized device")
	}
	up.mu.Lock()
	form := up.signForm
	up.mu.Unlock()
	if form.Get("Code") == "" {
		t.Fatal("the registered device code was not submitted")
	}
}

func TestProviderSignDeviceMissing(t *testing.T) {
	p, up := newTestProvider(t)
	up.deviceBody = []byte(`{"code":0,"data":{}}`)
	req := providerSignRequest("attempt-nodevice")
	req.Credential.DeviceCode = ""
	result, err := p.Sign(context.Background(), req)
	if err != nil {
		t.Fatalf("Sign: %v", err)
	}
	if result.Outcome != contract.OutcomeDeviceUnbound {
		t.Fatalf("outcome = %q", result.Outcome)
	}
	if got := up.count("/nightAttendance/student/index/signIn"); got != 0 {
		t.Fatalf("signIn calls = %d, want 0", got)
	}
}

func TestProviderSignAlreadyDone(t *testing.T) {
	p, up := newTestProvider(t)
	up.setSignPositionState(t, 3)
	p.client.clearPositions("acct-1")
	result, err := p.Sign(context.Background(), providerSignRequest("attempt-done"))
	if err != nil {
		t.Fatalf("Sign: %v", err)
	}
	if result.Outcome != contract.OutcomeAlreadyDone {
		t.Fatalf("outcome = %q", result.Outcome)
	}
	if got := up.count("/nightAttendance/student/index/signIn"); got != 0 {
		t.Fatalf("signIn calls = %d, want 0", got)
	}
}

func TestProviderSignPending(t *testing.T) {
	p, up := newTestProvider(t)
	up.setSignPositionState(t, 5)
	p.client.clearPositions("acct-1")
	result, err := p.Sign(context.Background(), providerSignRequest("attempt-pending"))
	if err != nil {
		t.Fatalf("Sign: %v", err)
	}
	if result.Outcome != contract.OutcomePendingUpstream {
		t.Fatalf("outcome = %q", result.Outcome)
	}
}

func TestProviderSignIdempotent(t *testing.T) {
	p, up := newTestProvider(t)
	req := providerSignRequest("attempt-same")
	first, err := p.Sign(context.Background(), req)
	if err != nil {
		t.Fatalf("first Sign: %v", err)
	}
	second, err := p.Sign(context.Background(), req)
	if err != nil {
		t.Fatalf("second Sign: %v", err)
	}
	if !reflect.DeepEqual(first, second) {
		t.Fatalf("results differ:\n%+v\n%+v", first, second)
	}
	if got := up.count("/nightAttendance/student/index/signIn"); got != 1 {
		t.Fatalf("signIn calls = %d, want 1", got)
	}
}

func TestProviderSignNoReceiptResult(t *testing.T) {
	up := newUpstream(t)
	up.signInBody = []byte(`{"code":0,"data":false}`)
	p := NewProvider(New(Config{Endpoints: up.endpoints()}))
	result, err := p.Sign(context.Background(), providerSignRequest("attempt-noreceipt"))
	if err != nil {
		t.Fatalf("Sign: %v", err)
	}
	if result.Outcome != contract.OutcomeUnknown {
		t.Fatalf("outcome = %q", result.Outcome)
	}
	if result.Retry.Retryable {
		t.Fatal("unknown must not be retryable")
	}
}

func TestProviderSignDeviceRejected(t *testing.T) {
	up := newUpstream(t)
	up.signInBody = []byte(`{"code":-1,"msg":"请使用授权设备进行签到"}`)
	p := NewProvider(New(Config{Endpoints: up.endpoints()}))
	result, err := p.Sign(context.Background(), providerSignRequest("attempt-rejected"))
	if err != nil {
		t.Fatalf("Sign: %v", err)
	}
	if result.Outcome != contract.OutcomeDeviceUnbound {
		t.Fatalf("outcome = %q", result.Outcome)
	}
}

func TestProviderSignSessionExpired(t *testing.T) {
	up := newUpstream(t)
	up.signInBody = []byte(`{"code":999,"msg":"expired"}`)
	p := NewProvider(New(Config{Endpoints: up.endpoints()}))
	result, err := p.Sign(context.Background(), providerSignRequest("attempt-expired"))
	if err != nil {
		t.Fatalf("Sign: %v", err)
	}
	if result.Outcome != contract.OutcomeTransient ||
		!result.Retry.Retryable || !result.Retry.ClearSession {
		t.Fatalf("result = %+v", result)
	}
	if got := up.count("/nightAttendance/student/index/signIn"); got != 1 {
		t.Fatalf("signIn calls = %d, want 1 (no duplicate POST)", got)
	}
}

func TestProviderSignChallenge(t *testing.T) {
	up := newUpstream(t)
	up.signInBody = []byte("<html><body>ydclearance=abc</body></html>")
	p := NewProvider(New(Config{Endpoints: up.endpoints()}))
	result, err := p.Sign(context.Background(), providerSignRequest("attempt-challenge"))
	if err != nil {
		t.Fatalf("Sign: %v", err)
	}
	if result.Outcome != contract.OutcomeChallenge || result.Retry.Retryable {
		t.Fatalf("result = %+v", result)
	}
}

func TestProviderSignMalformed(t *testing.T) {
	up := newUpstream(t)
	up.signInBody = []byte(`{"code":0`)
	p := NewProvider(New(Config{Endpoints: up.endpoints()}))
	result, err := p.Sign(context.Background(), providerSignRequest("attempt-malformed"))
	if err != nil {
		t.Fatalf("Sign: %v", err)
	}
	if result.Outcome != contract.OutcomeUnknown || result.Retry.Retryable {
		t.Fatalf("result = %+v", result)
	}
}

func TestProviderSignServerError(t *testing.T) {
	up := newUpstream(t)
	up.signInStatus = 500
	p := NewProvider(New(Config{Endpoints: up.endpoints()}))
	result, err := p.Sign(context.Background(), providerSignRequest("attempt-500"))
	if err != nil {
		t.Fatalf("Sign: %v", err)
	}
	if result.Outcome != contract.OutcomeTransient || !result.Retry.Retryable {
		t.Fatalf("result = %+v", result)
	}
}
