package events

// SSE 端点：GET /api/admin/events。
//
// 帧格式与判据见包 docstring。心跳 15s、空闲 60s 关闭，
// 都由注入的时钟与定时器驱动，可测。订阅 ctx 取消即退订；
// 请求取消即断流。admin 的院校过滤在总线侧完成。

import (
	"encoding/json"
	"errors"
	"fmt"
	"net/http"
	"sort"
	"time"

	"github.com/gin-gonic/gin"

	appevents "github.com/frostpolaris1/yiban-auto-sign/server/internal/app/events"
	"github.com/frostpolaris1/yiban-auto-sign/server/internal/core/errs"
	corescope "github.com/frostpolaris1/yiban-auto-sign/server/internal/core/scope"
	"github.com/frostpolaris1/yiban-auto-sign/server/internal/entry/http/oapi"
)

// frameData 是 data 行的 JSON 形状（设计稿信封）。
// 零值字段省略：heartbeat/end 的 data 都是 {}。
type frameData struct {
	SchoolID string         `json:"school_id,omitempty"`
	TaskID   string         `json:"task_id,omitempty"`
	Day      string         `json:"day,omitempty"`
	At       string         `json:"at,omitempty"` // RFC3339；零时刻省略
	Payload  map[string]any `json:"payload,omitempty"`
}

// resyncData 是 resync 帧的 data：客户端落后的位置。
type resyncData struct {
	From uint64 `json:"from"`
}

// errStreamingUnsupported 是写入器不支持流式的哨兵。
var errStreamingUnsupported = errors.New("events: streaming writer unsupported")

// AdminEvents 实现 GET /api/admin/events（master/admin）。
// master 订阅全部院校（schools 传 nil）；admin 按绑定校过滤，
// 过滤在总线侧完成，不是客户端过滤。
func (h *Handler) AdminEvents(c *gin.Context, params oapi.AdminEventsParams) {
	session, ok := h.principal(c)
	if !ok {
		return
	}
	scope, ok := h.authorize(c, session)
	if !ok {
		return
	}
	if err := requireKind(scope, corescope.KindMaster, corescope.KindAdmin); err != nil {
		fail(c, err)
		return
	}
	var schools []string
	if scope.Kind() == corescope.KindAdmin {
		schools = scope.SchoolIDs()
	}
	sub, cancel := h.broker.Subscribe(c.Request.Context(), schools, parseLastEventID(params.LastEventID))
	defer cancel()
	// 全局 http.Server 带有限的 WriteTimeout，从读到请求头起算，
	// 到点会掐断本连接的一切写入；长流必须只对本次响应清掉写超时。
	// 不清的后果：15s 心跳发两次后流断，60s 空闲关闭永不生效
	//（P6 审查 P1；回归见 cmd/server 的 TestP6SSESurvivesServerWriteTimeout）。
	if err := http.NewResponseController(c.Writer).SetWriteDeadline(time.Time{}); err != nil {
		fail(c, errs.New(errs.CodeInternal, "cannot clear write deadline"))
		return
	}

	flusher, canFlush := c.Writer.(http.Flusher)
	if !canFlush {
		// gin 的 ResponseWriter 恒实现 Flusher；缺失即部署错误，失败关闭。
		fail(c, errStreamingUnsupported)
		return
	}
	header := c.Writer.Header()
	header.Set("Content-Type", "text/event-stream; charset=utf-8")
	header.Set("Cache-Control", "no-store")
	header.Set("X-Accel-Buffering", "no")
	c.Writer.WriteHeader(http.StatusOK)
	flusher.Flush()

	h.stream(c, scope, sub, flusher)
}

// sseWriter 聚合一帧的写出。
type sseWriter struct {
	w       gin.ResponseWriter
	flusher http.Flusher
}

// write 写一帧并冲刷；写出失败返回 false（客户端已断开）。
func (s sseWriter) write(id uint64, typ string, data any) bool {
	var buf []byte
	if id > 0 {
		buf = append(buf, []byte(fmt.Sprintf("id: %d\n", id))...)
	}
	buf = append(buf, []byte("event: "+typ+"\n")...)
	raw, err := json.Marshal(data)
	if err != nil {
		// 信封类型不可序列化是实现缺陷：丢数据行也不能发坏帧。
		raw = []byte("{}")
	}
	buf = append(buf, []byte("data: ")...)
	buf = append(buf, raw...)
	buf = append(buf, '\n', '\n')
	if _, err := s.w.Write(buf); err != nil {
		return false
	}
	s.flusher.Flush()
	return true
}

