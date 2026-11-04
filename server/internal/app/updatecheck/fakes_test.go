package updatecheck

// 自检测试的内存替身：文件系统、数据库、时钟与固定输入。
//
// # 口径
// 替身只实现端口语义，不模拟 POSIX 细节；
// 可写性、读写错误与目录缺省都按用例逐项注入。

import (
	"context"
	"os"
	"testing"
	"time"

	"github.com/frostpolaris1/yiban-auto-sign/server/internal/core/fsperm"
)

// fixedNow 是报告与状态文件测试共用的时刻。
var fixedNow = time.Date(2026, 11, 4, 12, 0, 0, 0, time.UTC)

// 测试密钥与 DSN。口令故意带可识别的长串，便于断言不出现在输出里。
const (
	testDSN           = "postgres://yiban:s3cret-pass-word@127.0.0.1:5432/yiban?sslmode=disable"
	testDSNPassword   = "s3cret-pass-word"
	testCredentialKey = "MDEyMzQ1Njc4OWFiY2RlZjAxMjM0NTY3ODlhYmNkZWY="
	testPepper        = "0123456789abcdef0123456789abcdef"
)

// fakeInfo 实现 os.FileInfo。
type fakeInfo struct {
	name string
	dir  bool
	mode os.FileMode
}

func (i fakeInfo) Name() string       { return i.name }
func (i fakeInfo) Size() int64        { return 0 }
func (i fakeInfo) ModTime() time.Time { return time.Time{} }
func (i fakeInfo) IsDir() bool        { return i.dir }
func (i fakeInfo) Sys() any           { return nil }

func (i fakeInfo) Mode() os.FileMode {
	if i.dir {
		return os.ModeDir | i.mode
	}
	return i.mode
}

// fakeEntry 实现 os.DirEntry。
type fakeEntry struct{ name string }

func (e fakeEntry) Name() string               { return e.name }
func (e fakeEntry) IsDir() bool                { return false }
func (e fakeEntry) Type() os.FileMode          { return 0 }
func (e fakeEntry) Info() (os.FileInfo, error) { return fakeInfo{name: e.name}, nil }

// fakeFS 是 FileSystem 的内存替身。
type fakeFS struct {
	files    map[string][]byte
	dirs     map[string][]os.DirEntry
	statErr  map[string]error
	readErr  map[string]error
	writeErr error

	writes map[string][]byte
	perms  map[string]os.FileMode
}

func newFakeFS() *fakeFS {
	return &fakeFS{
		files:   map[string][]byte{},
		dirs:    map[string][]os.DirEntry{},
		statErr: map[string]error{},
		readErr: map[string]error{},
		writes:  map[string][]byte{},
		perms:   map[string]os.FileMode{},
	}
}

func (f *fakeFS) mkdir(path string, entries ...string) *fakeFS {
	list := make([]os.DirEntry, 0, len(entries))
	for _, name := range entries {
		list = append(list, fakeEntry{name: name})
	}
	f.dirs[path] = list
	return f
}

func (f *fakeFS) Stat(name string) (os.FileInfo, error) {
	if err := f.statErr[name]; err != nil {
		return nil, err
	}
	if _, ok := f.files[name]; ok {
		return fakeInfo{name: name, mode: 0o644}, nil
	}
	if _, ok := f.writes[name]; ok {
		return fakeInfo{name: name, mode: 0o600}, nil
	}
	if _, ok := f.dirs[name]; ok {
		return fakeInfo{name: name, dir: true, mode: 0o755}, nil
	}
	return nil, &os.PathError{Op: "stat", Path: name, Err: os.ErrNotExist}
}

func (f *fakeFS) ReadFile(name string) ([]byte, error) {
	if err := f.statErr[name]; err != nil {
		return nil, err
	}
	if body, ok := f.writes[name]; ok {
		return append([]byte(nil), body...), nil
	}
	if body, ok := f.files[name]; ok {
		return append([]byte(nil), body...), nil
	}
	return nil, &os.PathError{Op: "open", Path: name, Err: os.ErrNotExist}
}

func (f *fakeFS) ReadDir(name string) ([]os.DirEntry, error) {
	if err := f.statErr[name]; err != nil {
		return nil, err
	}
	if err := f.readErr[name]; err != nil {
		return nil, err
	}
	entries, ok := f.dirs[name]
	if !ok {
		return nil, &os.PathError{Op: "readdir", Path: name, Err: os.ErrNotExist}
	}
	return entries, nil
}

