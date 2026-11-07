package worker

// 手动签到多候选任务的随机选点。
//
// # 口径
// 同一打卡的多个点位等价，任取其一即可：旧线 signPosition 对 Position
// 数组先随机打乱、任一任务成功即停；自动执行 ClaimDue 也只按 run_at 取一条。
// 手动签到不再以「任务不唯一」拒绝，而是随机选一个候选入队。
//
// # 实现
// crypto/rand 取均匀下标，避免 math/rand 的全局可预测序列。
// 取随机数失败时退回首个候选：多选一的等价任务里，这仍是一个合法成员，
// 不该让一次随机数故障挡住用户签到。

import (
	"crypto/rand"
	"math/big"
)

// randomTaskPicker 是生产缺省实现。
type randomTaskPicker struct{}

// NewRandomTaskPicker 返回均匀随机选点的生产实现。
func NewRandomTaskPicker() TaskPicker { return randomTaskPicker{} }

func (randomTaskPicker) Pick(candidates []string) string {
	n := len(candidates)
	if n == 0 {
		return ""
	}
	if n == 1 {
		return candidates[0]
	}
	idx, err := rand.Int(rand.Reader, big.NewInt(int64(n)))
	if err != nil {
		return candidates[0]
	}
	return candidates[idx.Int64()]
}
