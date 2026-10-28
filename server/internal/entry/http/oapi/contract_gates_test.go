package oapi

// 契约对账门。
//
// # 功能
// 直接读 contracts/openapi.yaml 与 contracts/README.md，钉住五条不变式：
// 角色表两份逐行相等；带 limit 的列表声明 400；带请求体的端点声明 413；
// 可空字段一律 required（保证输出 null 键）；additionalProperties: false
// 逐条登记豁免。本文件**不**启动 HTTP 服务、**不**连数据库。
//
// # 归属
// entry/http/oapi 是生成物所在包，契约文本的唯一消费者。
//
// # 口径
// 解析按行：路径、方法、x-roles、requestBody、状态码、properties 与 required。
// 扫描结果为空即失败，**扫空不得当绿**。
// openapi 是权威表；README 的端点行必须与它逐行相等。

import (
	"bytes"
	"os"
	"regexp"
	"sort"
	"strings"
	"testing"
)

const (
	contractSpecPath   = "../../../../contracts/openapi.yaml"
	contractReadmePath = "../../../../contracts/README.md"
)

// contractVerbs 是 HTTP 方法白名单，用来把路径与操作行分开。
var contractVerbs = map[string]bool{
	"get": true, "post": true, "put": true, "patch": true, "delete": true,
	"head": true, "options": true,
}

var (
	contractStatusLine    = regexp.MustCompile(`^'([0-9]{3})':`)
	contractPathParam     = regexp.MustCompile(`:([A-Za-z_][A-Za-z0-9_]*)`)
	contractInlineProp    = regexp.MustCompile(`^\s*([A-Za-z_][A-Za-z0-9_]*): \{.*type:`)
	contractKeyLine       = regexp.MustCompile(`^\s*([A-Za-z_][A-Za-z0-9_]*):`)
	contractRequired      = regexp.MustCompile(`^(\s*)required: \[(.*)\]\s*$`)
	contractRequiredBlock = regexp.MustCompile(`^(\s*)required:\s*$`)
	contractRequiredItem  = regexp.MustCompile(`^\s*-\s*([A-Za-z_][A-Za-z0-9_]*)\s*$`)
)

// contractOp 是一条路径操作的对账摘录。
type contractOp struct {
	method   string
	path     string
	roles    []string
	hasBody  bool
	hasLimit bool
	statuses map[string]bool
	line     int
}

func contractRead(t *testing.T, path string) string {
	t.Helper()
	raw, err := os.ReadFile(path)
	if err != nil {
		t.Fatalf("read %s: %v", path, err)
	}
	if len(bytes.TrimSpace(raw)) == 0 {
		t.Fatalf("%s is empty; the gate DID NOT RUN", path)
	}
	return string(raw)
}

// contractOps 解析 openapi 的路径操作。
func contractOps(t *testing.T, doc string) map[string]*contractOp {
	t.Helper()
	ops := map[string]*contractOp{}
	inPaths := false
	path := ""
	var cur *contractOp
	inResponses := false
	for i, raw := range strings.Split(doc, "\n") {
		line := strings.TrimRight(raw, " \t\r")
		trimmed := strings.TrimSpace(line)
		if trimmed == "" || strings.HasPrefix(trimmed, "#") {
			continue
		}
		indent := len(line) - len(strings.TrimLeft(line, " "))
		switch trimmed {
		case "paths:":
			inPaths, path, cur, inResponses = true, "", nil, false
			continue
		case "components:":
			inPaths, cur = false, nil
			continue
		}
		if !inPaths {
			continue
		}
		if indent == 2 && strings.HasPrefix(trimmed, "/") && strings.HasSuffix(trimmed, ":") {
			path = strings.TrimSuffix(trimmed, ":")
			cur, inResponses = nil, false
			continue
		}
		if indent == 4 && strings.HasSuffix(trimmed, ":") &&
			contractVerbs[strings.TrimSuffix(trimmed, ":")] {
			method := strings.ToUpper(strings.TrimSuffix(trimmed, ":"))
			cur = &contractOp{method: method, path: path,
				statuses: map[string]bool{}, line: i + 1}
			ops[method+" "+path] = cur
			inResponses = false
			continue
		}
		if cur == nil {
			continue
		}
		if indent == 6 {
			switch {
			case strings.HasPrefix(trimmed, "x-roles:"):
				cur.roles = contractRoles(trimmed)
			case trimmed == "requestBody:":
				cur.hasBody = true
			case trimmed == "responses:":
				inResponses = true
			case trimmed == "parameters:":
				inResponses = false
			}
			continue
		}
		if strings.Contains(trimmed, "#/components/parameters/Limit'") {
			cur.hasLimit = true
		}
		if inResponses {
			if m := contractStatusLine.FindStringSubmatch(trimmed); m != nil {
				cur.statuses[m[1]] = true
			}
		}
	}
	if len(ops) == 0 {
		t.Fatal("openapi 解析出 0 个操作；门没有扫到东西")
	}
	return ops
}

