// Package conformance 检查一个提供方是否兑现契约 v1 的承诺。
//
// # 功能
// 接收一个提供方构造器，逐条执行判据。
// 每条判据独立构造提供方，判据之间不共享状态。
// 失败文本统一为 `conformance: <名称>: <原因>`。
// 本包**不**连真实平台、**不**读配置、**不**写库。
//
// # 归属
// signing 层的判据集。新 provider 接入前先跑本套件。
// 参考实现是 signing/fake 包；本包不依赖它。
//
// # 复用
// Run、Checks、Check、NewProvider、Expecter。
//
// # 口径
// 判据只看提供方的可观察行为，不看内部实现。
// 判据把「幂等」解释为：同一 AttemptID 重复签到得到同一结果。
// 判据把「无凭据」解释为：身份与结果不含凭据原值，也不含明文手机号。
// 并发判据只在 `-race` 构建下检验数据竞争。
// 结果串扰只在提供方实现 Expecter 时可精确判定。
// 提供方在判据内 panic 时，本包把它转成同格式失败，不中断整轮。
//
// # 判据清单与依据
//
// provider.constructible：构造器返回非 nil、非类型化 nil。
// 依据：宿主不得拿到空接口后崩溃。
//
// id.nonempty：ID() 去掉空白后非空。
// 依据：宿主按 ID 保存每个提供方的配置与限速状态。
//
// id.stable：ID() 多次调用相同。
// 依据：ID 是粘性键，变化会让既有配置失配。
//
// capabilities.stable：Capabilities() 多次调用相同。
// 依据：宿主按能力决定 UI 与调度；运行期漂移会让两者不一致。
//
// capabilities.honored：未声明的发现能力必须返回错误。
// 依据：契约只定义宿主行为；套件要求静默零值不得冒充发现结果（缺口 G1）。
//
// window.shape：窗口起止非零，结束晚于开始，来源在契约词表内。
// 依据：契约 Window 与 WindowSource 的定义。
//
// window.cancel：已取消上下文返回 context.Canceled。
// 依据：Go 惯例；宿主靠它区分取消与上游失败。
//
// targets.shape：每个目标 ID 非空，经纬度在物理范围内。
// 依据：契约 Target 与 Point 的定义；宿主按 ID 去重与记录。
//
// targets.cancel：已取消上下文返回 context.Canceled。依据同上。
//
// verify.result：用测试凭据调用 Verify 返回 nil 错误。
// 依据：契约 §7.3 要求 conformance 不连真实平台；
// 判据对象是参考实现，正常路径不得失败。
//
// sign.result：每次 Sign 返回 nil 错误与词表内结果。
// 依据：契约 Outcome 词表；错误只用于传输与上下文问题。
//
// pii.absent：身份与签到结果不含凭据原值与明文手机号。
// 依据：契约硬边界 1 与 Evidence 脱敏要求。
//
// sign.retry：重试分级与契约结果表一致。
// transient 必须可重试且预算至少 1。
// challenge 可重试时预算至少 1。
// 其余结果一律不可自动重试。
//
// sign.unknown：unknown 永不自动重试。
// 依据：契约结果表注明「否，人工确认」。
//
// sign.cancel：已取消上下文返回 context.Canceled，且不报告 success。
// 依据：取消不是成功；宿主不得把取消记成已签到。
//
// sign.idempotent：同一 AttemptID 重复签到得到同一结果。
// 依据：契约 §3 身份与幂等；提供方必须幂等。
//
// sign.concurrent：8 个并发 Sign 在 10 秒内返回，结果均在词表内。
// 实现 Expecter 时，再逐条比对每个请求的期望结果。
// 依据：契约要求提供方可被宿主并发调用。
//
// # 重试分级表
//
//	success             不可重试
//	already_done        不可重试
//	pending_upstream    不可重试
//	no_target           不可重试
//	invalid_target      不可重试
//	credential_invalid  不可重试
//	device_unbound      不可重试
//	challenge           可重试，预算有界
//	transient           可重试
//	rejected            不可重试
//	photo_missing       不可重试
//	photo_rejected      不可重试
//	unknown             不可重试，人工确认
//
// # 契约缺口
//
// G1：契约未定义未声明能力时方法的返回形状。
// 套件要求返回错误。
// 理由：静默零值会被宿主当成一次成功的发现。
//
// G2：契约未给 Result 与 AttemptID 的关联字段。
// 串扰判据因此依赖可选接口 Expecter。
// 未实现 Expecter 的提供方只能被判据覆盖词表合法性与数据竞争。
//
// G3：套件无法强制提供方返回某个结果。
// sign.retry 与 sign.unknown 只覆盖该提供方实际返回的结果。
// 判据要求提供方本身覆盖全部 13 项，才可证明分级完整。
//
// G4：数据竞争只在 `-race` 构建下可见。
// 本包不启用竞争检测；验收命令必须带 `-race`。
package conformance