// stream 是 SSE 主循环：事件、心跳与空闲三路 select。
// 判据：事件按到达序直写；心跳 15s；60s 无数据事件发 end 并返回；
// resync 后跟 snapshot（现算，失败只丢 snapshot 帧）。
func (h *Handler) stream(c *gin.Context, scope corescope.Scope, sub appevents.Subscription, flusher http.Flusher) {
	out := sseWriter{w: c.Writer, flusher: flusher}
	heartbeatDue := h.clock.Now().Add(defaultHeartbeat)
	idleDue := h.clock.Now().Add(defaultIdleClose)
	var pending <-chan time.Time
	arm := func() {
		wait := heartbeatDue
		if idleDue.Before(wait) {
			wait = idleDue
		}
		d := wait.Sub(h.clock.Now())
		if d < 0 {
			d = 0
		}
		pending = h.After(d)
	}
	arm()
	for {
		select {
		case <-c.Request.Context().Done():
			return
		case evt, ok := <-sub.C:
			if !ok {
				return
			}
			if evt.Type == appevents.TypeResync {
				// 缺口过大：先 resync，再由用例现算当日前景。
				out.write(0, appevents.TypeResync, resyncData{From: fromOf(evt)})
				if view, err := h.diag.Snapshot(c.Request.Context(), scope); err == nil {
					out.write(0, appevents.TypeSnapshot, view)
				}
				idleDue = h.clock.Now().Add(defaultIdleClose)
			} else {
				if !out.write(evt.ID, evt.Type, frameData{
					SchoolID: evt.SchoolID,
					TaskID:   evt.TaskID,
					Day:      evt.Day,
					At:       atOf(evt),
					Payload:  evt.Payload,
				}) {
					return
				}
				idleDue = h.clock.Now().Add(defaultIdleClose)
			}
			arm()
		case <-pending:
			pending = nil
			now := h.clock.Now()
			if !now.Before(idleDue) {
				out.write(0, appevents.TypeEnd, struct{}{})
				return
			}
			if !now.Before(heartbeatDue) {
				// 心跳点重鉴权：撤权、停用或绑定校变化都立即断开，
				// 不再向已失权的主体推送事件。
				if !h.reauthorize(c, scope) {
					out.write(0, appevents.TypeEnd, struct{}{})
					return
				}
				if !out.write(0, appevents.TypeHeartbeat, struct{}{}) {
					return
				}
				heartbeatDue = now.Add(defaultHeartbeat)
			}
			arm()
		}
	}
}

// fromOf 取 resync 事件 payload 里客户端的 lastEventID。
func fromOf(evt appevents.Event) uint64 {
	if v, ok := evt.Payload["from"].(uint64); ok {
		return v
	}
	return 0
}

// atOf 把事件时刻格式化为 RFC3339；零时刻省略。
func atOf(evt appevents.Event) string {
	if evt.At.IsZero() {
		return ""
	}
	return evt.At.Format(time.RFC3339)
}

// reauthorize 在心跳点重新解析会话与作用域。
// 会话被撤销、账号停用、角色或绑定校变化都返回 false。
// **必须重新 Resolve**：连接建立时注入的会话是快照，状态不会自己更新。
func (h *Handler) reauthorize(c *gin.Context, scope corescope.Scope) bool {
	if h.sessions == nil || h.cookieName == "" {
		return false
	}
	token, err := c.Cookie(h.cookieName)
	if err != nil {
		return false
	}
	session, err := h.sessions.Resolve(c.Request.Context(), token)
	if err != nil {
		return false
	}
	next, _, err := h.authz.Authorize(c.Request.Context(), session.User)
	if err != nil {
		return false
	}
	return sameScope(scope, next)
}

// sameScope 比较两个作用域的可见面。
// master 与 user 只看形状；admin 还要院校集合相等。
func sameScope(a, b corescope.Scope) bool {
	if a.Kind() != b.Kind() {
		return false
	}
	as, bs := a.SchoolIDs(), b.SchoolIDs()
	if len(as) != len(bs) {
		return false
	}
	sort.Strings(as)
	sort.Strings(bs)
	for i := range as {
		if as[i] != bs[i] {
			return false
		}
	}
	return true
}