// contractRoles 解析 x-roles 列表。
func contractRoles(line string) []string {
	start := strings.Index(line, "[")
	end := strings.LastIndex(line, "]")
	if start < 0 || end <= start {
		return nil
	}
	var out []string
	for _, part := range strings.Split(line[start+1:end], ",") {
		if role := strings.TrimSpace(part); role != "" {
			out = append(out, role)
		}
	}
	return out
}

// readmeRoles 解析 README 的端点表。路径参数写成 :name，比对前统一成 {name}。
func readmeRoles(t *testing.T, doc string) map[string]*contractOp {
	t.Helper()
	out := map[string]*contractOp{}
	for i, raw := range strings.Split(doc, "\n") {
		trimmed := strings.TrimSpace(raw)
		if !strings.HasPrefix(trimmed, "|") {
			continue
		}
		cells := strings.Split(trimmed, "|")
		if len(cells) != 6 {
			continue
		}
		method := strings.TrimSpace(cells[1])
		if !contractVerbs[strings.ToLower(method)] {
			continue
		}
		path := strings.Trim(strings.TrimSpace(cells[2]), "`")
		path = contractPathParam.ReplaceAllString(path, "{$1}")
		var roles []string
		for _, role := range strings.Split(strings.TrimSpace(cells[3]), "/") {
			if role = strings.TrimSpace(role); role != "" {
				roles = append(roles, role)
			}
		}
		op := &contractOp{method: strings.ToUpper(method), path: path, roles: roles, line: i + 1}
		out[op.method+" "+op.path] = op
	}
	if len(out) == 0 {
		t.Fatal("README 解析出 0 行端点；门没有扫到东西")
	}
	return out
}

func roleSet(roles []string) string {
	cp := append([]string(nil), roles...)
	sort.Strings(cp)
	return strings.Join(cp, ",")
}

// 角色表只有一份事实：openapi 的 x-roles。README 必须逐行相等。
// 两份表读者不同（实现者看 README，生成器看 openapi），漂移会让实现按错的表走。
func TestContractRoleTablesAgree(t *testing.T) {
	spec := contractRead(t, contractSpecPath)
	readme := contractRead(t, contractReadmePath)

	want := contractOps(t, spec)
	got := readmeRoles(t, readme)
	if len(want) < 39 {
		t.Fatalf("openapi 操作数 = %d, want at least 39", len(want))
	}
	if len(got) < 39 {
		t.Fatalf("README 端点数 = %d, want at least 39", len(got))
	}

	for key, op := range want {
		other, ok := got[key]
		if !ok {
			t.Errorf("README 缺行：%s", key)
			continue
		}
		if roleSet(op.roles) != roleSet(other.roles) {
			t.Errorf("%s 角色不一致：openapi=%v README.md:%d=%v",
				key, op.roles, other.line, other.roles)
		}
	}
	for key, op := range got {
		if _, ok := want[key]; !ok {
			t.Errorf("README 多行：%s（README.md:%d）", key, op.line)
		}
	}
}

// 查询串 limit 是 1..200 的校验参数：越界即 400。
// 声明了 limit 却未声明 400，客户端会把它当内部错误。
func TestListLimitEndpointsDeclare400(t *testing.T) {
	spec := contractRead(t, contractSpecPath)
	ops := contractOps(t, spec)
	seen := 0
	for key, op := range ops {
		if !op.hasLimit {
			continue
		}
		seen++
		if !op.statuses["400"] {
			t.Errorf("%s 声明 limit 但未声明 400（openapi.yaml:%d）", key, op.line)
		}
	}
	if seen < 4 {
		t.Fatalf("带 limit 的端点 = %d, want at least 4", seen)
	}
}

