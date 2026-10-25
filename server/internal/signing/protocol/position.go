package protocol

import (
	"strconv"
	"strings"
)

// Point 是一个 WGS84 坐标。
type Point struct {
	Lng float64
	Lat float64
}

// Position 是单个候选签到点。
type Position struct {
	ID          string
	Type        string
	Title       string
	Address     string
	LngLat      Point
	RangeM      *int
	Points      []Point
	MapType     *int
	AddressName *string
	Campus      *string
	BuildingID  *string
	CreateTime  *string
}

// TimeWindow 是顶层 Range：当日签到时间窗（epoch 秒）。
type TimeWindow struct {
	StartTime     int
	EndTime       int
	SignDay       *int
	RelatType     *int
	RelatTimeType *int
}

// SignPositionConfig 是 signPosition 响应的 data 对象。
type SignPositionConfig struct {
	State              *int
	Msg                string
	AcsState           *string
	OutState           *string
	Remark             *string
	FileURL            *string
	Type               *string
	IsNeedPhoto        *int
	AttachmentFileName *string
	TimeWindow         *TimeWindow
	Positions          []Position
}

// ParseSignPosition 解析 signPosition 的 data 对象。
//
// Position 必须是数组，可多于一个；每个点缺必填键即 ParseError。
// 顶层 Range 是时间窗对象，不走数值收敛；IsNeedPhoto 原样保留。
func ParseSignPosition(data any) (SignPositionConfig, error) {
	m, ok := data.(map[string]any)
	if !ok {
		return SignPositionConfig{}, parseError("signPosition", data)
	}

	rawPositions, ok := m["Position"]
	if !ok || rawPositions == nil {
		return SignPositionConfig{}, parseError("Position", m)
	}
	list, ok := rawPositions.([]any)
	if !ok {
		return SignPositionConfig{}, parseError("Position", rawPositions)
	}
	positions := make([]Position, 0, len(list))
	for _, item := range list {
		position, err := parsePosition(item)
		if err != nil {
			return SignPositionConfig{}, err
		}
		positions = append(positions, position)
	}

	window, err := parseTimeWindow(m["Range"])
	if err != nil {
		return SignPositionConfig{}, err
	}

	return SignPositionConfig{
		State:              intPtr(m["State"]),
		Msg:                msgsOf(m["Msg"]),
		AcsState:           optionalString(m["AcsState"]),
		OutState:           optionalString(m["OutState"]),
		Remark:             optionalString(m["Remark"]),
		FileURL:            optionalString(m["FileUrl"]),
		Type:               optionalString(m["Type"]),
		IsNeedPhoto:        intPtr(m["IsNeedPhoto"]),
		AttachmentFileName: optionalString(m["AttachmentFileName"]),
		TimeWindow:         window,
		Positions:          positions,
	}, nil
}

func parsePosition(item any) (Position, error) {
	m, ok := item.(map[string]any)
	if !ok {
		return Position{}, parseError("Position", item)
	}
	id, err := requiredString(m, "Id")
	if err != nil {
		return Position{}, err
	}
	typ, err := requiredString(m, "Type")
	if err != nil {
		return Position{}, err
	}
	title, err := requiredString(m, "Title")
	if err != nil {
		return Position{}, err
	}
	address, err := requiredString(m, "Address")
	if err != nil {
		return Position{}, err
	}
	rawLngLat, ok := m["LngLat"]
	if !ok || rawLngLat == nil {
		return Position{}, parseError("LngLat", m)
	}
	lngLat, err := parseLngLat(rawLngLat)
	if err != nil {
		return Position{}, err
	}
	points, err := parsePoints(m["Points"])
	if err != nil {
		return Position{}, err
	}
	return Position{
		ID:          id,
		Type:        typ,
		Title:       title,
		Address:     address,
		LngLat:      lngLat,
		RangeM:      intPtr(m["Range"]),
		Points:      points,
		MapType:     intPtr(m["MapType"]),
		AddressName: optionalString(m["AddressName"]),
		Campus:      optionalString(m["Campus"]),
		BuildingID:  noneLike(m["BuildingId"]),
		CreateTime:  optionalString(m["CreateTime"]),
	}, nil
}

func parseLngLat(v any) (Point, error) {
	s, ok := v.(string)
	if !ok {
		return Point{}, parseError("LngLat", v)
	}
	parts := strings.Split(s, ",")
	if len(parts) != 2 {
		return Point{}, parseError("LngLat", s)
	}
	lng, err := strconv.ParseFloat(strings.TrimSpace(parts[0]), 64)
	if err != nil {
		return Point{}, parseError("LngLat", s)
	}
	lat, err := strconv.ParseFloat(strings.TrimSpace(parts[1]), 64)
	if err != nil {
		return Point{}, parseError("LngLat", s)
	}
	return Point{Lng: lng, Lat: lat}, nil
}

func parsePoints(v any) ([]Point, error) {
	if v == nil {
		return []Point{}, nil
	}
	list, ok := v.([]any)
	if !ok {
		return nil, parseError("Points", v)
	}
	points := make([]Point, 0, len(list))
	for _, item := range list {
		point, err := parseLngLat(item)
		if err != nil {
			return nil, err
		}
		points = append(points, point)
	}
	return points, nil
}

func parseTimeWindow(v any) (*TimeWindow, error) {
	if v == nil {
		return nil, nil
	}
	m, ok := v.(map[string]any)
	if !ok {
		return nil, parseError("Range", v)
	}
	start, ok := toInt(m["StartTime"])
	if !ok {
		return nil, parseError("Range", v)
	}
	end, ok := toInt(m["EndTime"])
	if !ok {
		return nil, parseError("Range", v)
	}
	return &TimeWindow{
		StartTime:     start,
		EndTime:       end,
		SignDay:       intPtr(m["SignDay"]),
		RelatType:     intPtr(m["RelatType"]),
		RelatTimeType: intPtr(m["RelatTimeType"]),
	}, nil
}

func intPtr(v any) *int {
	if n, ok := toInt(v); ok {
		return &n
	}
	return nil
}
