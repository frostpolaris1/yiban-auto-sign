// audit 子命令：把当前审计链头追加到库外锚点文件。
//
// # 功能
// `yiban-cli audit anchor` 读取 audit_logs 的链头（最后一行 id、截至该行的
// 行数与存储哈希），追加一行到 YIBAN_AUDIT_ANCHOR_FILE（append-only，0600），
// 并把文件里的最强锚点（seq/head_hash/整行字节哈希）按棘轮写进 anchor_meta
// （seq 只进不退）。巡检端点 GET /api/admin/audit-status 在配置了同一路径时
// 比对锚点与高水位：删前缀、截尾、整表清空、链尾重签、锚点文件末行被删与
// 文件行改写（含 count 改写）都会翻红。
//
// # 归属
// cmd/cli 薄装配层。文件格式与读写住在 internal/adapters/anchor；
// 链头读取住在 adapters/pg.AuditRepo.Head。
//
// # 口径
// 退出码：0 写入成功或幂等跳过；1 运行失败（连库/读链头/写文件）或锚点门
// 拦截（anchor_truncated / anchor_rewritten）；2 用法错误。
// 链头未前进（seq/head_hash 与 anchor_meta 相同；count 比对文件最强行，
// 最强行字节又钉回 meta.LineHash——时间戳不参与身份）时幂等跳过：不追加
// 文件、不写 meta，stdout 一句 already anchored——每日 timer 在安静日不再
// 产生"同 seq 新时间戳"噪音行，Latest 不会漂出 meta 行造成误报。
// 一致性门在追加**之前**跑：失败不往文件里留噪音行。
// --reseed 显式重播种（运维恢复动作）：忽略"meta 记录行在文件中缺失"检查，
// 以当前链头追加新行并无条件覆盖 anchor_meta；见 deploy/README.md §6.1 第 3 步。
// DSN 与 pepper 只从环境读（YIBAN_DATABASE_URL / YIBAN_HASH_PEPPER）：
// 没有命令行参数，避免秘密进 ps/argv 与 shell 历史。
// --file 只覆盖非敏感路径（YIBAN_AUDIT_ANCHOR_FILE）。
// 审计链为空时拒绝写行：空字段行会被后续解析整体错位，宁缺毋滥。
// 本命令只追加锚点，不改库、不校验链、不清理。
package main

import (
	"bytes"
	"context"
	"flag"
	"fmt"
	"io"
	"strings"
	"time"

	"github.com/frostpolaris1/yiban-auto-sign/server/internal/adapters/anchor"
	"github.com/frostpolaris1/yiban-auto-sign/server/internal/adapters/config"
	"github.com/frostpolaris1/yiban-auto-sign/server/internal/adapters/pg"
	coreaudit "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/audit"
	coreclock "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/clock"
)