// 入口层对超界请求体回 413。任何带请求体的端点都可能触发它，
// 故契约必须声明 413；否则调用方无法区分体积超界与格式错误。
func TestRequestBodyEndpointsDeclare413(t *testing.T) {
	spec := contractRead(t, contractSpecPath)
	ops := contractOps(t, spec)
	seen := 0
	for key, op := range ops {
		if !op.hasBody {
			continue
		}
		seen++
		if !op.statuses["413"] {
			t.Errorf("%s 带请求体但未声明 413（openapi.yaml:%d）", key, op.line)
		}
	}
	if seen < 12 {
		t.Fatalf("带请求体的端点 = %d, want at least 12", seen)
	}
}

// nullableProp 是一条可空属性的对账摘录。
type nullableProp struct {
	schema   string
	prop     string
	line     int
	required bool
}

// contractSchemaBlocks 返回 components.schemas 下每个 schema 的行区间。
func contractSchemaBlocks(t *testing.T, lines []string) map[string][2]int {
	t.Helper()
	blocks := map[string][2]int{}
	inSchemas := false
	start := -1
	name := ""
	for i, raw := range lines {
		line := strings.TrimRight(raw, " \t\r")
		trimmed := strings.TrimSpace(line)
		if trimmed == "" || strings.HasPrefix(trimmed, "#") {
			continue
		}
		indent := len(line) - len(strings.TrimLeft(line, " "))
		if indent == 2 && trimmed == "schemas:" {
			inSchemas = true
			continue
		}
		if !inSchemas {
			continue
		}
		if indent == 2 && strings.HasSuffix(trimmed, ":") && trimmed != "schemas:" {
			inSchemas = false
			if start >= 0 {
				blocks[name] = [2]int{start, i}
			}
			continue
		}
		if indent == 4 && strings.HasSuffix(trimmed, ":") {
			if start >= 0 {
				blocks[name] = [2]int{start, i}
			}
			name = strings.TrimSuffix(trimmed, ":")
			start = i
		}
	}
	if start >= 0 {
		blocks[name] = [2]int{start, len(lines)}
	}
	if len(blocks) == 0 {
		t.Fatal("openapi 解析出 0 个 schema")
	}
	return blocks
}

func contractIndent(line string) int {
	return len(line) - len(strings.TrimLeft(line, " "))
}

// scanNullable 找出所有可空属性，并报告它是否列入所属对象的 required。
func scanNullable(t *testing.T, doc string) []nullableProp {
	t.Helper()
	lines := strings.Split(doc, "\n")
	blocks := contractSchemaBlocks(t, lines)
	var out []nullableProp
	for schema, block := range blocks {
		start, end := block[0], block[1]
		type requiredLine struct {
			indent int
			names  map[string]bool
		}
		var required []requiredLine
		for i := start; i < end; i++ {
			if m := contractRequired.FindStringSubmatch(lines[i]); m != nil {
				names := map[string]bool{}
				for _, part := range strings.Split(m[2], ",") {
					if name := strings.TrimSpace(part); name != "" {
						names[name] = true
					}
				}
				required = append(required, requiredLine{indent: len(m[1]), names: names})
				continue
			}
			// 块状写法：required: 换行后逐条 - name。
			if m := contractRequiredBlock.FindStringSubmatch(lines[i]); m != nil {
				indent := len(m[1])
				names := map[string]bool{}
				for j := i + 1; j < end; j++ {
					if strings.TrimSpace(lines[j]) == "" {
						continue
					}
					if contractIndent(lines[j]) <= indent {
						break
					}
					if item := contractRequiredItem.FindStringSubmatch(lines[j]); item != nil {
						names[item[1]] = true
					}
				}
				required = append(required, requiredLine{indent: indent, names: names})
			}
		}
		for i := start; i < end; i++ {
			line := lines[i]
			if !strings.Contains(line, `"null"`) || !strings.Contains(line, "type:") {
				continue
			}
			prop := ""
			propIndent := 0
			if m := contractInlineProp.FindStringSubmatch(line); m != nil {
				prop, propIndent = m[1], contractIndent(line)
			} else {
				propIndent = contractIndent(line) - 2
				for j := i - 1; j >= start; j-- {
					if contractIndent(lines[j]) != propIndent {
						continue
					}
					if m := contractKeyLine.FindStringSubmatch(lines[j]); m != nil {
						prop = m[1]
						break
					}
				}
			}
			if prop == "" {
				t.Fatalf("%s:%d 无法确定可空字段名", contractSpecPath, i+1)
			}
			ok := false
			for _, r := range required {
				if r.indent == propIndent-2 && r.names[prop] {
					ok = true
				}
			}
			out = append(out, nullableProp{schema: schema, prop: prop, line: i + 1, required: ok})
		}
	}
	return out
}

