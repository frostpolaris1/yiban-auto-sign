package protocol

import (
	"encoding/json"
	"fmt"
	"go/ast"
	"go/parser"
	"go/token"
	"os"
	"path/filepath"
	"sort"
	"strconv"
	"strings"
	"testing"
)

// 本文件是夹具来源门。
//
// 判据来自实验线约定 §7.2(c)：synthetic 样本只能证明解析分支，
// 不能证明业务成功。门分两层：
//
//  1. 登记门：testdata 下每份夹具必须入 provenance.json。
//     response 夹具还必须在 replay_manifest.json 中，且两处来源一致。
//     synthetic 与 unknown 夹具都不得被断言为业务成功。
//  2. 断言门：静态扫描 internal/signing 下的测试函数。
//     同一函数既读非 captured 夹具、又断成功，即红。
//
// 为何按函数而不是按包判：协议包在同一个文件里对 captured 夹具
// 断言成功，同时读 synthetic 夹具。按包判会误红。
//
// 为何只认两种成功面：本模块只有两个成功谓词。
// contract.OutcomeSuccess 是契约结果；CodeIsOK 是信封成功判据。
// 其他临时比较不在门内，登记为已知限制。
//
// 提供方假上游另有一道运行时守卫：它只读 captured 夹具。
// 理由：一致性套件把该假上游当作成功场景。

const (
	provenanceCaptured  = "captured"
	provenanceSynthetic = "synthetic"
	provenanceUnknown   = "unknown"

	fixtureKindResponse = "response"
	fixtureKindInput    = "input"

	provenanceRegistryName = "provenance.json"
	replayManifestName     = "replay_manifest.json"
	provenanceDocName      = "PROVENANCE.md"
)

// provenanceEntry 是一条夹具来源登记。
type provenanceEntry struct {
	Provenance string `json:"provenance"`
	Kind       string `json:"kind"`
	Note       string `json:"note"`
}

type provenanceRegistry struct {
	Version int                        `json:"version"`
	Files   map[string]provenanceEntry `json:"files"`
}

type replayManifest struct {
	Fixtures map[string]struct {
		File       string `json:"file"`
		Provenance string `json:"provenance"`
	} `json:"fixtures"`
}

func loadProvenanceRegistry(t *testing.T) provenanceRegistry {
	t.Helper()
	raw, err := os.ReadFile(filepath.Join("testdata", provenanceRegistryName))
	if err != nil {
		t.Fatalf("read provenance registry: %v", err)
	}
	var reg provenanceRegistry
	if err := json.Unmarshal(raw, &reg); err != nil {
		t.Fatalf("parse provenance registry: %v", err)
	}
	return reg
}

func loadReplayManifest(t *testing.T) replayManifest {
	t.Helper()
	raw, err := os.ReadFile(filepath.Join("testdata", replayManifestName))
	if err != nil {
		t.Fatalf("read replay manifest: %v", err)
	}
	var manifest replayManifest
	if err := json.Unmarshal(raw, &manifest); err != nil {
		t.Fatalf("parse replay manifest: %v", err)
	}
	return manifest
}

