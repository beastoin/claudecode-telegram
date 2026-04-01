import Testing
import Foundation
@testable import BooCore

@Suite("JSON-RPC Types")
struct JSONRPCTypesTests {

    // MARK: - Request encoding

    @Test("Request encodes with string ID")
    func requestEncodesStringID() throws {
        let req = JSONRPCRequest(id: .string("abc"), method: "list_terminals", params: nil)
        let data = try JSONEncoder().encode(req)
        let json = try JSONSerialization.jsonObject(with: data) as! [String: Any]

        #expect(json["jsonrpc"] as? String == "2.0")
        #expect(json["id"] as? String == "abc")
        #expect(json["method"] as? String == "list_terminals")
        #expect(json["params"] == nil)
    }

    @Test("Request encodes with int ID")
    func requestEncodesIntID() throws {
        let req = JSONRPCRequest(id: .int(42), method: "get_terminal_info", params: nil)
        let data = try JSONEncoder().encode(req)
        let json = try JSONSerialization.jsonObject(with: data) as! [String: Any]

        #expect(json["id"] as? Int == 42)
    }

    @Test("Request encodes params")
    func requestEncodesParams() throws {
        let params: [String: JSONValue] = [
            "terminal_id": .string("abc-123"),
            "text": .string("hello\r"),
        ]
        let req = JSONRPCRequest(id: .int(1), method: "send_text", params: .object(params))
        let data = try JSONEncoder().encode(req)
        let json = try JSONSerialization.jsonObject(with: data) as! [String: Any]
        let p = json["params"] as! [String: Any]

        #expect(p["terminal_id"] as? String == "abc-123")
        #expect(p["text"] as? String == "hello\r")
    }

    // MARK: - Response decoding

    @Test("Response decodes success result")
    func responseDecodesSuccess() throws {
        let json = """
        {"jsonrpc":"2.0","id":1,"result":{"ok":true}}
        """.data(using: .utf8)!
        let resp = try JSONDecoder().decode(JSONRPCResponse.self, from: json)

        #expect(resp.id == .int(1))
        if case .object(let obj) = resp.result {
            #expect(obj["ok"] == .bool(true))
        } else {
            Issue.record("Expected object result")
        }
        #expect(resp.error == nil)
    }

    @Test("Response decodes error")
    func responseDecodesError() throws {
        let json = """
        {"jsonrpc":"2.0","id":1,"error":{"code":-32600,"message":"Invalid Request"}}
        """.data(using: .utf8)!
        let resp = try JSONDecoder().decode(JSONRPCResponse.self, from: json)

        #expect(resp.result == nil)
        #expect(resp.error?.code == -32600)
        #expect(resp.error?.message == "Invalid Request")
    }

    @Test("Response decodes array result")
    func responseDecodesArrayResult() throws {
        let json = """
        {"jsonrpc":"2.0","id":"x","result":[{"id":"t1","title":"zsh","focused":true,"columns":80,"rows":24}]}
        """.data(using: .utf8)!
        let resp = try JSONDecoder().decode(JSONRPCResponse.self, from: json)

        #expect(resp.id == .string("x"))
        if case .array(let arr) = resp.result {
            #expect(arr.count == 1)
            if case .object(let term) = arr[0] {
                #expect(term["id"] == .string("t1"))
                #expect(term["focused"] == .bool(true))
                #expect(term["columns"] == .int(80))
            } else {
                Issue.record("Expected object in array")
            }
        } else {
            Issue.record("Expected array result")
        }
    }

    @Test("Response decodes null ID for notification errors")
    func responseDecodesNullID() throws {
        let json = """
        {"jsonrpc":"2.0","id":null,"error":{"code":-32700,"message":"Parse error"}}
        """.data(using: .utf8)!
        let resp = try JSONDecoder().decode(JSONRPCResponse.self, from: json)

        #expect(resp.id == nil)
        #expect(resp.error?.code == -32700)
    }

    // MARK: - JSONValue

    @Test("JSONValue equality")
    func jsonValueEquality() {
        #expect(JSONValue.string("a") == JSONValue.string("a"))
        #expect(JSONValue.int(1) == JSONValue.int(1))
        #expect(JSONValue.double(1.5) == JSONValue.double(1.5))
        #expect(JSONValue.bool(true) == JSONValue.bool(true))
        #expect(JSONValue.null == JSONValue.null)
        #expect(JSONValue.string("a") != JSONValue.int(1))
    }

    // MARK: - RPCID

    @Test("RPCID equality")
    func rpcIDEquality() {
        #expect(RPCID.string("a") == RPCID.string("a"))
        #expect(RPCID.int(1) == RPCID.int(1))
        #expect(RPCID.string("1") != RPCID.int(1))
    }
}