// 可空响应字段必须 required：缺键与 null 对客户端是两种形状。
// 生成物靠 required 去掉 omitempty，故本门同时钉住 Go 的编码结果。
func TestNullablePropertiesAreRequired(t *testing.T) {
	spec := contractRead(t, contractSpecPath)
	props := scanNullable(t, spec)
	if len(props) < 17 {
		t.Fatalf("可空属性 = %d, want at least 17; 门没有扫到东西", len(props))
	}
	for _, p := range props {
		if !p.required {
			t.Errorf("%s.%s 可空但未列入 required（openapi.yaml:%d）：输出会缺键",
				p.schema, p.prop, p.line)
		}
	}
}

// additionalPropertiesExemptions 是显式豁免登记：每个 schema 一处一条理由。
// 只活在契约里的约束等于违反时静默：新增声明（含嵌套）必须在这里登记。
var additionalPropertiesExemptions = map[string][]string{
	"AccountSignAccepted":     {"响应对象；未知字段被编码器忽略"},
	"AccountLocation":         {"响应对象；未知字段被编码器忽略"},
	"AccountLocationUpdate":   {"请求对象；解码器忽略未知字段"},
	"SchedulePlanRequest":     {"请求对象；解码器忽略未知字段"},
	"ScheduleOverrideRequest": {"请求对象；解码器忽略未知字段"},
	"TimePref":                {"响应对象；未知字段被编码器忽略"},
	"TimePrefUpdate":          {"请求对象；解码器忽略未知字段"},
	"Photo":                   {"响应对象；未知字段被编码器忽略"},
	"PhotoUpload":             {"multipart 表单；解码器忽略未知字段"},
	"PhotoRetention":          {"响应对象；未知字段被编码器忽略"},
	"PhotoRetentionUpdate":    {"请求对象；解码器忽略未知字段"},
}

// contractNoAdditionalProperties 匹配 YAML 映射里的一项 additionalProperties: false。
// 块式（独占一行）与流式（行内 `{ ... }`）两种写法同权；键可带引号，值可带锚点。
// 不覆盖的形状：值写成别名（*x）、值换行写在下一行、整张 schemas 映射写成一行。
// 本契约不使用这三种写法；新增时须同步扩门。
var contractNoAdditionalProperties = regexp.MustCompile(
	`(?:^|[{,])\s*["']?additionalProperties["']?[ \t]*:[ \t]*(?:&\S+[ \t]+)?false[ \t]*(?:[,}]|$)`)

// contractSchemaKey 识别 components.schemas 下的 schema 名。
// 行内 schema 声明（`Name: { ... }`）同样识别，其上的声明不得漏计。
var contractSchemaKey = regexp.MustCompile(`^["']?([A-Za-z0-9_]+)["']?:`)

