package assembly

// 交互面 gated provider 的装配接线守卫（批 abs R1 / F2，规则 7）。
//
// 只测 gatedProvider 本体不够：把四处消费点改回裸 provider，
// 单元测试仍全绿，急停/出口闸会静默失守。本文件读 assembly.go 源，
// 钉住四个消费点持 gated、worker 仍收裸 provider（避免双重排队）。
// 这是静态接线门；端到端另由 cmd/server 的 paused×detect 用例咬住。

import (
	"os"
	"regexp"
	"testing"
)

func TestAssemblyWiresGatedProvider(t *testing.T) {
	raw, err := os.ReadFile("assembly.go")
	if err != nil {
		t.Fatalf("read assembly.go: %v", err)
	}
	src := string(raw)

	wants := []struct {
		name    string
		pattern string
	}{
		{"gate 构造", `gated\s*:=\s*gatedProvider\{inner:\s*provider,\s*gate:\s*platformGate\}`},
		{"账号认证", `Identity:\s+gated,`},
		{"设备读回", `Device:\s+accountDeviceReader\{provider:\s+gated\}`},
		{"凭据校验/姓名", `signinghttp\.NewHandler\(accountSvc,\s*adminSvc,\s*gated\)`},
		{"窗口发现", `Discover:\s+scheduleDiscoverer\{provider:\s+gated,`},
		{"worker 裸 provider", `signer:\s+provider,`},
	}
	for _, want := range wants {
		if !regexp.MustCompile(want.pattern).MatchString(src) {
			t.Errorf("装配接线缺失：%s（pattern %q）", want.name, want.pattern)
		}
	}

	// 负向：四个交互消费点不得再出现裸 provider。
	bads := []struct {
		name    string
		pattern string
	}{
		{"账号认证裸 provider", `Identity:\s+provider,`},
		{"设备读回裸 provider", `Device:\s+accountDeviceReader\{provider:\s+provider\}`},
		{"凭据校验裸 provider", `signinghttp\.NewHandler\(accountSvc,\s*adminSvc,\s*provider\)`},
		{"窗口发现裸 provider", `Discover:\s+scheduleDiscoverer\{provider:\s+provider,`},
	}
	for _, bad := range bads {
		if regexp.MustCompile(bad.pattern).MatchString(src) {
			t.Errorf("交互面仍直连裸 provider：%s（pattern %q）", bad.name, bad.pattern)
		}
	}
}
