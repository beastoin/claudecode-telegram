package forge

import "github.com/beastoin/claudecode-telegram/forge/connector"

type Caps = connector.Caps
type Reqs = connector.Reqs
type ConnectorConfig = connector.ConnectorConfig
type InboundMessage = connector.InboundMessage
type File = connector.File
type Response = connector.Response
type FileSend = connector.FileSend
type WorkerInfo = connector.WorkerInfo
type Connector = connector.Connector
type ExternalReceiver = connector.ExternalReceiver
type PushReceiver = connector.PushReceiver
type PollReceiver = connector.PollReceiver
type StreamReceiver = connector.StreamReceiver
type TeamAware = connector.TeamAware
type InboundSink = connector.InboundSink
type AuthPrompter = connector.AuthPrompter
type AuthStatusNotifier = connector.AuthStatusNotifier
type AuthPromptRequest = connector.AuthPromptRequest
type AuthPromptResult = connector.AuthPromptResult
type CommandSpec = connector.CommandSpec
type CommandArg = connector.CommandArg
type CommandInvocation = connector.CommandInvocation
type CommandResult = connector.CommandResult
type CommandHandler = connector.CommandHandler
type CommandSurface = connector.CommandSurface

const (
	CapText           = connector.CapText
	CapFiles          = connector.CapFiles
	CapTeamDiscovery  = connector.CapTeamDiscovery
	CapWorkerToWorker = connector.CapWorkerToWorker
	CapThreads        = connector.CapThreads
	CapMarkdown       = connector.CapMarkdown
	CapVoice          = connector.CapVoice
	CapTemplates      = connector.CapTemplates
	CapMultiChannel   = connector.CapMultiChannel

	ReqTmuxReady    = connector.ReqTmuxReady
	ReqRuntime      = connector.ReqRuntime
	ReqHTTPListener = connector.ReqHTTPListener
	ReqTransport    = connector.ReqTransport
)

var (
	ErrNoMessage = connector.ErrNoMessage
	ErrNoRoute   = connector.ErrNoRoute
)

var RegisterConnector = connector.RegisterConnector
var NewConnector = connector.NewConnector
var ListConnectorTypes = connector.ListConnectorTypes

func formatInbound(msg InboundMessage) string {
	return connector.FormatInbound(msg)
}
