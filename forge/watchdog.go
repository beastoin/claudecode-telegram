package forge

import "github.com/beastoin/claudecode-telegram/forge/watchdog"

type Watchdog = watchdog.Watchdog
type BridgeClient = watchdog.BridgeClient
type Clock = watchdog.Clock
type IntegrityMonitor = watchdog.IntegrityMonitor
type RestartPolicy = watchdog.RestartPolicy
type ExponentialBackoffPolicy = watchdog.ExponentialBackoffPolicy

var NewExponentialBackoffPolicy = watchdog.NewExponentialBackoffPolicy
