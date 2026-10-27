package assembly

// 嵌入 IANA 时区库。
//
// 容器镜像（distroless）与精简宿主可能没有 /usr/share/zoneinfo；
// 缺它时 clock.LoadLocation 只在启动或排期时才失败，属于静默延迟暴露。
// time/tzdata 是兜底实现：系统库存在时优先系统库，缺失时用嵌入数据，
// 两种宿主的时区解析结果一致（代价约 450 KiB 二进制体积）。
import _ "time/tzdata"
