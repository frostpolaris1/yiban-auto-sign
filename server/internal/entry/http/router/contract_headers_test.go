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
// 解析走 YAML 结构，不按行：流式写法、行尾注释与键序都不影响结果。
// 参数可内联，也可 $ref 到 components.parameters。
// 扫空不得当绿；指向未知组件的 $ref 直接失败。

import (
	"os"
	"sort"
	"strings"
	"testing"

	"go.yaml.in/yaml/v3"
)

const contractSpecPath = "../../../../contracts/openapi.yaml"

// contractParameterRefPrefix 是门唯一解析的 $ref 前缀。
const contractParameterRefPrefix = "#/components/parameters/"

// singleValueRequestHeaders 是显式单值登记：参数名 → 理由。
// 进入本表的头多行即畸形请求，保持生成绑定的 400。
var singleValueRequestHeaders = map[string]string{
	"X-CSRF-Token":  "单值令牌；多行即畸形请求，保持 400",
	"Last-Event-ID": "单值续传标识；多行即畸形请求，保持 400",
}

// contractParameter 是一个参数对象；只取门用到的三个键。
type contractParameter struct {
	Name string `yaml:"name"`
	In   string `yaml:"in"`
	Ref  string `yaml:"$ref"`
}

// contractOperation 是一个路径操作；只取参数列表。
type contractOperation struct {
	Parameters []contractParameter `yaml:"parameters"`
}

// contractPathItem 是一个路径项。方法逐个列出，其余键忽略。
type contractPathItem struct {
	Parameters []contractParameter `yaml:"parameters"`
	Get        *contractOperation  `yaml:"get"`
	Put        *contractOperation  `yaml:"put"`
	Post       *contractOperation  `yaml:"post"`
	Delete     *contractOperation  `yaml:"delete"`
	Patch      *contractOperation  `yaml:"patch"`
	Head       *contractOperation  `yaml:"head"`
	Options    *contractOperation  `yaml:"options"`
}

// operations 返回路径项下所有已声明的方法。
func (p contractPathItem) operations() []*contractOperation {
	all := []*contractOperation{p.Get, p.Put, p.Post, p.Delete, p.Patch, p.Head, p.Options}
	out := make([]*contractOperation, 0, len(all))
	for _, op := range all {
		if op != nil {
			out = append(out, op)
		}
	}
	return out
}

// contractDocument 是门关心的契约子树。
type contractDocument struct {
	Paths      map[string]contractPathItem `yaml:"paths"`
	Components struct {
		Parameters map[string]contractParameter `yaml:"parameters"`
	} `yaml:"components"`
}

// contractHeaderParams 返回契约里所有 in: header 的参数名，去重且排序。
func contractHeaderParams(t *testing.T, doc []byte) []string {
	t.Helper()
	var parsed contractDocument
	if err := yaml.Unmarshal(doc, &parsed); err != nil {
		t.Fatalf("parse %s: %v", contractSpecPath, err)
	}
	if len(parsed.Paths) == 0 {
		t.Fatal("契约里没有 paths；门没有扫到东西")
	}

	seen := map[string]bool{}
	var names []string
	add := func(raw contractParameter) {
		param := raw
		if param.Ref != "" {
			param = resolveParameter(t, parsed, param.Ref)
		}
		if param.In != "header" {
			return
		}
		if param.Name == "" {
			t.Fatalf("%s 里的头参数没有 name", contractSpecPath)
		}
		if !seen[param.Name] {
			seen[param.Name] = true
			names = append(names, param.Name)
		}
	}
	// 组件参数全量登记：未被引用也算契约里声明过的头。
	for _, param := range parsed.Components.Parameters {
		add(param)
	}
	for _, item := range parsed.Paths {
		for _, param := range item.Parameters {
			add(param)
		}
		for _, op := range item.operations() {
			for _, param := range op.Parameters {
				add(param)
			}
		}
	}
	sort.Strings(names)
	return names
}

// resolveParameter 解析 $ref。只接受 components.parameters 前缀。
// 未知指向直接失败：静默跳过会让门漏掉参数。
func resolveParameter(t *testing.T, doc contractDocument, ref string) contractParameter {
	t.Helper()
	name := strings.TrimPrefix(ref, contractParameterRefPrefix)
	if name == ref {
		t.Fatalf("不支持的 $ref：%s；门只解析 %s", ref, contractParameterRefPrefix)
	}
	param, ok := doc.Components.Parameters[name]
	if !ok {
		t.Fatalf("$ref 指向不存在的组件参数：%s", ref)
	}
	if param.Ref != "" {
		t.Fatalf("组件参数 %s 又指向 %s；门不支持二级 $ref", name, param.Ref)
	}
	return param
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
	names := contractHeaderParams(t, raw)
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

// 三种合法书写与 $ref 都必须识别：流式、行尾注释、in 在 name 之前。
// 判据是「契约里每个 in: header」，不是某一种书写方式。
func TestContractHeaderParamShapes(t *testing.T) {
	docs := map[string]string{
		"flow": `paths:
  /probe:
    get:
      operationId: Probe
      parameters:
        - { name: If-Match, in: header }
`,
		"trailing comment": `paths:
  /probe:
    get:
      operationId: Probe
      parameters:
        - name: If-Match
          in: header  # 条件头
`,
		"in before name": `paths:
  /probe:
    get:
      operationId: Probe
      parameters:
        - in: header
          name: If-Match
`,
		"ref": `paths:
  /probe:
    get:
      operationId: Probe
      parameters:
        - $ref: '#/components/parameters/Conditional'
components:
  parameters:
    Conditional:
      name: If-Match
      in: header
`,
		"query is not a header": `paths:
  /probe:
    get:
      operationId: Probe
      parameters:
        - { name: limit, in: query }
`,
	}
	for name, doc := range docs {
		t.Run(name, func(t *testing.T) {
			names := contractHeaderParams(t, []byte(doc))
			if name == "query is not a header" {
				if len(names) != 0 {
					t.Fatalf("names = %v, want 空", names)
				}
				return
			}
			if len(names) != 1 || names[0] != "If-Match" {
				t.Fatalf("names = %v, want [If-Match]", names)
			}
		})
	}
}
