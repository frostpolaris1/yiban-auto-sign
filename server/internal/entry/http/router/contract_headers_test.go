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
// 路径项取自 paths、webhooks 与 components.pathItems。
// 路径项可 $ref 到 components.pathItems；组件路径项另作全量扫。
// 操作覆盖全部八种方法，含 trace。
// 扫空不得当绿；指向未知组件的 $ref 直接失败。

import (
	"errors"
	"fmt"
	"os"
	"sort"
	"strings"
	"testing"

	"github.com/goccy/go-yaml"
)

const contractSpecPath = "../../../../contracts/openapi.yaml"

// contractParameterRefPrefix 是门唯一解析的参数 $ref 前缀。
const contractParameterRefPrefix = "#/components/parameters/"

// contractPathItemRefPrefix 是门唯一解析的路径项 $ref 前缀。
const contractPathItemRefPrefix = "#/components/pathItems/"

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
// $ref 让一个路径项复用 components.pathItems 里的另一项。
type contractPathItem struct {
	Ref        string              `yaml:"$ref"`
	Parameters []contractParameter `yaml:"parameters"`
	Get        *contractOperation  `yaml:"get"`
	Put        *contractOperation  `yaml:"put"`
	Post       *contractOperation  `yaml:"post"`
	Delete     *contractOperation  `yaml:"delete"`
	Patch      *contractOperation  `yaml:"patch"`
	Head       *contractOperation  `yaml:"head"`
	Options    *contractOperation  `yaml:"options"`
	Trace      *contractOperation  `yaml:"trace"`
}

// operations 返回路径项下所有已声明的方法。
func (p contractPathItem) operations() []*contractOperation {
	all := []*contractOperation{p.Get, p.Put, p.Post, p.Delete, p.Patch, p.Head, p.Options, p.Trace}
	out := make([]*contractOperation, 0, len(all))
	for _, op := range all {
		if op != nil {
			out = append(out, op)
		}
	}
	return out
}

// contractDocument 是门关心的契约子树。
// webhooks 与 paths 同形：两者都挂路径项。
type contractDocument struct {
	Paths      map[string]contractPathItem `yaml:"paths"`
	Webhooks   map[string]contractPathItem `yaml:"webhooks"`
	Components struct {
		Parameters map[string]contractParameter `yaml:"parameters"`
		PathItems  map[string]contractPathItem  `yaml:"pathItems"`
	} `yaml:"components"`
}

// contractHeaderParams 返回契约里所有 in: header 的参数名，去重且排序。
// 解析失败一律返回 error：静默跳过会让门漏掉头参数。
func contractHeaderParams(doc []byte) ([]string, error) {
	var parsed contractDocument
	if err := yaml.Unmarshal(doc, &parsed); err != nil {
		return nil, fmt.Errorf("parse 契约 YAML: %w", err)
	}
	if len(parsed.Paths) == 0 {
		return nil, errors.New("契约里没有 paths；门没有扫到东西")
	}

	seen := map[string]bool{}
	var names []string
	add := func(raw contractParameter) error {
		param := raw
		if param.Ref != "" {
			resolved, err := resolveParameter(parsed, param.Ref)
			if err != nil {
				return err
			}
			param = resolved
		}
		if param.In != "header" {
			return nil
		}
		if param.Name == "" {
			return fmt.Errorf("%s 里的头参数没有 name", contractSpecPath)
		}
		if !seen[param.Name] {
			seen[param.Name] = true
			names = append(names, param.Name)
		}
		return nil
	}
	addItem := func(item contractPathItem) error {
		for _, param := range item.Parameters {
			if err := add(param); err != nil {
				return err
			}
		}
		for _, op := range item.operations() {
			for _, param := range op.Parameters {
				if err := add(param); err != nil {
					return err
				}
			}
		}
		return nil
	}
	// 全量扫是检测面：路径项无论写在哪一处，头都会被取到。
	scanItem := func(item contractPathItem) error {
		if err := addItem(item); err != nil {
			return err
		}
		// $ref 目标与本地声明同扫：3.1 允许 $ref 带同级键。
		if item.Ref != "" {
			_, target, err := resolvePathItem(parsed, item.Ref)
			if err != nil {
				return err
			}
			return addItem(target)
		}
		return nil
	}
	// 组件参数全量登记：未被引用也算契约里声明过的头。
	for _, param := range parsed.Components.Parameters {
		if err := add(param); err != nil {
			return nil, err
		}
	}
	// webhooks 与 paths 同权：两者都能声明头参数。
	// 组件路径项全量登记：未被引用也算契约里声明过的头。
	groups := []map[string]contractPathItem{
		parsed.Paths,
		parsed.Webhooks,
		parsed.Components.PathItems,
	}
	for _, group := range groups {
		for _, item := range group {
			if err := scanItem(item); err != nil {
				return nil, err
			}
		}
	}
	sort.Strings(names)
	return names, nil
}