func (f *fakeFS) WriteFileAtomic(name string, data []byte, perm os.FileMode) error {
	if f.writeErr != nil {
		return f.writeErr
	}
	f.writes[name] = append([]byte(nil), data...)
	f.perms[name] = perm
	return nil
}

// fakeDB 是 DB 的内存替身。
type fakeDB struct {
	version int64
	err     error
	closed  bool
}

func (d *fakeDB) SchemaVersion(context.Context) (int64, error) { return d.version, d.err }

func (d *fakeDB) Close() { d.closed = true }

// fixture 是一次自检的固定输入。
type fixture struct {
	fs         *fakeFS
	deps       Deps
	opts       Options
	db         *fakeDB
	embedded   int64
	owners     map[string]fsperm.Owner
	identities map[string]fsperm.Identity
	// currentIdentity 覆盖「调用者」身份；nil = root。
	currentIdentity *fsperm.Identity
}

// newFixture 构造全通过的基线：目录齐、权限按服务用户可读可写、工具齐、库连得上、服务 active。
func newFixture() *fixture {
	fs := newFakeFS()
	fs.mkdir("/srv/ui", "index.html", "assets")
	fs.mkdir("/var/lib/yiban")
	fs.mkdir("/var/lib/yiban/photos")
	fs.mkdir("/var/lib/yiban/pre-migrate")
	db := &fakeDB{version: 22}
	f := &fixture{
		fs: fs,
		db: db,
		opts: Options{
			DSN:           testDSN,
			HTTPAddr:      "127.0.0.1:47183",
			UIDir:         "/srv/ui",
			PhotoDir:      "/var/lib/yiban/photos",
			CredentialKey: testCredentialKey,
			HashPepper:    testPepper,
			BackupDir:     "/var/lib/yiban/pre-migrate",
			StatePath:     "/var/lib/yiban/update-state.json",
			Unit:          "yiban",
			ServiceUser:   "yiban",
		},
		embedded: 22,
		owners: map[string]fsperm.Owner{
			"/srv/ui":                    {OwnerName: "yiban", GroupName: "yiban", Mode: 0o755},
			"/var/lib/yiban":             {OwnerName: "yiban", GroupName: "yiban", Mode: 0o755},
			"/var/lib/yiban/photos":      {OwnerName: "yiban", GroupName: "yiban", Mode: 0o750},
			"/var/lib/yiban/pre-migrate": {OwnerName: "yiban", GroupName: "yiban", Mode: 0o700},
		},
		identities: map[string]fsperm.Identity{
			"yiban": {Name: "yiban", GroupNames: []string{"yiban"}},
		},
	}
	f.deps = Deps{
		Now: func() time.Time { return fixedNow },
		FS:  fs,
		Build: BuildInfo{
			Version: "0.7.0", Commit: "abc1234", BuildTime: "2026-11-04T10:00:00Z",
		},
		LookPath:        func(name string) (string, error) { return "/usr/bin/" + name, nil },
		OpenDB:          func(context.Context, string) (DB, error) { return db, nil },
		EmbeddedVersion: func() (int64, error) { return f.embedded, nil },
		ServiceProbe:    func(context.Context) (bool, bool, string) { return true, true, "active" },
		StatPermission: func(path string) (fsperm.Owner, bool) {
			owner, ok := f.owners[path]
			return owner, ok
		},
		// 备份目录按调用者（root）判权：生产里 selfcheck 以 root 跑。
		CurrentIdentity: func() (fsperm.Identity, bool) {
			if f.currentIdentity == nil {
				return fsperm.Identity{UID: 0, GID: 0, Name: "root", HasIDs: true}, true
			}
			return *f.currentIdentity, true
		},
		UserIdentity: func(name string) (fsperm.Identity, bool) {
			id, ok := f.identities[name]
			return id, ok
		},
	}
	return f
}

// run 跑一次自检。
func (f *fixture) run(t *testing.T) Report {
	t.Helper()
	rep, err := Run(context.Background(), f.deps, f.opts)
	if err != nil {
		t.Fatalf("Run: %v", err)
	}
	return rep
}

// byName 找一条检查结果。
func byName(t *testing.T, rep Report, name string) Check {
	t.Helper()
	for _, c := range rep.Checks {
		if c.Name == name {
			return c
		}
	}
	t.Fatalf("check %q not found in %v", name, checkNames(rep))
	return Check{}
}

// checkNames 返回检查名序列。
func checkNames(rep Report) []string {
	names := make([]string, 0, len(rep.Checks))
	for _, c := range rep.Checks {
		names = append(names, c.Name)
	}
	return names
}