// TestFixtureProvenanceRegistry 钉住登记面。
// 空目录、空登记表、漏登记、过期登记、两表不一致都判红。
func TestFixtureProvenanceRegistry(t *testing.T) {
	entries, err := os.ReadDir("testdata")
	if err != nil {
		t.Fatalf("read testdata: %v", err)
	}
	if len(entries) == 0 {
		t.Fatal("testdata is empty: the provenance gate has nothing to check")
	}

	registry := loadProvenanceRegistry(t)
	if len(registry.Files) == 0 {
		t.Fatal("provenance registry is empty: every fixture needs an entry")
	}
	manifest := loadReplayManifest(t)
	if len(manifest.Fixtures) == 0 {
		t.Fatal("replay manifest is empty: every response fixture needs an entry")
	}

	var problems []string
	manifestByFile := make(map[string]string, len(manifest.Fixtures))
	for key, fixture := range manifest.Fixtures {
		if fixture.File == "" {
			problems = append(problems, fmt.Sprintf("manifest entry %q has no file field", key))
			continue
		}
		if fixture.Provenance != provenanceCaptured && fixture.Provenance != provenanceSynthetic {
			problems = append(problems, fmt.Sprintf(
				"manifest entry %q: provenance %q is not %s or %s",
				key, fixture.Provenance, provenanceCaptured, provenanceSynthetic))
		}
		if previous, ok := manifestByFile[fixture.File]; ok {
			problems = append(problems, fmt.Sprintf(
				"manifest entry %q repeats file %s (first provenance %s)", key, fixture.File, previous))
			continue
		}
		manifestByFile[fixture.File] = fixture.Provenance
	}

	for name, entry := range registry.Files {
		switch entry.Provenance {
		case provenanceCaptured, provenanceSynthetic, provenanceUnknown:
		default:
			problems = append(problems, fmt.Sprintf(
				"fixture %s: provenance %q is not %s, %s or %s",
				name, entry.Provenance, provenanceCaptured, provenanceSynthetic, provenanceUnknown))
		}
		if _, err := os.Stat(filepath.Join("testdata", name)); err != nil {
			problems = append(problems, fmt.Sprintf(
				"fixture %s is registered but does not exist: %v", name, err))
		}
		switch entry.Kind {
		case fixtureKindResponse:
			if entry.Provenance == provenanceUnknown {
				problems = append(problems, fmt.Sprintf(
					"fixture %s is a response but its provenance is unknown; mark %s or %s",
					name, provenanceCaptured, provenanceSynthetic))
			}
			manifestProvenance, ok := manifestByFile[name]
			if !ok {
				problems = append(problems, fmt.Sprintf(
					"fixture %s is a response but is missing from %s", name, replayManifestName))
			} else if manifestProvenance != entry.Provenance {
				problems = append(problems, fmt.Sprintf(
					"fixture %s: registry says %s, manifest says %s",
					name, entry.Provenance, manifestProvenance))
			}
		case fixtureKindInput:
			if _, ok := manifestByFile[name]; ok {
				problems = append(problems, fmt.Sprintf(
					"fixture %s is an input but appears in %s", name, replayManifestName))
			}
		default:
			problems = append(problems, fmt.Sprintf(
				"fixture %s: kind %q is not %s or %s",
				name, entry.Kind, fixtureKindResponse, fixtureKindInput))
		}
	}

	for name := range manifestByFile {
		entry, ok := registry.Files[name]
		if !ok {
			problems = append(problems, fmt.Sprintf(
				"manifest file %s has no provenance entry", name))
			continue
		}
		if entry.Kind != fixtureKindResponse {
			problems = append(problems, fmt.Sprintf(
				"manifest file %s is registered as %q, want %s",
				name, entry.Kind, fixtureKindResponse))
		}
	}

	exempt := map[string]bool{
		provenanceDocName:      true,
		provenanceRegistryName: true,
		replayManifestName:     true,
	}
	for _, dirEntry := range entries {
		name := dirEntry.Name()
		if exempt[name] {
			continue
		}
		if dirEntry.IsDir() {
			problems = append(problems, fmt.Sprintf("testdata/%s is a directory, want a file", name))
			continue
		}
		if _, ok := registry.Files[name]; !ok {
			problems = append(problems, fmt.Sprintf("fixture %s has no provenance entry", name))
		}
	}

	if len(problems) > 0 {
		sort.Strings(problems)
		t.Fatalf("provenance registry gate failed:\n%s", strings.Join(problems, "\n"))
	}
}

