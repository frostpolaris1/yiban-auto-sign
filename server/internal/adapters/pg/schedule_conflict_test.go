package pg_test

// 认领冲突哨兵的错误码门：
// 适配器返回裸 errors.New 时，worker 的结算拒绝日志永远不会触发
// （coreerrs.From 对非 *coreerrs.Error 返回 internal_error）。
// 本用例不连库，只钉住哨兵携带 CodeConflict。

import (
	"testing"

	"github.com/frostpolaris1/yiban-auto-sign/server/internal/adapters/pg"
	coreerrs "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/errs"
)

func TestScheduleConflictCarriesConflictCode(t *testing.T) {
	if got := coreerrs.From(pg.ErrScheduleConflict).Code; got != coreerrs.CodeConflict {
		t.Fatalf("ErrScheduleConflict code = %q, want conflict", got)
	}
}
