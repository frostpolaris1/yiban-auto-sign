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
// 判据把「幂等」解释为：同一 AttemptID 两次得到完全相同的 Result。
// 判据把「无 PII」解释为：身份与结果不含凭据原值，
// 且 core/mask 判定的手机号与邮箱已脱敏。
// 手机号判定只有 core/mask 一份，且用**检测口径** TextHasPhone：
// 宁缺勿冤——11 至 21 位的整段数字只认对齐窗口，秒/毫秒/微秒/纳秒时间戳不判；
// 分隔与带国家码形态仍被抓；22 位以上的连续数字段按滑动窗口抓内嵌号码。
// Expecter 是**必选**接口：不实现者不通过认证（G2）。
// 并发判据只在 `-race` 构建下检验数据竞争。
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
// capabilities.honored：未声明的发现能力必须返回**非 nil 错误**。
// 依据：契约 G1 裁决；静默零值会被宿主当成一次成功的发现。
//
// outcome.error：发现与校验返回的 OutcomeError 结论必须在词表内，且不得为 success。
// Message 不得含凭据原值、明文手机号或邮箱；口径同 pii.absent 的检测口径。
// 检测口径的限制：下划线分隔、全角数字、编码形态与截断后缀不在判定内；
// 原值与 mask 覆盖的数字/邮箱形态会被抓。
// 取消与网络错误不是平台结论，不在此判据内。
// 依据：宿主用 errors.As 取回结论；无效词会让宿主静默走错分支。
//
// window.shape：窗口起止非零，结束晚于开始，来源在契约词表内。
// 提供方返回合法的非 success 结论错误时显性跳过：没有窗口可查。
// 依据：契约 Window 与 WindowSource 的定义。
//
// window.cancel：声明 WindowDiscovery 时，已取消上下文返回 context.Canceled。
// 未声明时本判据不适用（显性跳过）：该方法本就不该被调用（G1）。
// 依据：Go 惯例；宿主靠它区分取消与上游失败。
//
// targets.shape：每个目标 ID 非空，经纬度在物理范围内。
// 提供方返回合法的非 success 结论错误时显性跳过：没有目标可查。
// 依据：契约 Target 与 Point 的定义；宿主按 ID 去重与记录。
//
// targets.cancel：口径同 window.cancel。
//
// verify.result：用测试凭据调用 Verify 返回 nil 错误。
// 提供方返回合法的非 success 结论错误时显性跳过：没有档案可查。
// 依据：契约 §7.3 要求 conformance 不连真实平台；
// 判据对象是参考实现，正常路径不得失败。
//
// sign.result：每次 Sign 返回 nil 错误与词表内结果。
// 依据：契约 Outcome 词表；错误只用于传输与上下文问题。
//
// sign.photo_missing：声明 ImageRequired 时，无图请求返回 photo_missing。
// 未声明 ImageRequired 时跳过，并在标准输出登记：**跳过不是通过**。
// 依据：契约 §4.5「无图但需要图 → photo_missing，不登录」。
//
// pii.absent：身份与签到结果不含凭据原值，也不含明文手机号或邮箱。
// 判定复用 core/mask 的检测口径（TextHasPhone/TextHasEmail）：
// 分隔与带国家码形态被抓；时间戳不被冤；
// 22 位以上的连续数字段按滑动窗口抓内嵌号码（12 至 21 位不判，已登记）。
// Evidence.Raw 无法 JSON 序列化时判红。这是**有意收紧**：
// 证明不了已脱敏，就不算通过。
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
// sign.cancel：已取消上下文返回 context.Canceled，且不返回伪结果（Outcome 为空）。
// 依据：取消不是成功；宿主不得把取消记成已签到。
//
// sign.timeout：期限已过时返回上下文错误，且不返回伪结果。
// 调用中被取消时，提供方在 20ms 窗口后才返回的，错误必须是上下文错误。
// 两个场景的单次调用都以 5 秒为完成上限；此外每条判据都有 15 秒的
// 整体上限：任意调用都阻塞的提供方同样判红，套件不挂起（W2C-R2-01、R3-04）。
// 依据：契约 §7.2 把「超时」列为 conformance 必测项。
//
// sign.idempotent：同一 AttemptID 两次得到完全相同的 Result。
// 依据：契约 §3 身份与幂等；只比 Outcome 会放过证据与重试建议的漂移。
//
// sign.concurrent：8 个并发 Sign 在 10 秒内返回，结果均在词表内。
// 提供方必须实现 Expecter，再逐条比对每个请求的期望结果。
// 依据：契约要求提供方可被宿主并发调用；无 AttemptID 关联就无法证明不串扰。
//
// 未声明能力时，对应判据返回 errNotApplicable；Run 在标准输出打印跳过行。
// 跳过行需 `go test -v` 才可见：verify.sh 以 -v 跑本包，跳过不会静默。
// 跳过不是通过。
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
//	blocked             不可重试（风控拦截，重发只会加深标记）
//	transient           可重试
//	rejected            不可重试
//	photo_missing       不可重试
//	photo_rejected      不可重试
//	unknown             不可重试，人工确认
//
// # 契约缺口
//
// G1：契约未定义未声明能力时方法的返回形状。
// 处置：契约 v1 补「未声明能力的发现方法必须返回非 nil 错误，宿主不得调用」。
// 套件据此要求非 nil 错误；cancel 判据在未声明能力时不再要求 context.Canceled。
//
// G2：契约未给 Result 与 AttemptID 的关联字段。
// 处置：Expecter 是认证必选接口；未实现者在 sign.concurrent 报红。
//
// G3：套件无法强制提供方返回某个结果。
// sign.retry 与 sign.unknown 只覆盖该提供方实际返回的结果。
// **认证要求**：提供方自己的测试必须覆盖全部 14 项结果的分级。
//
// G4：数据竞争只在 `-race` 构建下可见。
// 本包不启用竞争检测；验收命令必须带 `-race`。
package conformance
