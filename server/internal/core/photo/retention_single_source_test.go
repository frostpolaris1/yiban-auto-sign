package photo

import (
	"os"
	"regexp"
	"sort"
	"strconv"
	"strings"
	"testing"
)

// migrationPath 是保留期闭集的 SQL 镜像。
// cwd = server/internal/core/photo；server 根是三跳。
const migrationPath = "../../../migrations/postgres/00015_photos.sql"

// sqlRetentionSetRe 匹配迁移里的 CHECK 闭集。
var sqlRetentionSetRe = regexp.MustCompile(`ck_photo_retention_days\s+CHECK\s*\(\s*days\s+IN\s*\(([^)]*)\)\s*\)`)

// TestRetentionDaysMatchMigration 钉住 Go 闭集与 SQL 闭集相等。
//
// SQL 的 CHECK 无法引用 Go 常量，故用本测试钉住两侧：
// 任一侧单独改值即红。缺省天数也必须属于同一闭集。
func TestRetentionDaysMatchMigration(t *testing.T) {
	body, err := os.ReadFile(migrationPath)
	if err != nil {
		t.Fatalf("读迁移失败: %v", err)
	}
	m := sqlRetentionSetRe.FindStringSubmatch(string(body))
	if m == nil {
		t.Fatal("迁移里找不到保留期 CHECK 闭集——扫描面塌了即拒判")
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

	got := append([]int(nil), RetentionDays...)
	sort.Ints(got)
	sqlDays = append([]int(nil), sqlDays...)
	sort.Ints(sqlDays)
	if len(got) != len(sqlDays) {
		t.Fatalf("保留期闭集大小漂移：Go %v，SQL %v", got, sqlDays)
	}
	for i := range got {
		if got[i] != sqlDays[i] {
			t.Fatalf("保留期闭集漂移：Go %v，SQL %v", got, sqlDays)
		}
	}
}

// TestRetentionDaysAreSortedUniqueAndContainDefault 钉住闭集的形状与缺省归属。
func TestRetentionDaysAreSortedUniqueAndContainDefault(t *testing.T) {
	if len(RetentionDays) < 2 {
		t.Fatalf("保留期闭集只有 %d 项——删空即拒判", len(RetentionDays))
	}
	for i, days := range RetentionDays {
		if days <= 0 {
			t.Fatalf("保留期 %d 必须为正", days)
		}
		if i > 0 && RetentionDays[i-1] >= days {
			t.Fatalf("保留期闭集必须严格升序：%v", RetentionDays)
		}
	}
	found := false
	for _, days := range RetentionDays {
		if days == DefaultRetentionDays {
			found = true
			break
		}
	}
	if !found {
		t.Fatalf("缺省保留期 %d 不在闭集 %v 内", DefaultRetentionDays, RetentionDays)
	}
}

// TestValidRetentionCoversExactlyTheSet 钉住判据与闭集同源。
func TestValidRetentionCoversExactlyTheSet(t *testing.T) {
	inSet := map[int]bool{}
	for _, days := range RetentionDays {
		inSet[days] = true
		if !ValidRetention(days) {
			t.Fatalf("闭集内的 %d 被判非法", days)
		}
	}
	for days := 1; days <= 400; days++ {
		if inSet[days] != ValidRetention(days) {
			t.Fatalf("ValidRetention(%d) = %v，闭集归属 %v", days, ValidRetention(days), inSet[days])
		}
	}
}
