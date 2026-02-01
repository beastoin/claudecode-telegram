// Package sandbox provides helpers for Docker sandbox configuration.
package sandbox

import "strings"

// Mount represents a bind mount specification for sandbox containers.
type Mount struct {
	HostPath      string
	ContainerPath string
	ReadOnly      bool
}

// ParseMounts parses a comma-separated mount spec string.
// Supported formats:
//
//	/host/path:/container/path
//	/path (same host+container)
//	ro:/host:/container (read-only)
func ParseMounts(spec string) []Mount {
	var mounts []Mount
	for _, part := range strings.Split(spec, ",") {
		part = strings.TrimSpace(part)
		if part == "" {
			continue
		}
		readonly := false
		if strings.HasPrefix(part, "ro:") {
			readonly = true
			part = strings.TrimPrefix(part, "ro:")
		}
		host := part
		container := part
		if idx := strings.Index(part, ":"); idx >= 0 {
			host = part[:idx]
			container = part[idx+1:]
		}
		mounts = append(mounts, Mount{
			HostPath:      host,
			ContainerPath: container,
			ReadOnly:      readonly,
		})
	}
	return mounts
}