// runAudit 处理 audit 子命令。当前只有 anchor。
func runAudit(args []string, stdout, stderr io.Writer) int {
	if len(args) == 0 || args[0] != "anchor" {
		_, _ = fmt.Fprintln(stderr, `用法: yiban-cli audit anchor [--file <路径>] [--reseed]
把当前审计链头追加到库外锚点文件（append-only，0600）。
DSN 与 pepper 只从环境读：YIBAN_DATABASE_URL / YIBAN_HASH_PEPPER。
路径缺省取 YIBAN_AUDIT_ANCHOR_FILE；未配置即用法错误。
建议由 systemd timer 每日跑一次（见 deploy/README.md §6.1）。
链头未前进时重复运行幂等：不追加、不写 meta，stdout 一句 already anchored。
恢复场景（合法回滚/整表清理）用 --reseed 显式重播种。`)
		return 2
	}
	fs := flag.NewFlagSet("audit anchor", flag.ContinueOnError)
	fs.SetOutput(stderr)
	file := fs.String("file", "", "覆盖 YIBAN_AUDIT_ANCHOR_FILE（非敏感路径）")
	reseed := fs.Bool("reseed", false, "显式重播种（运维动作）：忽略 anchor_meta 行缺失检查，"+
		"以当前链头追加新行并无条件覆盖 anchor_meta；仅用于合法回滚/整表清理后的恢复，日常 timer 禁用")
	if err := fs.Parse(args[1:]); err != nil {
		return 2
	}
	url := strings.TrimSpace(envValue("YIBAN_DATABASE_URL"))
	if url == "" {
		_, _ = fmt.Fprintln(stderr, "audit anchor: YIBAN_DATABASE_URL 未设置")
		return 2
	}
	pep := strings.TrimSpace(envValue("YIBAN_HASH_PEPPER"))
	if len(pep) < 16 {
		_, _ = fmt.Fprintln(stderr, "audit anchor: YIBAN_HASH_PEPPER 未设置或不足 16 字符（审计链钥由它派生）")
		return 2
	}
	path := strings.TrimSpace(*file)
	if path == "" {
		path = strings.TrimSpace(envValue("YIBAN_AUDIT_ANCHOR_FILE"))
	}
	if path == "" {
		_, _ = fmt.Fprintln(stderr, "audit anchor: YIBAN_AUDIT_ANCHOR_FILE 未设置（或用 --file 传入）")
		return 2
	}

	ctx, cancel := context.WithTimeout(context.Background(), 30*time.Second)
	defer cancel()
	db, err := pg.Open(ctx, pg.Options{DSN: url, MaxConns: 1, StatementTimeout: 10 * time.Second})
	if err != nil {
		_, _ = fmt.Fprintf(stderr, "audit anchor: %v\n", err)
		return 1
	}
	defer db.Close()
	key, err := coreaudit.DeriveKey([]byte(pep))
	if err != nil {
		_, _ = fmt.Fprintf(stderr, "audit anchor: %v\n", err)
		return 2
	}
	audits, err := pg.NewAuditRepo(db, key)
	if err != nil {
		_, _ = fmt.Fprintf(stderr, "audit anchor: %v\n", err)
		return 1
	}
	head, ok, err := audits.Head(ctx)
	if err != nil {
		_, _ = fmt.Fprintf(stderr, "audit anchor: %v\n", err)
		return 1
	}
	if !ok {
		_, _ = fmt.Fprintln(stderr, "audit anchor: 审计链为空，拒绝写锚点行")
		return 1
	}
	anchorFile := anchor.NewFile(path)
	if *reseed {
		// 显式重播种（运维动作，见 deploy/README.md §6.1 第 3 步）：忽略
		// "meta 记录行在文件中缺失"检查，以当前链头追加新行并无条件覆盖
		// anchor_meta。信任假设＝人工确认过的恢复——合法回滚/整表清理后
		// 旧高水位指向已不存在的链头，棘轮会永远挡住新播种。日常 timer
		// 严禁带这个 flag：它等于放弃截断/改写检测。
		_, _ = fmt.Fprintln(stderr,
			"audit anchor: --reseed 显式重播种（运维动作）：忽略 anchor_meta 行缺失检查，以当前链头追加并无条件覆盖 anchor_meta")
	}
	meta, err := audits.AnchorMeta(ctx)
	if err != nil {
		_, _ = fmt.Fprintf(stderr, "audit anchor: 读 anchor_meta 失败: %v\n", err)
		return 1
	}
	// 一致性门全部在追加**之前**跑（--reseed 除外）：失败时不得往锚点文件
	// 里留下噪音行。旧顺序"先追加后判"在截空攻击后永不自愈——文件里永远
	// 缺 meta 记录的那一行，巡检在 truncated/unrecorded/rewritten 间循环。
	//   1. meta 记录的那一行必须还在文件里（按整行字节哈希找）；
	//      缺失即文件被截断（含截空后重跑），判 anchor_truncated；
	//   2. 链头未前进（seq/head_hash 对 meta，count 对文件最强行，时间戳
	//      不参与）时幂等跳过：不追加、不写 meta，exit 0；但文件最强行与
	//      链头或 meta 行字节不符仍是 anchor_rewritten，失败关闭；
	//   3. 链头已前进时文件最强锚点不得低于 meta；同 seq 但行字节不同即被改写。
	if !*reseed && meta != nil {
		present, err := anchorFile.ContainsLineHash(ctx, meta.LineHash)
		if err != nil {
			_, _ = fmt.Fprintf(stderr, "audit anchor: %v\n", err)
			return 1
		}
		if !present {
			_, _ = fmt.Fprintf(stderr,
				"audit anchor: anchor_truncated——anchor_meta 记录的行（seq=%d）在文件中缺失，不写文件也不写 meta；"+
					"确认是合法恢复后用 --reseed 重播种（见 deploy/README.md §6.1 第 3 步）\n", meta.Seq)
			return 1
		}
		strongest, err := anchorFile.Latest(ctx)
		if err != nil {
			_, _ = fmt.Fprintf(stderr, "audit anchor: 读最强锚点失败: %v\n", err)
			return 1
		}
		if head.Seq == meta.Seq && bytes.Equal(head.Hash, meta.HeadHash) {
			// 链头未前进。meta 不存 count：count 逐字段比走文件最强行，
			// 最强行字节再钉回 meta.LineHash——三处全等才跳过。任一不符
			// （同 seq 不同字节、前缀删除改变了 count）失败关闭。
			if strongest.Seq != head.Seq || strongest.Count != head.Count ||
				!bytes.Equal(strongest.Hash, head.Hash) || !bytes.Equal(strongest.LineHash, meta.LineHash) {
				_, _ = fmt.Fprintf(stderr,
					"audit anchor: anchor_rewritten——文件最强行（seq=%d）与本次链头或 anchor_meta 记录的行不符，拒绝幂等跳过\n",
					strongest.Seq)
				return 1
			}
			_, _ = fmt.Fprintf(stdout,
				"audit anchor: already anchored（seq=%d count=%d 链头未前进，不追加）\n", head.Seq, head.Count)
			return 0
		}
		if strongest.Seq < meta.Seq {
			_, _ = fmt.Fprintf(stderr,
				"audit anchor: anchor_truncated——文件最强 seq=%d 低于 anchor_meta 的 %d，拒绝写 meta\n",
				strongest.Seq, meta.Seq)
			return 1
		}
		if strongest.Seq == meta.Seq && !bytes.Equal(strongest.LineHash, meta.LineHash) {
			_, _ = fmt.Fprintf(stderr,
				"audit anchor: anchor_rewritten——文件最强行（seq=%d）与 anchor_meta 记录的行字节不符，拒绝写 meta\n",
				strongest.Seq)
			return 1
		}
	}
	if err := anchorFile.Append(ctx, head, coreclock.System{}.Now()); err != nil {
		_, _ = fmt.Fprintf(stderr, "audit anchor: %v\n", err)
		return 1
	}
	// 文件追加成功后先读回末行，确认刚写的链头确实落在文件末尾。
	last, err := anchorFile.Last(ctx)
	if err != nil {
		_, _ = fmt.Fprintf(stderr, "audit anchor: 读回锚点末行失败: %v\n", err)
		return 1
	}
	if last.Seq != head.Seq || last.Count != head.Count || !bytes.Equal(last.Hash, head.Hash) {
		_, _ = fmt.Fprintf(stderr, "audit anchor: 锚点末行与本次链头不一致（seq=%d/%d）\n", last.Seq, head.Seq)
		return 1
	}
	// 高水位取文件里的最强锚点：回滚后新链头可能小于旧锚点，
	// 此时 meta 必须停在旧高水位，不能倒退（--reseed 例外：显式覆盖）。
	strongest, err := anchorFile.Latest(ctx)
	if err != nil {
		_, _ = fmt.Fprintf(stderr, "audit anchor: 读回最强锚点失败: %v\n", err)
		return 1
	}
	if strongest.Seq < head.Seq {
		_, _ = fmt.Fprintf(stderr, "audit anchor: 最强锚点 seq=%d 小于本次链头 %d\n", strongest.Seq, head.Seq)
		return 1
	}
	// 锚点文件与库内高水位两处一致才算锚定成功；写表失败即回报错。
	// 正常路径走 RecordAnchorMeta 棘轮（seq 只进不退，同 seq 还要求行哈希
	// 相同，倒退与平行改写都报错）；--reseed 走 OverwriteAnchorMeta，
	// 无条件覆盖——误用等于放弃检测，只允许人工恢复时用。
	writeErr := db.InTx(ctx, func(ctx context.Context) error {
		anchorMeta := coreaudit.AnchorMeta{
			Seq: strongest.Seq, HeadHash: strongest.Hash, LineHash: strongest.LineHash,
		}
		if *reseed {
			return audits.OverwriteAnchorMeta(ctx, anchorMeta)
		}
		return audits.RecordAnchorMeta(ctx, anchorMeta)
	})
	if writeErr != nil {
		_, _ = fmt.Fprintf(stderr, "audit anchor: 写 anchor_meta 失败: %v\n", writeErr)
		return 1
	}
	mode := ""
	if *reseed {
		mode = " reseed"
	}
	_, _ = fmt.Fprintf(stdout, "audit anchor:%s seq=%d count=%d hash=%x meta_seq=%d file=%s\n",
		mode, head.Seq, head.Count, head.Hash, strongest.Seq, path)
	return 0
}

// envValue 读取进程环境。环境读点仍收敛在 adapters/config。
func envValue(key string) string {
	v, _ := config.Env{}.Lookup(key)
	return v
}
