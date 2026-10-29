package router

// 契约头参数登记门。
//
// # 功能
// 解析 contracts/openapi.yaml，取出每个 in: header 参数名。
// 每个名字必须二选一：登记在 listRequestHeaders，或在此登记为单值头并给理由。
// 漏登记即红：列表头漏登记时，多行请求被生成绑定判成 400。
// 本文件**不**启动 HTTP 服务、**不**连数据库。
//
// # 归属
// entry/http/router 是合并中间件的所有者，故门也落在这里。
// 落在 oapi 包会与生成物混住，且测试要读未导出的 listRequestHeaders。
//
// # 口径
// 解析按行：name 与 in 必须是同一映射的兄弟键。
// 每处 in: header 必须配到一个名字；配不上即门与契约形状不符。
// 扫空不得当绿。

import (
	"os"
	"regexp"
	"sort"
	"strings"
	"testing"
)

const contractSpecPath = "../../../../contracts/openapi.yaml"

// singleValueRequestHeaders 是显式单值登记：参数名 → 理由。
// 进入本表的头多行即畸形请求，保持生成绑定的 400。
var singleValueRequestHeaders = map[string]string{
	"X-CSRF-Token":  "单值令牌；多行即畸形请求，保持 400",
	"Last-Event-ID": "单值续传标识；多行即畸形请求，保持 400",
}

var (
	// contractParamNameLine 匹配 name 键：可带列表连字符，值必须是单个记号。
	// 带花括号的属性声明（name: { type: string }）不匹配。
	contractParamNameLine = regexp.MustCompile(`^(?:-\s+)?name:\s*(\S+)\s*$`)
	// contractParamInLine 匹配 in 键：值必须是单个记号。
	contractParamInLine = regexp.MustCompile(`^in:\s*(\S+)\s*$`)
)

// contractHeaderParams 返回契约里所有 in: header 的参数名，去重且排序。
//
// name 与 in 的兄弟关系用缩进判定：
// 组件写法两行同缩进；操作内联写法是 `- name:`，兄弟键缩进多 2。
// 缩进小于该映射即离开映射，清空待定名。
func contractHeaderParams(t *testing.T, doc string) []string {
	t.Helper()
	var names []string
	seen := map[string]bool{}
	headerLines := 0
	pending, mapIndent := "", -1
	for i, raw := range strings.Split(doc, "\n") {
		line := strings.TrimRight(raw, " \t\r")
		trimmed := strings.TrimSpace(line)
		if trimmed == "" || strings.HasPrefix(trimmed, "#") {
			continue
		}
		indent := len(line) - len(strings.TrimLeft(line, " "))
		if m := contractParamNameLine.FindStringSubmatch(trimmed); m != nil {
			pending, mapIndent = strings.Trim(m[1], `"'`), indent
			if strings.HasPrefix(trimmed, "-") {
				mapIndent = indent + 2
			}
			continue
		}
		if m := contractParamInLine.FindStringSubmatch(trimmed); m != nil {
			if indent < mapIndent {
				pending = ""
				continue
			}
			if m[1] != "header" {
				continue
			}
			headerLines++
			if indent != mapIndent || pending == "" {
				t.Fatalf("%s:%d 的 in: header 与 name 不成兄弟键；门与契约形状不符",
					contractSpecPath, i+1)
			}
			if !seen[pending] {
				seen[pending] = true
				names = append(names, pending)
			}
			continue
		}
		if pending != "" && indent < mapIndent {
			pending = ""
		}
	}
	if headerLines == 0 {
		t.Fatal("契约里没有 in: header 参数；门没有扫到东西")
	}
	sort.Strings(names)
	return names
}

func contractHeaderListed(name string) bool {
	for _, listed := range listRequestHeaders {
		if listed == name {
			return true
		}
	}
	return false
}

// 契约是头的唯一来源。新增一个头却不登记，多行请求会静默变 400。
func TestContractHeaderParamsRegistered(t *testing.T) {
	raw, err := os.ReadFile(contractSpecPath)
	if err != nil {
		t.Fatalf("read %s: %v", contractSpecPath, err)
	}
	if len(strings.TrimSpace(string(raw))) == 0 {
		t.Fatalf("%s is empty; the gate DID NOT RUN", contractSpecPath)
	}
	names := contractHeaderParams(t, string(raw))
	if len(names) < 3 {
		t.Fatalf("契约头参数 = %d, want at least 3; 门没有扫到东西", len(names))
	}

	for _, name := range names {
		listed := contractHeaderListed(name)
		reason, single := singleValueRequestHeaders[name]
		switch {
		case listed && single:
			t.Errorf("%s 同时登记为列表头与单值头；两者语义冲突", name)
		case listed:
		case single && strings.TrimSpace(reason) != "":
		case single:
			t.Errorf("%s 的单值登记没有理由", name)
		default:
			t.Errorf("%s 未登记：登记到 listRequestHeaders，或登记为单值头并给理由", name)
		}
	}

	for name := range singleValueRequestHeaders {
		found := false
		for _, present := range names {
			if present == name {
				found = true
				break
			}
		}
		if !found {
			t.Errorf("单值登记过期：%s 不再是契约里的头参数", name)
		}
	}

	if len(listRequestHeaders) == 0 {
		t.Fatal("listRequestHeaders 为空；合并中间件没有可处理的头")
	}
}
