package photo

import (
	"os"
	"regexp"
	"sort"
	"strconv"
	"strings"
	"testing"
)

// 保留期的四个载体。cwd = server/internal/core/photo。
const (
	migrationPath = "../../../migrations/postgres/00015_photos.sql"
	openapiPath   = "../../../contracts/openapi.yaml"
	generatedPath = "../../entry/http/oapi/oapi.gen.go"
)

var (
	// sqlRetentionSetRe 匹配迁移里的 CHECK 闭集。匹配前先剥 `--` 行注释：
	// 注释掉真实 CHECK 后门必须红，不能被注释里的同形文本骗过。
	sqlRetentionSetRe = regexp.MustCompile(`ck_photo_retention_days\s+CHECK\s*\(\s*days\s+IN\s*\(([^)]*)\)\s*\)`)
	sqlLineCommentRe  = regexp.MustCompile(`(?m)--.*$`)
	// 生成物里的两个枚举类型。同时捕获名字后缀与赋值：
	// 只认名字会让「N90 = 91」这种改值逃逸。
	genRetentionRe    = regexp.MustCompile(`PhotoRetentionDaysN(\d+)\s+PhotoRetentionDays\s*=\s*(\d+)`)
	genRetentionUpdRe = regexp.MustCompile(`PhotoRetentionUpdateDaysN(\d+)\s+PhotoRetentionUpdateDays\s*=\s*(\d+)`)
)

// sortedInts 返回排序后的副本。
func sortedInts(in []int) []int {
	out := append([]int(nil), in...)
	sort.Ints(out)
	return out
}

// sameInts 报告两个集合（按排序比较）是否相等。
func sameInts(a, b []int) bool {
	as, bs := sortedInts(a), sortedInts(b)
	if len(as) != len(bs) {
		return false
	}
	for i := range as {
		if as[i] != bs[i] {
			return false
		}
	}
	return true
}

// TestRetentionDaysMatchMigration 钉住 Go 闭集与 SQL 闭集相等。
// SQL 无法引用 Go 常量，故用本测试钉住两侧；注释掉的 CHECK 不算数。
func TestRetentionDaysMatchMigration(t *testing.T) {
	body, err := os.ReadFile(migrationPath)
	if err != nil {
		t.Fatalf("读迁移失败: %v", err)
	}
	sql := sqlLineCommentRe.ReplaceAllString(string(body), "")
	m := sqlRetentionSetRe.FindStringSubmatch(sql)
	if m == nil {
		t.Fatal("迁移里找不到未注释的保留期 CHECK 闭集——扫描面塌了即拒判")
	}
	var sqlDays []int
	for _, part := range strings.Split(m[1], ",") {
		part = strings.TrimSpace(part)
		if part == "" {
			continue
		}
		v, err := strconv.Atoi(part)
		if err != nil {
			t.Fatalf("迁移闭集含非整数 %q: %v", part, err)
		}
		sqlDays = append(sqlDays, v)
	}
	if len(sqlDays) == 0 {
		t.Fatal("迁移闭集解析为空——扫空不得当绿")
	}
	if !sameInts(RetentionDays(), sqlDays) {
		t.Fatalf("保留期闭集漂移：Go %v，SQL %v", RetentionDays(), sqlDays)
	}
}

// TestRetentionDaysMatchOpenAPI 钉住契约文件的两处 enum 与散文缺省。
// spec 是外部消费方的可见契约；只钉 SQL 会漏掉这一面。
func TestRetentionDaysMatchOpenAPI(t *testing.T) {
	body, err := os.ReadFile(openapiPath)
	if err != nil {
		t.Fatalf("读 OpenAPI 契约失败: %v", err)
	}
	text := string(body)
	days := RetentionDays()
	if len(days) < 2 {
		t.Fatalf("保留期闭集只有 %d 项——删空即拒判", len(days))
	}

	parts := make([]string, len(days))
	for i, d := range days {
		parts[i] = strconv.Itoa(d)
	}
	wantEnum := "enum: [" + strings.Join(parts, ", ") + "]"
	if got := strings.Count(text, wantEnum); got != 2 {
		t.Fatalf("契约里 %q 出现 %d 次，want 2（PhotoRetention 与 PhotoRetentionUpdate）", wantEnum, got)
	}

	spoken := strings.Join(parts[:len(parts)-1], ", ") + " or " + parts[len(parts)-1]
	wantProse := "days is one of " + spoken + ". Missing retention means " + strconv.Itoa(DefaultRetentionDays) + " days."
	if !strings.Contains(text, wantProse) {
		t.Fatalf("契约散文漂移：want %q", wantProse)
	}
}