// TestNonCapturedFixturesNotAssertedAsSuccess 钉住断言面。
//
// 粒度是测试函数：同一函数读到非 captured 夹具，又出现成功断言，判红。
// 该粒度与实现形状对齐：协议包对 captured 夹具断成功，对 synthetic
// 夹具只断解析分支，两类断言分属不同函数。
// 逐字面量判读夹具名；动态拼接的夹具名不被识别，已登记为已知限制。
func TestNonCapturedFixturesNotAssertedAsSuccess(t *testing.T) {
	registry := loadProvenanceRegistry(t)
	nonCaptured := make(map[string]string)
	for name, entry := range registry.Files {
		if entry.Provenance != provenanceCaptured {
			nonCaptured[name] = entry.Provenance
		}
	}

	signingRoot := filepath.Join("..", "..")
	var testFiles []string
	err := filepath.Walk(signingRoot, func(path string, info os.FileInfo, err error) error {
		if err != nil {
			return err
		}
		if info.IsDir() {
			if info.Name() == "testdata" {
				return filepath.SkipDir
			}
			return nil
		}
		if strings.HasSuffix(path, "_test.go") {
			testFiles = append(testFiles, path)
		}
		return nil
	})
	if err != nil {
		t.Fatalf("walk %s: %v", signingRoot, err)
	}
	if len(testFiles) == 0 {
		t.Fatalf("no Go test files under %s: refused to pass", signingRoot)
	}

	var problems []string
	references := 0
	for _, path := range testFiles {
		relative, err := filepath.Rel(signingRoot, path)
		if err != nil {
			relative = path
		}
		relative = filepath.ToSlash(relative)

		fset := token.NewFileSet()
		file, err := parser.ParseFile(fset, path, nil, 0)
		if err != nil {
			problems = append(problems, fmt.Sprintf("%s: parse: %v", relative, err))
			continue
		}
		for _, decl := range file.Decls {
			fn, ok := decl.(*ast.FuncDecl)
			if !ok || fn.Body == nil {
				continue
			}
			fixtures := make(map[string]bool)
			var successes []string
			ast.Inspect(fn.Body, func(node ast.Node) bool {
				switch x := node.(type) {
				case *ast.BasicLit:
					if x.Kind != token.STRING {
						break
					}
					literal, err := strconv.Unquote(x.Value)
					if err != nil {
						break
					}
					if name, ok := registeredFixture(literal, registry.Files); ok {
						fixtures[name] = true
						references++
					}
				case *ast.SelectorExpr:
					if x.Sel.Name == "OutcomeSuccess" {
						successes = append(successes, marker(fset, x.Sel, "OutcomeSuccess"))
					}
				case *ast.CallExpr:
					if callName(x.Fun) == "CodeIsOK" {
						successes = append(successes, marker(fset, x, "CodeIsOK"))
					}
				}
				return true
			})
			if len(fixtures) == 0 || len(successes) == 0 {
				continue
			}
			var flagged []string
			for name := range fixtures {
				if provenance, ok := nonCaptured[name]; ok {
					flagged = append(flagged, fmt.Sprintf("%s (%s)", name, provenance))
				}
			}
			if len(flagged) == 0 {
				continue
			}
			sort.Strings(flagged)
			problems = append(problems, fmt.Sprintf(
				"%s: %s reads non-captured fixture(s) %s and asserts success via %s",
				relative, fn.Name.Name, strings.Join(flagged, ", "), strings.Join(successes, ", ")))
		}
	}
	if references == 0 {
		t.Fatalf("no test function reads a registered fixture under %s: refused to pass", signingRoot)
	}
	if len(problems) > 0 {
		sort.Strings(problems)
		t.Fatalf("synthetic success gate failed:\n%s", strings.Join(problems, "\n"))
	}
}

// registeredFixture 把字符串字面量映射到登记夹具名。
// 认直接文件名，也认带 testdata 目录的路径。
func registeredFixture(literal string, files map[string]provenanceEntry) (string, bool) {
	if _, ok := files[literal]; ok {
		return literal, true
	}
	if !strings.Contains(filepath.ToSlash(literal), "testdata/") {
		return "", false
	}
	base := filepath.Base(literal)
	if _, ok := files[base]; ok {
		return base, true
	}
	return "", false
}

// callName 返回调用名；包限定调用取最后一段。
func callName(fun ast.Expr) string {
	switch x := fun.(type) {
	case *ast.Ident:
		return x.Name
	case *ast.SelectorExpr:
		return x.Sel.Name
	}
	return ""
}

func marker(fset *token.FileSet, node ast.Node, name string) string {
	position := fset.Position(node.Pos())
	return fmt.Sprintf("%s at %s:%d", name, filepath.Base(position.Filename), position.Line)
}
