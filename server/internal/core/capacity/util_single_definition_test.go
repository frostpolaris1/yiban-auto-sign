package capacity

import (
	"io/fs"
	"os"
	"path/filepath"
	"regexp"
	"strconv"
	"strings"
	"testing"
)

// utilDefRe 匹配「util 形状的常量被赋成 0.8」。
// 名字必须含 util（大小写不敏感），避免把图像面积比例一类无关的 0.8 算进来。
var utilDefRe = regexp.MustCompile(`(?m)^\s*(?:const\s+)?([A-Za-z_]\w*)\s*=\s*0\.8\b`)

// pyUtilRe 匹配旧引擎的利用系数定义。
var pyUtilRe = regexp.MustCompile(`(?m)^_DEFAULT_UTIL\s*=\s*([0-9.]+)\s*$`)

// TestUtilDefaultHasSingleDefinition 钉住利用系数只有一个定义点。
//
// 两个包各定义一份 0.8 时，改一份不改另一份就是静默漂移：
// 装配取一个值、测试或预览取另一个值。本测试扫生产 Go 源（不含 _test.go），
// 发现第二份 util 常量即红。
func TestUtilDefaultHasSingleDefinition(t *testing.T) {
	// cwd = server/internal/core/capacity；server 根是三跳。
	root := filepath.Join("..", "..", "..")
	var defs []string
	err := filepath.WalkDir(root, func(path string, d fs.DirEntry, err error) error {
		if err != nil {
			return err
		}
		if d.IsDir() {
			switch d.Name() {
			case ".git", "node_modules", "testdata":
				return filepath.SkipDir
			}
			return nil
		}
		if !strings.HasSuffix(d.Name(), ".go") || strings.HasSuffix(d.Name(), "_test.go") {
			return nil
		}
		body, err := os.ReadFile(path)
		if err != nil {
			return err
		}
		for _, m := range utilDefRe.FindAllStringSubmatch(string(body), -1) {
			if strings.Contains(strings.ToLower(m[1]), "util") {
				defs = append(defs, m[1]+"（"+path+"）")
			}
		}
		return nil
	})
	if err != nil {
		t.Fatalf("扫描生产 Go 源失败: %v", err)
	}
	if len(defs) == 0 {
		t.Fatal("没扫到任何 util 常量定义——扫描面塌了即拒判")
	}
	if len(defs) > 1 {
		t.Fatalf("util 缺省有 %d 个定义点，want 1：%s", len(defs), strings.Join(defs, "；"))
	}
	if !strings.HasPrefix(defs[0], "UtilDefault（") {
		t.Fatalf("util 常量的名字不是 UtilDefault：%s", defs[0])
	}

	// 旧线（Python）的同一系数也必须相等：两线容量公式同口径。
	// Python 无法 import Go 常量，故在此显式对账。
	pyBody, err := os.ReadFile(filepath.Join("..", "..", "..", "..", "yiban", "engine", "schedule.py"))
	if err != nil {
		t.Fatalf("读旧引擎调度源失败: %v", err)
	}
	matches := pyUtilRe.FindAllStringSubmatch(string(pyBody), -1)
	if len(matches) != 1 {
		t.Fatalf("旧引擎 util 定义点 = %d，want 1", len(matches))
	}
	pyUtil, err := strconv.ParseFloat(matches[0][1], 64)
	if err != nil {
		t.Fatalf("解析旧引擎 util 失败: %v", err)
	}
	if pyUtil != UtilDefault {
		t.Fatalf("两线 util 漂移：Go %v，Python %v", UtilDefault, pyUtil)
	}
}