// schemasDeclaringNoAdditionalProperties 全树扫描 components.schemas。
// 返回 schema → 每处声明的行号；嵌套对象里的声明同样计入。
// 扫描按 YAML 映射项而不是按整行相等：流式写法里的声明也计入。
func schemasDeclaringNoAdditionalProperties(t *testing.T, doc string) map[string][]int {
	t.Helper()
	out := map[string][]int{}
	inSchemas := false
	schema := ""
	for i, raw := range strings.Split(doc, "\n") {
		line := strings.TrimRight(raw, " \t\r")
		trimmed := strings.TrimSpace(line)
		if trimmed == "" || strings.HasPrefix(trimmed, "#") {
			continue
		}
		indent := contractIndent(line)
		if indent == 2 && trimmed == "schemas:" {
			inSchemas = true
			continue
		}
		if !inSchemas {
			continue
		}
		if indent == 2 && strings.HasSuffix(trimmed, ":") && trimmed != "schemas:" {
			inSchemas = false
			continue
		}
		if indent == 4 {
			if m := contractSchemaKey.FindStringSubmatch(trimmed); m != nil {
				schema = m[1]
			}
		}
		// 任意深度都算：嵌套对象的声明与 schema 级声明同权。
		if indent >= 4 && schema != "" && contractNoAdditionalProperties.MatchString(line) {
			out[schema] = append(out[schema], i+1)
		}
	}
	total := 0
	for _, lines := range out {
		total += len(lines)
	}
	if total == 0 {
		t.Fatal("openapi 里没有 additionalProperties: false；门没有扫到东西")
	}
	return out
}

// 运行时**不**执行 additionalProperties: false：解码器忽略未知字段。
// 豁免必须逐条登记；声明数与登记数（含嵌套）两边都不许静默漂移。
func TestAdditionalPropertiesExemptionsRegistered(t *testing.T) {
	spec := contractRead(t, contractSpecPath)
	readme := contractRead(t, contractReadmePath)
	declared := schemasDeclaringNoAdditionalProperties(t, spec)

	names := make([]string, 0, len(declared))
	for name := range declared {
		names = append(names, name)
	}
	sort.Strings(names)

	total := 0
	for _, name := range names {
		lines := declared[name]
		total += len(lines)
		reasons, ok := additionalPropertiesExemptions[name]
		if !ok {
			t.Errorf("%s 有 %d 处 additionalProperties: false（行 %v）但未登记豁免",
				name, len(lines), lines)
			continue
		}
		if len(reasons) != len(lines) {
			t.Errorf("%s 声明 %d 处（行 %v），只登记 %d 条理由",
				name, len(lines), lines, len(reasons))
		}
		for _, reason := range reasons {
			if strings.TrimSpace(reason) == "" {
				t.Errorf("%s 的豁免理由为空", name)
			}
		}
	}
	for name, reasons := range additionalPropertiesExemptions {
		if _, ok := declared[name]; !ok {
			t.Errorf("豁免登记过期：%s 不再声明 additionalProperties: false", name)
		}
		if len(reasons) == 0 {
			t.Errorf("%s 的登记没有任何理由条目", name)
		}
	}
	if total < 11 {
		t.Fatalf("声明总数 = %d, want at least 11；门没有扫到东西", total)
	}
	registered := 0
	for _, reasons := range additionalPropertiesExemptions {
		registered += len(reasons)
	}
	if total != registered {
		t.Fatalf("声明 %d 处，登记 %d 条；两边必须逐条相等", total, registered)
	}
	if !strings.Contains(readme, "additionalProperties") {
		t.Error("README 未写明 additionalProperties 的豁免口径")
	}
}

// 流式写法与块式写法必须同权（4nf）：只认块式时，内联新增的声明会静默放行。
// 合法流式值（映射或 true）不得误报，否则门会逼人登记并不存在的声明。
func TestAdditionalPropertiesScannerCoversFlowStyle(t *testing.T) {
	doc := `components:
  schemas:
    Block:
      type: object
      additionalProperties: false
    FlowNested:
      type: object
      properties:
        probe: { type: object, additionalProperties: false }
    FlowSchema: { type: object, additionalProperties: false }
    LegalFlow:
      type: object
      properties:
        probe: { type: object, additionalProperties: { type: integer } }
        flag: { type: object, additionalProperties: true }
        plain: { type: object }
`
	got := schemasDeclaringNoAdditionalProperties(t, doc)
	want := map[string]int{"Block": 1, "FlowNested": 1, "FlowSchema": 1}
	for name, n := range want {
		if len(got[name]) != n {
			t.Errorf("%s 检出 %d 处 additionalProperties: false，want %d（流式写法漏检）",
				name, len(got[name]), n)
		}
	}
	for name := range got {
		if _, ok := want[name]; !ok {
			t.Errorf("合法流式写法被误报：%s 行 %v", name, got[name])
		}
	}
}
