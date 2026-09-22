import queue
import threading
import time

import grpc

import bridge_pb2
import bridge_pb2_grpc
from bridge_grpc import BridgeGRPCServer


def wait_until(predicate, timeout=3.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return False


class GRPCTestClient:
    def __init__(self, port):
        self.channel = grpc.insecure_channel(f"localhost:{port}")
        grpc.channel_ready_future(self.channel).result(timeout=3)
        self.stub = bridge_pb2_grpc.BridgeStub(self.channel)

    def close(self):
        self.channel.close()


def start_server(**kwargs):
    server = BridgeGRPCServer(**kwargs)
    port = server.start(0)
    return server, port


def test_health_check():
    server, port = start_server()
    client = GRPCTestClient(port)
    try:
        response = client.stub.Check(bridge_pb2.HealthCheckRequest(service="bridge"))
        assert response.status == bridge_pb2.HealthCheckResponse.SERVING
    finally:
        client.close()
        server.stop()


def test_register_and_heartbeat():
    registered = []
    server, port = start_server(on_worker_register=lambda *args: registered.append(args))
    client = GRPCTestClient(port)
    try:
        response = client.stub.Register(
            bridge_pb2.RegisterRequest(
                name="mon",
                host="host-a",
                version="1.0.0",
                tools={"shell": "true"},
            )
        )
        assert response.ok
        assert registered == [("mon", "host-a", "1.0.0", {"shell": "true"})]

        heartbeat = client.stub.Heartbeat(bridge_pb2.HeartbeatRequest(name="mon"))
        assert heartbeat.ok
    finally:
        client.close()
        server.stop()


def test_message_stream():
    responses = []
    server, port = start_server(
        on_worker_response=lambda name, text, payload: responses.append((name, text, payload))
    )
    client = GRPCTestClient(port)
    outbound = queue.Queue()

    def worker_messages():
        while True:
            item = outbound.get()
            if item is None:
                return
            yield item

    try:
        assert client.stub.Register(bridge_pb2.RegisterRequest(name="mon")).ok

        stream = client.stub.MessageStream(worker_messages())
        received = queue.Queue()

        def receive_bridge_messages():
            try:
                for message in stream:
                    received.put(message)
            except grpc.RpcError as exc:
                received.put(exc)

        thread = threading.Thread(target=receive_bridge_messages, daemon=True)
        thread.start()
        assert wait_until(lambda: server.is_worker_connected("mon"))

        assert server.send_to_worker("mon", "hello", "bridge")
        bridge_message = received.get(timeout=3)
        assert isinstance(bridge_message, bridge_pb2.BridgeMessage)
        assert bridge_message.type == "message"
        assert bridge_message.text == "hello"
        assert getattr(bridge_message, "from") == "bridge"

        outbound.put(bridge_pb2.WorkerMessage(type="response", text="done", payload=b"{}"))
        assert wait_until(lambda: responses == [("mon", "done", b"{}")])
    finally:
        outbound.put(None)
        client.close()
        server.stop()


def test_deregister():
    server, port = start_server()
    client = GRPCTestClient(port)
    try:
        assert client.stub.Register(bridge_pb2.RegisterRequest(name="mon")).ok
        response = client.stub.Deregister(bridge_pb2.DeregisterRequest(name="mon"))
        assert response.ok
        assert not server.is_worker_connected("mon")
        assert server.get_connected_workers() == []
    finally:
        client.close()
        server.stop()


def test_worker_disconnect_cleanup():
    disconnected = []
    server, port = start_server(on_worker_disconnect=lambda name: disconnected.append(name))
    client = GRPCTestClient(port)
    outbound = queue.Queue()

    def worker_messages():
        while True:
            item = outbound.get()
            if item is None:
                return
            yield item

    try:
        assert client.stub.Register(bridge_pb2.RegisterRequest(name="mon")).ok
        stream = client.stub.MessageStream(worker_messages())

        received = queue.Queue()

        def receive_bridge_messages():
            try:
                for message in stream:
                    received.put(message)
            except grpc.RpcError:
                pass

        thread = threading.Thread(target=receive_bridge_messages, daemon=True)
        thread.start()
        assert wait_until(lambda: server.is_worker_connected("mon"))

        outbound.put(None)
        assert wait_until(lambda: not server.is_worker_connected("mon"))
        assert disconnected == ["mon"]
    finally:
        client.close()
        server.stop()


if __name__ == "__main__":
    import pytest

    raise SystemExit(pytest.main([__file__, "-v"]))
