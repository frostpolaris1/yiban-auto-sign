package pg_test

// 00031 存量默认迁移的回归 e2e（审查 M-1）。
//
// # 判据
// 前滚到 00030 后种三种存量形状，再跑 00031：
//  ① 默认指针 → 未软删用户行：复制登记本校资产行并改道（backfill 正路）；
//  ② 默认指针 → 已软删用户行（悬引用）：指针置 NULL——属主之后重传同字节
//     恢复源行时，旧默认**不得**复活；
//  ③ 无指针：保持 NULL。
// 读面用真仓储复核：只认 school-owned 资产行；即便有人把指针手写回用户行
// （纵深防御靶），读面也按未设置回。
// 最后重复前滚 00031 幂等：资产行不重复、指针不动。
//
// # 归属
// 真库 e2e（package pg_test）。复用 migrationDownEnv 的独立 schema 与
// goose 前滚面；读面经 pg.PhotoRepo（与产品同一 SQL）。

import (
	"strings"
	"testing"
	"time"

	"github.com/frostpolaris1/yiban-auto-sign/server/internal/adapters/pg"
	corescope "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/scope"
)

func TestMigration00031DanglingDefaultClearedE2E(t *testing.T) {
	m := newMigrationDownEnv(t)
	m.upTo(30)

	userID := m.scalarString(`INSERT INTO users (email, password_hash)
		VALUES ('mig31-owner@example.invalid', 'argon2id$test') RETURNING id::text`)
	schoolLive := m.scalarString(`INSERT INTO schools (code, name)
		VALUES ('mig31-a', 'Mig 31 A') RETURNING id::text`)
	schoolDangling := m.scalarString(`INSERT INTO schools (code, name)
		VALUES ('mig31-b', 'Mig 31 B') RETURNING id::text`)
	schoolUserPointer := m.scalarString(`INSERT INTO schools (code, name)
		VALUES ('mig31-c', 'Mig 31 C') RETURNING id::text`)

	shaLive := strings.Repeat("ab", 32)
	shaDead := strings.Repeat("cd", 32)
	livePhoto := m.scalarString(`INSERT INTO photos (owner_user_id, sha256, mime, width, height, bytes)
		VALUES ($1::uuid, decode($2, 'hex'), 'image/png', 8, 8, 10) RETURNING id::text`, userID, shaLive)
	deadPhoto := m.scalarString(`INSERT INTO photos (owner_user_id, sha256, mime, width, height, bytes, deleted_at)
		VALUES ($1::uuid, decode($2, 'hex'), 'image/png', 8, 8, 10, now()) RETURNING id::text`, userID, shaDead)
	m.exec(`UPDATE schools SET default_photo_id = $2::uuid WHERE id = $1::uuid`, schoolLive, livePhoto)
	m.exec(`UPDATE schools SET default_photo_id = $2::uuid WHERE id = $1::uuid`, schoolDangling, deadPhoto)

	// 跑 00031（backfill + 收尾清悬）。
	m.upTo(31)

	// ① live 源：指针改道到本校资产行（owner NULL、school 挂、同 sha）。
	if got := m.scalarString(`
		SELECT coalesce(p.owner_user_id::text, '') || '|' || coalesce(p.school_id::text, '')
		  FROM schools s JOIN photos p ON p.id = s.default_photo_id
		 WHERE s.id = $1::uuid`, schoolLive); got != "|"+schoolLive {
		t.Fatalf("live 源改道 = %q, want owner NULL + school %s", got, schoolLive)
	}
	// ② 悬引用：指针在迁移收尾被置 NULL。
	if got := m.scalarString(
		`SELECT coalesce(default_photo_id::text, '') FROM schools WHERE id = $1::uuid`,
		schoolDangling); got != "" {
		t.Fatalf("悬引用指针 = %q, want NULL（迁移收尾清悬）", got)
	}

	ctx := m.ctx
	repoDB, err := pg.Open(ctx, pg.Options{DSN: m.dsn, MaxConns: 2, StatementTimeout: 30 * time.Second})
	if err != nil {
		t.Fatalf("open repo DB: %v", err)
	}
	t.Cleanup(repoDB.Close)
	repo := pg.NewPhotoRepo(repoDB)
	read := func(schoolID string) (string, string) {
		t.Helper()
		id, etag, err := repo.SchoolDefault(ctx, corescope.Master(), schoolID)
		if err != nil {
			t.Fatalf("read default %s: %v", schoolID, err)
		}
		return id, etag
	}

	// ① 读面：live 源学校命中资产行，etag 与源字节一致。
	assetID, etag := read(schoolLive)
	if assetID == "" || assetID == livePhoto || etag != shaLive {
		t.Fatalf("live 源默认 = (%q,%q), want 资产行（≠源 %s）与 sha %s", assetID, etag, livePhoto, shaLive)
	}
	// ② 读面：悬引用学校无默认。
	if id, etag := read(schoolDangling); id != "" || etag != "" {
		t.Fatalf("悬引用读面 = (%q,%q), want 未设置", id, etag)
	}
	// ③ 无指针学校：未设置。
	if id, etag := read(schoolUserPointer); id != "" || etag != "" {
		t.Fatalf("无指针读面 = (%q,%q), want 未设置", id, etag)
	}

	// M-1 核心谓词：属主重传同字节恢复源行（Create 的恢复语义=deleted_at 清空），
	// 旧默认**不复活**——迁移已清悬，指针不在了。
	m.exec(`UPDATE photos SET deleted_at = NULL WHERE id = $1::uuid`, deadPhoto)
	if id, etag := read(schoolDangling); id != "" || etag != "" {
		t.Fatalf("重传复活了旧默认: (%q,%q)", id, etag)
	}
	if got := m.scalarString(
		`SELECT coalesce(default_photo_id::text, '') FROM schools WHERE id = $1::uuid`,
		schoolDangling); got != "" {
		t.Fatalf("重传后悬引用学生指针 = %q, want 仍 NULL", got)
	}

	// 纵深防御：手工把指针写回用户行（模拟未来任何写面 bug/旁路），
	// 读面 join 谓词必须让它永不生效。
	m.exec(`UPDATE schools SET default_photo_id = $2::uuid WHERE id = $1::uuid`, schoolUserPointer, deadPhoto)
	if id, etag := read(schoolUserPointer); id != "" || etag != "" {
		t.Fatalf("用户行指针在读面生效: (%q,%q), want 未设置（join 防御）", id, etag)
	}

	// 幂等：重复前滚 00031 无副作用，资产行仍恰一条。
	m.upTo(31)
	if n := m.scalarInt(`SELECT count(*) FROM photos WHERE school_id = $1::uuid AND owner_user_id IS NULL`, schoolLive); n != 1 {
		t.Fatalf("重复前滚后资产行 = %d, want 1", n)
	}
}