// resolveParameter 解析参数 $ref。只接受 components.parameters 前缀。
// 未知指向返回 error：静默跳过会让门漏掉参数。
func resolveParameter(doc contractDocument, ref string) (contractParameter, error) {
	name := strings.TrimPrefix(ref, contractParameterRefPrefix)
	if name == ref {
		return contractParameter{}, fmt.Errorf(
			"不支持的 $ref：%s；门只解析 %s", ref, contractParameterRefPrefix)
	}
	param, ok := doc.Components.Parameters[name]
	if !ok {
		return contractParameter{}, fmt.Errorf("$ref 指向不存在的组件参数：%s", ref)
	}
	if param.Ref != "" {
		return contractParameter{}, fmt.Errorf(
			"组件参数 %s 又指向 %s；门不支持二级 $ref", name, param.Ref)
	}
	return param, nil
}

// resolvePathItem 解析路径项的 $ref，返回组件名与目标项。
// 只接受 components.pathItems 前缀。
// 未知指向返回 error：静默跳过会让门漏掉该路径项下的参数。
func resolvePathItem(doc contractDocument, ref string) (string, contractPathItem, error) {
	name := strings.TrimPrefix(ref, contractPathItemRefPrefix)
	if name == ref {
		return "", contractPathItem{}, fmt.Errorf(
			"不支持的 $ref：%s；门只解析 %s", ref, contractPathItemRefPrefix)
	}
	item, ok := doc.Components.PathItems[name]
	if !ok {
		return "", contractPathItem{}, fmt.Errorf("$ref 指向不存在的组件路径项：%s", ref)
	}
	if item.Ref != "" {
		return "", contractPathItem{}, fmt.Errorf(
			"组件路径项 %s 又指向 %s；门不支持二级 $ref", name, item.Ref)
	}
	return name, item, nil
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
	names, err := contractHeaderParams(raw)
	if err != nil {
		t.Fatalf("%v", err)
	}
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

// 书写方式与所有承载位置都必须识别：流式、行尾注释、in 在 name 之前、
// 参数 $ref、组件路径项、路径项 $ref、trace、webhooks。
// 判据是「契约里每个 in: header」，不是某一种书写方式或某一个位置。
func TestContractHeaderParamShapes(t *testing.T) {
	cases := []struct {
		name string
		doc  string
		want []string
	}{
		{name: "flow", doc: `paths:
  /probe:
    get:
      operationId: Probe
      parameters:
        - { name: If-Match, in: header }
`, want: []string{"If-Match"}},
		{name: "trailing comment", doc: `paths:
  /probe:
    get:
      operationId: Probe
      parameters:
        - name: If-Match
          in: header  # 条件头
`, want: []string{"If-Match"}},
		{name: "in before name", doc: `paths:
  /probe:
    get:
      operationId: Probe
      parameters:
        - in: header
          name: If-Match
`, want: []string{"If-Match"}},
		{name: "ref", doc: `paths:
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
`, want: []string{"If-Match"}},
		{name: "component path item", doc: `paths:
  /probe:
    get:
      operationId: Probe
components:
  pathItems:
    Conditional:
      get:
        operationId: Conditional
        parameters:
          - { name: If-Match, in: header }
`, want: []string{"If-Match"}},
		{name: "path item ref", doc: `paths:
  /probe:
    $ref: '#/components/pathItems/Conditional'
    parameters:
      - { name: X-Probe, in: header }
components:
  pathItems:
    Conditional:
      get:
        operationId: Conditional
        parameters:
          - { name: If-Match, in: header }
`, want: []string{"If-Match", "X-Probe"}},
		{name: "trace", doc: `paths:
  /probe:
    trace:
      operationId: Probe
      parameters:
        - { name: If-Match, in: header }
`, want: []string{"If-Match"}},
		{name: "webhook", doc: `paths:
  /probe:
    get:
      operationId: Probe
webhooks:
  probeEvent:
    post:
      operationId: ProbeEvent
      parameters:
        - { name: If-Match, in: header }
`, want: []string{"If-Match"}},
		{name: "query is not a header", doc: `paths:
  /probe:
    get:
      operationId: Probe
      parameters:
        - { name: limit, in: query }
`, want: nil},
	}
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			names, err := contractHeaderParams([]byte(c.doc))
			if err != nil {
				t.Fatalf("%v", err)
			}
			if len(names) != len(c.want) {
				t.Fatalf("names = %v, want %v", names, c.want)
			}
			for i := range names {
				if names[i] != c.want[i] {
					t.Fatalf("names = %v, want %v", names, c.want)
				}
			}
		})
	}
}

