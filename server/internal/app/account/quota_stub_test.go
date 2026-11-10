package account

import (
	"context"
	"testing"
)

// fixedQuota 是测试用固定配额替身：0=不限，正数=上限。
// 生产实现是 app/settings.Resolver（DB 覆盖链）；本包测试只关心
// 用例把读到的值原样交给仓储。
type fixedQuota int

func (q fixedQuota) UserLimit(context.Context) (int, error)    { return q.value() }
func (q fixedQuota) AccountLimit(context.Context) (int, error) { return q.value() }

// value 是唯一的取值口径：负数不是合法配额，读取即 panic。
// 替身只服务测试；把非法值带进用例必须立刻炸，不得静默当成上限。
func (q fixedQuota) value() (int, error) {
	if q < 0 {
		panic("fixedQuota: negative limit is not a legal quota")
	}
	return int(q), nil
}

// TestFixedQuotaRejectsNegative 钉住替身自身：负配额读取即 panic。
func TestFixedQuotaRejectsNegative(t *testing.T) {
	defer func() {
		if recover() == nil {
			t.Fatal("negative fixedQuota must panic")
		}
	}()
	_, _ = fixedQuota(-1).AccountLimit(context.Background())
}
