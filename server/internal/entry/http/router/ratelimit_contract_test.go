package router

// 限流面与契约的对账门：
// 每个按 IP 限流的端点必须在 openapi 里声明 429，且指向具名响应。
// 本文件**不**启动 HTTP 服务、**不**读数据库。

import (
	"os"
	"strings"
	"testing"
)

// TestRateLimitedRoutesDeclare429 钉住「实现限流的面 == 契约声明的面」。
// 少一条声明，客户端会把 429 当内部错误；扫空不得当绿。
func TestRateLimitedRoutesDeclare429(t *testing.T) {
	raw, err := os.ReadFile(contractSpecPath)
	if err != nil {
		t.Fatalf("read contract: %v", err)
	}
	doc := string(raw)
	if len(strings.TrimSpace(doc)) == 0 {
		t.Fatal("contract is empty; the gate DID NOT RUN")
	}
	if len(rateLimitedPaths) < 6 {
		t.Fatalf("限流端点 = %d, want at least 6", len(rateLimitedPaths))
	}
	for route := range rateLimitedPaths {
		path := strings.ReplaceAll(route, ":id", "{id}")
		block, ok := pathBlock(doc, path)
		if !ok {
			t.Errorf("契约缺路径 %s（实现已限流）", path)
			continue
		}
		if !strings.Contains(block, "'429':") {
			t.Errorf("%s 已限流但未声明 429", route)
			continue
		}
		if !strings.Contains(block, "TooManyRequestsError") {
			t.Errorf("%s 的 429 未指向 TooManyRequestsError", route)
		}
	}
}

// pathBlock 返回一个路径段：从 `  /path:` 到下一个路径段之前。
func pathBlock(doc, path string) (string, bool) {
	lines := strings.Split(doc, "\n")
	start := -1
	for i, line := range lines {
		if line == "  "+path+":" {
			start = i
			break
		}
	}
	if start < 0 {
		return "", false
	}
	end := len(lines)
	for i := start + 1; i < len(lines); i++ {
		if strings.HasPrefix(lines[i], "  /") || lines[i] == "components:" {
			end = i
			break
		}
	}
	return strings.Join(lines[start:end], "\n"), true
}