// $ref 解析必须 fail-closed：前缀不符、目标不存在、二级 $ref 都返回 error。
// 静默跳过会让门漏掉该路径项/参数下的头。
// 正例由上面两个用例覆盖：合法 $ref 必须解析成功。
func TestContractHeaderRefFailClosed(t *testing.T) {
	const paramRef = `paths:
  /probe:
    get:
      operationId: Probe
      parameters:
        - $ref: '%s'
`
	const itemRef = `paths:
  /probe:
    $ref: '%s'
`
	cases := []struct{ name, doc, want string }{
		{
			name: "parameter prefix mismatch",
			doc:  fmt.Sprintf(paramRef, "#/components/schemas/Conditional"),
			want: "不支持的 $ref",
		},
		{
			name: "parameter target missing",
			doc:  fmt.Sprintf(paramRef, "#/components/parameters/Missing"),
			want: "不存在的组件参数",
		},
		{
			name: "parameter second level",
			doc: fmt.Sprintf(paramRef, "#/components/parameters/Chained") + `components:
  parameters:
    Chained:
      $ref: '#/components/parameters/Other'
    Other:
      name: limit
      in: query
`,
			want: "二级 $ref",
		},
		{
			name: "path item prefix mismatch",
			doc:  fmt.Sprintf(itemRef, "#/components/schemas/Conditional"),
			want: "不支持的 $ref",
		},
		{
			name: "path item target missing",
			doc:  fmt.Sprintf(itemRef, "#/components/pathItems/Missing"),
			want: "不存在的组件路径项",
		},
		{
			name: "path item second level",
			doc: fmt.Sprintf(itemRef, "#/components/pathItems/Chained") + `components:
  pathItems:
    Chained:
      $ref: '#/components/pathItems/Other'
`,
			want: "二级 $ref",
		},
	}
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			names, err := contractHeaderParams([]byte(c.doc))
			if err == nil {
				t.Fatalf("没有报错；静默跳过会让门漏掉头参数（names=%v）", names)
			}
			if !strings.Contains(err.Error(), c.want) {
				t.Fatalf("err = %v, want contains %q", err, c.want)
			}
		})
	}
}
