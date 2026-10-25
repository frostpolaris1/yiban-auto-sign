package protocol

import (
	"errors"
	"testing"
)

func positionData(t *testing.T) map[string]any {
	t.Helper()
	return envelopeData(t, "sign_position.json").(map[string]any)
}

func TestParseSignPositionFixture(t *testing.T) {
	cfg, err := ParseSignPosition(positionData(t))
	if err != nil {
		t.Fatalf("ParseSignPosition: %v", err)
	}
	if len(cfg.Positions) != 2 {
		t.Fatalf("positions = %d, want 2", len(cfg.Positions))
	}
	first := cfg.Positions[0]
	if first.ID != "27464ed31464763a6827816a0ac9c8ab" ||
		first.Type != "campus" ||
		first.Title != "示例省示例市示例区示例大学示例校区-示例苑·公共教学组团" {
		t.Fatalf("first position = %+v", first)
	}
	if first.RangeM == nil || *first.RangeM != 70 {
		t.Fatalf("range = %v, want 70", first.RangeM)
	}
	if first.MapType == nil || *first.MapType != 2 {
		t.Fatalf("map type = %v, want 2", first.MapType)
	}
	if first.LngLat.Lng != 0.101837 || first.LngLat.Lat != 1.003954 {
		t.Fatalf("lnglat = %+v", first.LngLat)
	}
	if len(first.Points) != 13 {
		t.Fatalf("points = %d, want 13", len(first.Points))
	}
	if first.BuildingID != nil {
		t.Fatalf("building id = %v, want nil", first.BuildingID)
	}
	if cfg.Positions[1].RangeM == nil || *cfg.Positions[1].RangeM != 110 {
		t.Fatalf("second range = %v, want 110", cfg.Positions[1].RangeM)
	}
	if cfg.State == nil || *cfg.State != 0 {
		t.Fatalf("state = %v, want 0", cfg.State)
	}
	if cfg.IsNeedPhoto == nil || *cfg.IsNeedPhoto != 2 {
		t.Fatalf("is_need_photo = %v, want 2", cfg.IsNeedPhoto)
	}
	if cfg.AttachmentFileName == nil || *cfg.AttachmentFileName != "" {
		t.Fatalf("attachment = %v, want empty", cfg.AttachmentFileName)
	}
	if cfg.TimeWindow == nil ||
		cfg.TimeWindow.StartTime != 1790980200 ||
		cfg.TimeWindow.EndTime != 1790985000 {
		t.Fatalf("time window = %+v", cfg.TimeWindow)
	}
	if cfg.TimeWindow.SignDay == nil || *cfg.TimeWindow.SignDay != 1 {
		t.Fatalf("sign day = %v", cfg.TimeWindow.SignDay)
	}
}

func TestParseSignPositionConvertsStrings(t *testing.T) {
	data := positionData(t)
	first := data["Position"].([]any)[0].(map[string]any)
	first["Range"] = "110"
	first["MapType"] = "2"
	cfg, err := ParseSignPosition(data)
	if err != nil {
		t.Fatalf("ParseSignPosition: %v", err)
	}
	if cfg.Positions[0].RangeM == nil || *cfg.Positions[0].RangeM != 110 {
		t.Fatalf("range = %v, want 110", cfg.Positions[0].RangeM)
	}
	if cfg.Positions[0].MapType == nil || *cfg.Positions[0].MapType != 2 {
		t.Fatalf("map type = %v, want 2", cfg.Positions[0].MapType)
	}
}

func TestParseSignPositionNoneLike(t *testing.T) {
	for _, raw := range []any{"None", "", nil} {
		data := positionData(t)
		first := data["Position"].([]any)[0].(map[string]any)
		first["BuildingId"] = raw
		cfg, err := ParseSignPosition(data)
		if err != nil {
			t.Fatalf("ParseSignPosition: %v", err)
		}
		if cfg.Positions[0].BuildingID != nil {
			t.Fatalf("BuildingId %#v = %v, want nil", raw, cfg.Positions[0].BuildingID)
		}
	}
}

func TestParseSignPositionRejectsBadShapes(t *testing.T) {
	cases := []struct {
		name   string
		mutate func(map[string]any)
		field  string
	}{
		{"missing positions", func(m map[string]any) { delete(m, "Position") }, "Position"},
		{"positions not a list", func(m map[string]any) { m["Position"] = "x" }, "Position"},
		{"missing id", func(m map[string]any) { m["Position"].([]any)[0].(map[string]any)["Id"] = nil }, "Id"},
		{"bad lnglat", func(m map[string]any) { m["Position"].([]any)[0].(map[string]any)["LngLat"] = "1,2,3" }, "LngLat"},
		{"bad points", func(m map[string]any) { m["Position"].([]any)[0].(map[string]any)["Points"] = "x" }, "Points"},
		{"bad range", func(m map[string]any) { m["Range"] = "x" }, "Range"},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			data := positionData(t)
			tc.mutate(data)
			_, err := ParseSignPosition(data)
			var parse *ParseError
			if !errors.As(err, &parse) || parse.Field != tc.field {
				t.Fatalf("error = %v, want ParseError(%s)", err, tc.field)
			}
		})
	}
}

func TestParseSignPositionEmptyList(t *testing.T) {
	data := positionData(t)
	data["Position"] = []any{}
	cfg, err := ParseSignPosition(data)
	if err != nil {
		t.Fatalf("ParseSignPosition: %v", err)
	}
	if len(cfg.Positions) != 0 {
		t.Fatalf("positions = %d, want 0", len(cfg.Positions))
	}
}