// TestRetentionDaysMatchGenerated 钉住生成物的两个枚举类型。
// 生成物由 generate.sh --check 钉住 spec；本测试让焦点测试也能独立发现漂移。
func TestRetentionDaysMatchGenerated(t *testing.T) {
	body, err := os.ReadFile(generatedPath)
	if err != nil {
		t.Fatalf("读生成物失败: %v", err)
	}
	for name, re := range map[string]*regexp.Regexp{
		"PhotoRetentionDays":       genRetentionRe,
		"PhotoRetentionUpdateDays": genRetentionUpdRe,
	} {
		var got []int
		for _, m := range re.FindAllStringSubmatch(string(body), -1) {
			if m[1] != m[2] {
				t.Fatalf("%s 常量名与值不符：N%s = %s", name, m[1], m[2])
			}
			v, err := strconv.Atoi(m[2])
			if err != nil {
				t.Fatalf("%s 生成值 %q 不是整数: %v", name, m[2], err)
			}
			got = append(got, v)
		}
		if len(got) == 0 {
			t.Fatalf("%s 在生成物里没有常量——扫描面塌了即拒判", name)
		}
		if !sameInts(got, RetentionDays()) {
			t.Fatalf("%s 枚举漂移：生成物 %v，Go %v", name, sortedInts(got), RetentionDays())
		}
	}
}

// TestRetentionDaysAreSortedUniqueAndContainDefault 钉住闭集的形状与缺省归属。
func TestRetentionDaysAreSortedUniqueAndContainDefault(t *testing.T) {
	days := RetentionDays()
	if len(days) < 2 {
		t.Fatalf("保留期闭集只有 %d 项——删空即拒判", len(days))
	}
	for i, d := range days {
		if d <= 0 {
			t.Fatalf("保留期 %d 必须为正", d)
		}
		if i > 0 && days[i-1] >= d {
			t.Fatalf("保留期闭集必须严格升序：%v", days)
		}
	}
	found := false
	for _, d := range days {
		if d == DefaultRetentionDays {
			found = true
			break
		}
	}
	if !found {
		t.Fatalf("缺省保留期 %d 不在闭集 %v 内", DefaultRetentionDays, days)
	}
}

// TestValidRetentionCoversExactlyTheSet 钉住判据与闭集同源。
func TestValidRetentionCoversExactlyTheSet(t *testing.T) {
	inSet := map[int]bool{}
	for _, d := range RetentionDays() {
		inSet[d] = true
		if !ValidRetention(d) {
			t.Fatalf("闭集内的 %d 被判非法", d)
		}
	}
	for d := 1; d <= 400; d++ {
		if inSet[d] != ValidRetention(d) {
			t.Fatalf("ValidRetention(%d) = %v，闭集归属 %v", d, ValidRetention(d), inSet[d])
		}
	}
}

// TestRetentionDaysReturnsCopy 钉住访问器不暴露可变包状态。
// 调用方改返回值不得改变 ValidRetention 的判据。
func TestRetentionDaysReturnsCopy(t *testing.T) {
	first := RetentionDays()
	if len(first) == 0 {
		t.Fatal("闭集为空——删空即拒判")
	}
	original := first[0]
	first[0] = 999
	if !ValidRetention(original) {
		t.Fatalf("改返回值后 %d 被判非法：访问器暴露了可变内部状态", original)
	}
	if got := RetentionDays()[0]; got != original {
		t.Fatalf("二次取值 = %d，want %d", got, original)
	}
}
