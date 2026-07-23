package forge

import "github.com/beastoin/claudecode-telegram/forge/transport"

type Transport = transport.Transport
type RegisterRequest = transport.RegisterRequest
type RegisterResponse = transport.RegisterResponse
type BridgeMessage = transport.BridgeMessage
type WorkerMessage = transport.WorkerMessage
type JSONLChunk = transport.JSONLChunk
type HTTPTransport = transport.HTTPTransport
type GRPCTransport = transport.GRPCTransport
type StubTransport = transport.StubTransport
type ReconnectTransport = transport.ReconnectTransport

var ErrUnsupported = transport.ErrUnsupported
var NewTransport = transport.NewTransport
