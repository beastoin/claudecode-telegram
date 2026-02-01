package sandbox

import "testing"

func TestParseMounts(t *testing.T) {
	tests := []struct {
		name string
		spec string
		want []Mount
	}{
		{name: "empty", spec: "", want: nil},
		{
			name: "single mount",
			spec: "/host:/container",
			want: []Mount{{HostPath: "/host", ContainerPath: "/container", ReadOnly: false}},
		},
		{
			name: "readonly mount",
			spec: "ro:/secret:/secret",
			want: []Mount{{HostPath: "/secret", ContainerPath: "/secret", ReadOnly: true}},
		},
		{
			name: "same path mount",
			spec: "/work",
			want: []Mount{{HostPath: "/work", ContainerPath: "/work", ReadOnly: false}},
		},
		{
			name: "trim and multiple",
			spec: " /a:/b , ro:/c:/d ",
			want: []Mount{
				{HostPath: "/a", ContainerPath: "/b", ReadOnly: false},
				{HostPath: "/c", ContainerPath: "/d", ReadOnly: true},
			},
		},
	}

	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			got := ParseMounts(tt.spec)
			if len(got) != len(tt.want) {
				t.Fatalf("expected %d mounts, got %d", len(tt.want), len(got))
			}
			for i := range got {
				if got[i] != tt.want[i] {
					t.Errorf("mount %d = %+v, want %+v", i, got[i], tt.want[i])
				}
			}
		})
	}
}
