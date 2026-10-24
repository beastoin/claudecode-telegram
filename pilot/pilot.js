#!/usr/bin/env node
import { createRequire } from "node:module";
var __create = Object.create;
var __getProtoOf = Object.getPrototypeOf;
var __defProp = Object.defineProperty;
var __getOwnPropNames = Object.getOwnPropertyNames;
var __hasOwnProp = Object.prototype.hasOwnProperty;
function __accessProp(key) {
  return this[key];
}
var __toESMCache_node;
var __toESMCache_esm;
var __toESM = (mod, isNodeMode, target) => {
  var canCache = mod != null && typeof mod === "object";
  if (canCache) {
    var cache = isNodeMode ? __toESMCache_node ??= new WeakMap : __toESMCache_esm ??= new WeakMap;
    var cached = cache.get(mod);
    if (cached)
      return cached;
  }
  target = mod != null ? __create(__getProtoOf(mod)) : {};
  const to = isNodeMode || !mod || !mod.__esModule ? __defProp(target, "default", { value: mod, enumerable: true }) : target;
  for (let key of __getOwnPropNames(mod))
    if (!__hasOwnProp.call(to, key))
      __defProp(to, key, {
        get: __accessProp.bind(mod, key),
        enumerable: true
      });
  if (canCache)
    cache.set(mod, to);
  return to;
};
var __commonJS = (cb, mod) => () => (mod || cb((mod = { exports: {} }).exports, mod), mod.exports);
var __require = /* @__PURE__ */ createRequire(import.meta.url);

// node_modules/@lydell/node-pty/eventEmitter2.js
var require_eventEmitter2 = __commonJS((exports) => {
  Object.defineProperty(exports, "__esModule", { value: true });
  var EventEmitter2 = function() {
    function EventEmitter22() {
      this._listeners = [];
    }
    Object.defineProperty(EventEmitter22.prototype, "event", {
      get: function() {
        var _this = this;
        if (!this._event) {
          this._event = function(listener) {
            _this._listeners.push(listener);
            var disposable = {
              dispose: function() {
                for (var i = 0;i < _this._listeners.length; i++) {
                  if (_this._listeners[i] === listener) {
                    _this._listeners.splice(i, 1);
                    return;
                  }
                }
              }
            };
            return disposable;
          };
        }
        return this._event;
      },
      enumerable: true,
      configurable: true
    });
    EventEmitter22.prototype.fire = function(data) {
      var queue = [];
      for (var i = 0;i < this._listeners.length; i++) {
        queue.push(this._listeners[i]);
      }
      for (var i = 0;i < queue.length; i++) {
        queue[i].call(undefined, data);
      }
    };
    return EventEmitter22;
  }();
  exports.EventEmitter2 = EventEmitter2;
});

// node_modules/@lydell/node-pty/terminal.js
var require_terminal = __commonJS((exports) => {
  Object.defineProperty(exports, "__esModule", { value: true });
  var events_1 = __require("events");
  var eventEmitter2_1 = require_eventEmitter2();
  exports.DEFAULT_COLS = 80;
  exports.DEFAULT_ROWS = 24;
  var FLOW_CONTROL_PAUSE = "\x13";
  var FLOW_CONTROL_RESUME = "\x11";
  var Terminal = function() {
    function Terminal2(opt) {
      this._pid = 0;
      this._fd = 0;
      this._cols = 0;
      this._rows = 0;
      this._readable = false;
      this._writable = false;
      this._onData = new eventEmitter2_1.EventEmitter2;
      this._onExit = new eventEmitter2_1.EventEmitter2;
      this._internalee = new events_1.EventEmitter;
      this.handleFlowControl = !!(opt === null || opt === undefined ? undefined : opt.handleFlowControl);
      this._flowControlPause = (opt === null || opt === undefined ? undefined : opt.flowControlPause) || FLOW_CONTROL_PAUSE;
      this._flowControlResume = (opt === null || opt === undefined ? undefined : opt.flowControlResume) || FLOW_CONTROL_RESUME;
      if (!opt) {
        return;
      }
      this._checkType("name", opt.name ? opt.name : undefined, "string");
      this._checkType("cols", opt.cols ? opt.cols : undefined, "number");
      this._checkType("rows", opt.rows ? opt.rows : undefined, "number");
      this._checkType("cwd", opt.cwd ? opt.cwd : undefined, "string");
      this._checkType("env", opt.env ? opt.env : undefined, "object");
      this._checkType("uid", opt.uid ? opt.uid : undefined, "number");
      this._checkType("gid", opt.gid ? opt.gid : undefined, "number");
      this._checkType("encoding", opt.encoding ? opt.encoding : undefined, "string");
    }
    Object.defineProperty(Terminal2.prototype, "onData", {
      get: function() {
        return this._onData.event;
      },
      enumerable: true,
      configurable: true
    });
    Object.defineProperty(Terminal2.prototype, "onExit", {
      get: function() {
        return this._onExit.event;
      },
      enumerable: true,
      configurable: true
    });
    Object.defineProperty(Terminal2.prototype, "pid", {
      get: function() {
        return this._pid;
      },
      enumerable: true,
      configurable: true
    });
    Object.defineProperty(Terminal2.prototype, "cols", {
      get: function() {
        return this._cols;
      },
      enumerable: true,
      configurable: true
    });
    Object.defineProperty(Terminal2.prototype, "rows", {
      get: function() {
        return this._rows;
      },
      enumerable: true,
      configurable: true
    });
    Terminal2.prototype.write = function(data) {
      if (this.handleFlowControl) {
        if (data === this._flowControlPause) {
          this.pause();
          return;
        }
        if (data === this._flowControlResume) {
          this.resume();
          return;
        }
      }
      this._write(data);
    };
    Terminal2.prototype._forwardEvents = function() {
      var _this = this;
      this.on("data", function(e) {
        return _this._onData.fire(e);
      });
      this.on("exit", function(exitCode, signal) {
        return _this._onExit.fire({ exitCode, signal });
      });
    };
    Terminal2.prototype._checkType = function(name, value, type, allowArray) {
      if (allowArray === undefined) {
        allowArray = false;
      }
      if (value === undefined) {
        return;
      }
      if (allowArray) {
        if (Array.isArray(value)) {
          value.forEach(function(v, i) {
            if (typeof v !== type) {
              throw new Error(name + "[" + i + "] must be a " + type + " (not a " + typeof v[i] + ")");
            }
          });
          return;
        }
      }
      if (typeof value !== type) {
        throw new Error(name + " must be a " + type + " (not a " + typeof value + ")");
      }
    };
    Terminal2.prototype.end = function(data) {
      this._socket.end(data);
    };
    Terminal2.prototype.pipe = function(dest, options) {
      return this._socket.pipe(dest, options);
    };
    Terminal2.prototype.pause = function() {
      return this._socket.pause();
    };
    Terminal2.prototype.resume = function() {
      return this._socket.resume();
    };
    Terminal2.prototype.setEncoding = function(encoding) {
      if (this._socket._decoder) {
        delete this._socket._decoder;
      }
      if (encoding) {
        this._socket.setEncoding(encoding);
      }
    };
    Terminal2.prototype.addListener = function(eventName, listener) {
      this.on(eventName, listener);
    };
    Terminal2.prototype.on = function(eventName, listener) {
      if (eventName === "close") {
        this._internalee.on("close", listener);
        return;
      }
      this._socket.on(eventName, listener);
    };
    Terminal2.prototype.emit = function(eventName) {
      var args = [];
      for (var _i = 1;_i < arguments.length; _i++) {
        args[_i - 1] = arguments[_i];
      }
      if (eventName === "close") {
        return this._internalee.emit.apply(this._internalee, arguments);
      }
      return this._socket.emit.apply(this._socket, arguments);
    };
    Terminal2.prototype.listeners = function(eventName) {
      return this._socket.listeners(eventName);
    };
    Terminal2.prototype.removeListener = function(eventName, listener) {
      this._socket.removeListener(eventName, listener);
    };
    Terminal2.prototype.removeAllListeners = function(eventName) {
      this._socket.removeAllListeners(eventName);
    };
    Terminal2.prototype.once = function(eventName, listener) {
      this._socket.once(eventName, listener);
    };
    Terminal2.prototype._close = function() {
      this._socket.readable = false;
      this.write = function() {};
      this.end = function() {};
      this._writable = false;
      this._readable = false;
    };
    Terminal2.prototype._parseEnv = function(env) {
      var keys = Object.keys(env || {});
      var pairs = [];
      for (var i = 0;i < keys.length; i++) {
        if (keys[i] === undefined) {
          continue;
        }
        pairs.push(keys[i] + "=" + env[keys[i]]);
      }
      return pairs;
    };
    return Terminal2;
  }();
  exports.Terminal = Terminal;
});

// node_modules/@lydell/node-pty/shared/conout.js
var require_conout = __commonJS((exports) => {
  Object.defineProperty(exports, "__esModule", { value: true });
  function getWorkerPipeName(conoutPipeName) {
    return conoutPipeName + "-worker";
  }
  exports.getWorkerPipeName = getWorkerPipeName;
});

// node_modules/@lydell/node-pty/windowsConoutConnection.js
var require_windowsConoutConnection = __commonJS((exports) => {
  var __dirname = "/home/claude/claudecode-telegram/pilot/node_modules/@lydell/node-pty";
  var __awaiter = exports && exports.__awaiter || function(thisArg, _arguments, P, generator) {
    function adopt(value) {
      return value instanceof P ? value : new P(function(resolve) {
        resolve(value);
      });
    }
    return new (P || (P = Promise))(function(resolve, reject) {
      function fulfilled(value) {
        try {
          step(generator.next(value));
        } catch (e) {
          reject(e);
        }
      }
      function rejected(value) {
        try {
          step(generator["throw"](value));
        } catch (e) {
          reject(e);
        }
      }
      function step(result) {
        result.done ? resolve(result.value) : adopt(result.value).then(fulfilled, rejected);
      }
      step((generator = generator.apply(thisArg, _arguments || [])).next());
    });
  };
  var __generator = exports && exports.__generator || function(thisArg, body) {
    var _ = { label: 0, sent: function() {
      if (t[0] & 1)
        throw t[1];
      return t[1];
    }, trys: [], ops: [] }, f, y, t, g;
    return g = { next: verb(0), throw: verb(1), return: verb(2) }, typeof Symbol === "function" && (g[Symbol.iterator] = function() {
      return this;
    }), g;
    function verb(n) {
      return function(v) {
        return step([n, v]);
      };
    }
    function step(op) {
      if (f)
        throw new TypeError("Generator is already executing.");
      while (_)
        try {
          if (f = 1, y && (t = op[0] & 2 ? y["return"] : op[0] ? y["throw"] || ((t = y["return"]) && t.call(y), 0) : y.next) && !(t = t.call(y, op[1])).done)
            return t;
          if (y = 0, t)
            op = [op[0] & 2, t.value];
          switch (op[0]) {
            case 0:
            case 1:
              t = op;
              break;
            case 4:
              _.label++;
              return { value: op[1], done: false };
            case 5:
              _.label++;
              y = op[1];
              op = [0];
              continue;
            case 7:
              op = _.ops.pop();
              _.trys.pop();
              continue;
            default:
              if (!(t = _.trys, t = t.length > 0 && t[t.length - 1]) && (op[0] === 6 || op[0] === 2)) {
                _ = 0;
                continue;
              }
              if (op[0] === 3 && (!t || op[1] > t[0] && op[1] < t[3])) {
                _.label = op[1];
                break;
              }
              if (op[0] === 6 && _.label < t[1]) {
                _.label = t[1];
                t = op;
                break;
              }
              if (t && _.label < t[2]) {
                _.label = t[2];
                _.ops.push(op);
                break;
              }
              if (t[2])
                _.ops.pop();
              _.trys.pop();
              continue;
          }
          op = body.call(thisArg, _);
        } catch (e) {
          op = [6, e];
          y = 0;
        } finally {
          f = t = 0;
        }
      if (op[0] & 5)
        throw op[1];
      return { value: op[0] ? op[1] : undefined, done: true };
    }
  };
  Object.defineProperty(exports, "__esModule", { value: true });
  var worker_threads_1 = __require("worker_threads");
  var conout_1 = require_conout();
  var path_1 = __require("path");
  var eventEmitter2_1 = require_eventEmitter2();
  var FLUSH_DATA_INTERVAL = 1000;
  var ConoutConnection = function() {
    function ConoutConnection2(_conoutPipeName) {
      var _this = this;
      this._conoutPipeName = _conoutPipeName;
      this._isDisposed = false;
      this._onReady = new eventEmitter2_1.EventEmitter2;
      var workerData = { conoutPipeName: _conoutPipeName };
      var scriptPath = __dirname.replace("node_modules.asar", "node_modules.asar.unpacked");
      this._worker = new worker_threads_1.Worker(path_1.join(scriptPath, "worker/conoutSocketWorker.js"), { workerData });
      this._worker.on("message", function(message) {
        switch (message) {
          case 1:
            _this._onReady.fire();
            return;
          default:
            console.warn("Unexpected ConoutWorkerMessage", message);
        }
      });
    }
    Object.defineProperty(ConoutConnection2.prototype, "onReady", {
      get: function() {
        return this._onReady.event;
      },
      enumerable: true,
      configurable: true
    });
    ConoutConnection2.prototype.dispose = function() {
      if (this._isDisposed) {
        return;
      }
      this._isDisposed = true;
      this._drainDataAndClose();
    };
    ConoutConnection2.prototype.connectSocket = function(socket) {
      socket.connect(conout_1.getWorkerPipeName(this._conoutPipeName));
    };
    ConoutConnection2.prototype._drainDataAndClose = function() {
      var _this = this;
      if (this._drainTimeout) {
        clearTimeout(this._drainTimeout);
      }
      this._drainTimeout = setTimeout(function() {
        return _this._destroySocket();
      }, FLUSH_DATA_INTERVAL);
    };
    ConoutConnection2.prototype._destroySocket = function() {
      return __awaiter(this, undefined, undefined, function() {
        return __generator(this, function(_a) {
          switch (_a.label) {
            case 0:
              return [4, this._worker.terminate()];
            case 1:
              _a.sent();
              return [2];
          }
        });
      });
    };
    return ConoutConnection2;
  }();
  exports.ConoutConnection = ConoutConnection;
});

// node_modules/@lydell/node-pty/package.json
var require_package = __commonJS((exports, module) => {
  module.exports = {
    name: "@lydell/node-pty",
    description: "node-pty with prebuilt binaries",
    author: "Simon Lydell",
    version: "1.1.0",
    license: "MIT",
    main: "./index.js",
    types: "./node-pty.d.ts",
    repository: {
      type: "git",
      url: "git://github.com/lydell/node-pty.git"
    },
    keywords: [
      "pty",
      "tty",
      "terminal",
      "pseudoterminal",
      "forkpty",
      "openpty",
      "prebuild",
      "prebuilt"
    ],
    optionalDependencies: {
      "@lydell/node-pty-darwin-x64": "1.1.0",
      "@lydell/node-pty-darwin-arm64": "1.1.0",
      "@lydell/node-pty-linux-x64": "1.1.0",
      "@lydell/node-pty-linux-arm64": "1.1.0",
      "@lydell/node-pty-win32-x64": "1.1.0",
      "@lydell/node-pty-win32-arm64": "1.1.0"
    }
  };
});

// node_modules/@lydell/node-pty/requireBinary.js
var require_requireBinary = __commonJS((exports) => {
  Object.defineProperty(exports, "__esModule", { value: true });
  var PACKAGE_NAME = "@lydell/node-pty-" + process.platform + "-" + process.arch;
  var help = `
This can happen if you use the "--omit=optional" (or "--no-optional") npm flag.
The "optionalDependencies" package.json feature is used to install the correct
binary executable for your current platform. Remove that flag to use @lydell/node-pty.

This can also happen if the "node_modules" folder was copied between two operating systems
that need different binaries - including "virtual" operating systems like Docker and WSL.
If so, try installing with npm rather than copying "node_modules".
`.trim();
  function requireBinary(file) {
    try {
      return __require(PACKAGE_NAME + "/" + file);
    } catch (error) {
      if (error && error.code === "MODULE_NOT_FOUND") {
        var optionalDependencies = getOptionalDependencies();
        throw new Error(optionalDependencies === undefined ? "The @lydell/node-pty package could not find the binary package: " + PACKAGE_NAME + "/" + file + `

` + help + `

Your platform (` + process.platform + "-" + process.arch + ") might not be supported." : (PACKAGE_NAME in optionalDependencies) ? "The @lydell/node-pty package supports your platform (" + process.platform + "-" + process.arch + "), but it could not find the binary package for it: " + PACKAGE_NAME + "/" + file + `

` + help : "The @lydell/node-pty package currently does not support your platform: " + process.platform + "-" + process.arch, { cause: error });
      } else {
        throw error;
      }
    }
  }
  exports.requireBinary = requireBinary;
  function getOptionalDependencies() {
    try {
      return require_package().optionalDependencies;
    } catch (_error) {
      return;
    }
  }
});

// node_modules/@lydell/node-pty/windowsPtyAgent.js
var require_windowsPtyAgent = __commonJS((exports) => {
  var __dirname = "/home/claude/claudecode-telegram/pilot/node_modules/@lydell/node-pty";
  Object.defineProperty(exports, "__esModule", { value: true });
  var fs = __require("fs");
  var path = __require("path");
  var net_1 = __require("net");
  var child_process_1 = __require("child_process");
  var windowsConoutConnection_1 = require_windowsConoutConnection();
  var requireBinary_1 = require_requireBinary();
  var conptyNative;
  var FLUSH_DATA_INTERVAL = 1000;
  var WindowsPtyAgent = function() {
    function WindowsPtyAgent2(file, args, env, cwd, cols, rows, debug, conptyInheritCursor) {
      var _this = this;
      if (conptyInheritCursor === undefined) {
        conptyInheritCursor = false;
      }
      this._pid = 0;
      this._innerPid = 0;
      if (!conptyNative) {
        conptyNative = requireBinary_1.requireBinary("conpty.node");
      }
      this._ptyNative = conptyNative;
      cwd = path.resolve(cwd);
      var commandLine = argsToCommandLine(file, args);
      var term = this._ptyNative.startProcess(file, cols, rows, debug, this._generatePipeName(), conptyInheritCursor);
      this._fd = term.fd;
      this._pty = term.pty;
      this._outSocket = new net_1.Socket;
      this._outSocket.setEncoding("utf8");
      this._conoutSocketWorker = new windowsConoutConnection_1.ConoutConnection(term.conout);
      this._conoutSocketWorker.onReady(function() {
        _this._conoutSocketWorker.connectSocket(_this._outSocket);
      });
      this._outSocket.on("connect", function() {
        _this._outSocket.emit("ready_datapipe");
      });
      var inSocketFD = fs.openSync(term.conin, "w");
      this._inSocket = new net_1.Socket({
        fd: inSocketFD,
        readable: false,
        writable: true
      });
      this._inSocket.setEncoding("utf8");
      var connect = this._ptyNative.connect(this._pty, commandLine, cwd, env, function(c) {
        return _this._$onProcessExit(c);
      });
      this._innerPid = connect.pid;
    }
    Object.defineProperty(WindowsPtyAgent2.prototype, "inSocket", {
      get: function() {
        return this._inSocket;
      },
      enumerable: true,
      configurable: true
    });
    Object.defineProperty(WindowsPtyAgent2.prototype, "outSocket", {
      get: function() {
        return this._outSocket;
      },
      enumerable: true,
      configurable: true
    });
    Object.defineProperty(WindowsPtyAgent2.prototype, "fd", {
      get: function() {
        return this._fd;
      },
      enumerable: true,
      configurable: true
    });
    Object.defineProperty(WindowsPtyAgent2.prototype, "innerPid", {
      get: function() {
        return this._innerPid;
      },
      enumerable: true,
      configurable: true
    });
    Object.defineProperty(WindowsPtyAgent2.prototype, "pty", {
      get: function() {
        return this._pty;
      },
      enumerable: true,
      configurable: true
    });
    WindowsPtyAgent2.prototype.resize = function(cols, rows) {
      if (this._exitCode !== undefined) {
        throw new Error("Cannot resize a pty that has already exited");
      }
      this._ptyNative.resize(this._pty, cols, rows);
    };
    WindowsPtyAgent2.prototype.clear = function() {
      this._ptyNative.clear(this._pty);
    };
    WindowsPtyAgent2.prototype.kill = function() {
      var _this = this;
      this._inSocket.readable = false;
      this._outSocket.readable = false;
      this._getConsoleProcessList().then(function(consoleProcessList) {
        consoleProcessList.forEach(function(pid) {
          try {
            process.kill(pid);
          } catch (e) {}
        });
        _this._ptyNative.kill(_this._pty);
      });
      this._conoutSocketWorker.dispose();
    };
    WindowsPtyAgent2.prototype._getConsoleProcessList = function() {
      var _this = this;
      return new Promise(function(resolve) {
        var agent = child_process_1.fork(path.join(__dirname, "conpty_console_list_agent"), [_this._innerPid.toString()]);
        agent.on("message", function(message) {
          clearTimeout(timeout);
          resolve(message.consoleProcessList);
        });
        var timeout = setTimeout(function() {
          agent.kill();
          resolve([_this._innerPid]);
        }, 5000);
      });
    };
    Object.defineProperty(WindowsPtyAgent2.prototype, "exitCode", {
      get: function() {
        return this._exitCode;
      },
      enumerable: true,
      configurable: true
    });
    WindowsPtyAgent2.prototype._generatePipeName = function() {
      return "conpty-" + Math.random() * 1e7;
    };
    WindowsPtyAgent2.prototype._$onProcessExit = function(exitCode) {
      var _this = this;
      this._exitCode = exitCode;
      this._flushDataAndCleanUp();
      this._outSocket.on("data", function() {
        return _this._flushDataAndCleanUp();
      });
    };
    WindowsPtyAgent2.prototype._flushDataAndCleanUp = function() {
      var _this = this;
      if (this._closeTimeout) {
        clearTimeout(this._closeTimeout);
      }
      this._closeTimeout = setTimeout(function() {
        return _this._cleanUpProcess();
      }, FLUSH_DATA_INTERVAL);
    };
    WindowsPtyAgent2.prototype._cleanUpProcess = function() {
      this._inSocket.readable = false;
      this._outSocket.readable = false;
      this._outSocket.destroy();
    };
    return WindowsPtyAgent2;
  }();
  exports.WindowsPtyAgent = WindowsPtyAgent;
  function argsToCommandLine(file, args) {
    if (isCommandLine(args)) {
      if (args.length === 0) {
        return file;
      }
      return argsToCommandLine(file, []) + " " + args;
    }
    var argv = [file];
    Array.prototype.push.apply(argv, args);
    var result = "";
    for (var argIndex = 0;argIndex < argv.length; argIndex++) {
      if (argIndex > 0) {
        result += " ";
      }
      var arg = argv[argIndex];
      var hasLopsidedEnclosingQuote = xOr(arg[0] !== '"', arg[arg.length - 1] !== '"');
      var hasNoEnclosingQuotes = arg[0] !== '"' && arg[arg.length - 1] !== '"';
      var quote = arg === "" || (arg.indexOf(" ") !== -1 || arg.indexOf("\t") !== -1) && (arg.length > 1 && (hasLopsidedEnclosingQuote || hasNoEnclosingQuotes));
      if (quote) {
        result += '"';
      }
      var bsCount = 0;
      for (var i = 0;i < arg.length; i++) {
        var p = arg[i];
        if (p === "\\") {
          bsCount++;
        } else if (p === '"') {
          result += repeatText("\\", bsCount * 2 + 1);
          result += '"';
          bsCount = 0;
        } else {
          result += repeatText("\\", bsCount);
          bsCount = 0;
          result += p;
        }
      }
      if (quote) {
        result += repeatText("\\", bsCount * 2);
        result += '"';
      } else {
        result += repeatText("\\", bsCount);
      }
    }
    return result;
  }
  exports.argsToCommandLine = argsToCommandLine;
  function isCommandLine(args) {
    return typeof args === "string";
  }
  function repeatText(text, count) {
    var result = "";
    for (var i = 0;i < count; i++) {
      result += text;
    }
    return result;
  }
  function xOr(arg1, arg2) {
    return arg1 && !arg2 || !arg1 && arg2;
  }
});

// node_modules/@lydell/node-pty/utils.js
var require_utils = __commonJS((exports) => {
  Object.defineProperty(exports, "__esModule", { value: true });
  function assign(target) {
    var sources = [];
    for (var _i = 1;_i < arguments.length; _i++) {
      sources[_i - 1] = arguments[_i];
    }
    sources.forEach(function(source) {
      return Object.keys(source).forEach(function(key) {
        return target[key] = source[key];
      });
    });
    return target;
  }
  exports.assign = assign;
});

// node_modules/@lydell/node-pty/windowsTerminal.js
var require_windowsTerminal = __commonJS((exports) => {
  var __extends = exports && exports.__extends || function() {
    var extendStatics = function(d, b) {
      extendStatics = Object.setPrototypeOf || { __proto__: [] } instanceof Array && function(d2, b2) {
        d2.__proto__ = b2;
      } || function(d2, b2) {
        for (var p in b2)
          if (b2.hasOwnProperty(p))
            d2[p] = b2[p];
      };
      return extendStatics(d, b);
    };
    return function(d, b) {
      extendStatics(d, b);
      function __() {
        this.constructor = d;
      }
      d.prototype = b === null ? Object.create(b) : (__.prototype = b.prototype, new __);
    };
  }();
  Object.defineProperty(exports, "__esModule", { value: true });
  var terminal_1 = require_terminal();
  var windowsPtyAgent_1 = require_windowsPtyAgent();
  var utils_1 = require_utils();
  var DEFAULT_FILE = "cmd.exe";
  var DEFAULT_NAME = "Windows Shell";
  var WindowsTerminal = function(_super) {
    __extends(WindowsTerminal2, _super);
    function WindowsTerminal2(file, args, opt) {
      var _this = _super.call(this, opt) || this;
      _this._checkType("args", args, "string", true);
      args = args || [];
      file = file || DEFAULT_FILE;
      opt = opt || {};
      opt.env = opt.env || process.env;
      if (opt.encoding) {
        console.warn("Setting encoding on Windows is not supported");
      }
      var env = utils_1.assign({}, opt.env);
      _this._cols = opt.cols || terminal_1.DEFAULT_COLS;
      _this._rows = opt.rows || terminal_1.DEFAULT_ROWS;
      var cwd = opt.cwd || process.cwd();
      var name = opt.name || env.TERM || DEFAULT_NAME;
      var parsedEnv = _this._parseEnv(env);
      _this._isReady = false;
      _this._deferreds = [];
      _this._agent = new windowsPtyAgent_1.WindowsPtyAgent(file, args, parsedEnv, cwd, _this._cols, _this._rows, false, opt.conptyInheritCursor);
      _this._socket = _this._agent.outSocket;
      _this._pid = _this._agent.innerPid;
      _this._fd = _this._agent.fd;
      _this._pty = _this._agent.pty;
      _this._socket.on("ready_datapipe", function() {
        ["connect", "data", "end", "timeout", "drain"].forEach(function(event) {
          _this._socket.on(event, function() {
            if (!_this._isReady && event === "data") {
              _this._isReady = true;
              _this._deferreds.forEach(function(fn) {
                fn.run();
              });
              _this._deferreds = [];
            }
          });
        });
        _this._socket.on("error", function(err) {
          _this._close();
          if (err.code) {
            if (~err.code.indexOf("errno 5") || ~err.code.indexOf("EIO"))
              return;
          }
          if (_this.listeners("error").length < 2) {
            throw err;
          }
        });
        _this._socket.on("close", function() {
          _this.emit("exit", _this._agent.exitCode);
          _this._close();
        });
      });
      _this._file = file;
      _this._name = name;
      _this._readable = true;
      _this._writable = true;
      _this._forwardEvents();
      return _this;
    }
    WindowsTerminal2.prototype._write = function(data) {
      this._defer(this._doWrite, data);
    };
    WindowsTerminal2.prototype._doWrite = function(data) {
      this._agent.inSocket.write(data);
    };
    WindowsTerminal2.open = function(options) {
      throw new Error("open() not supported on windows, use Fork() instead.");
    };
    WindowsTerminal2.prototype.resize = function(cols, rows) {
      var _this = this;
      if (cols <= 0 || rows <= 0 || isNaN(cols) || isNaN(rows) || cols === Infinity || rows === Infinity) {
        throw new Error("resizing must be done using positive cols and rows");
      }
      this._deferNoArgs(function() {
        _this._agent.resize(cols, rows);
        _this._cols = cols;
        _this._rows = rows;
      });
    };
    WindowsTerminal2.prototype.clear = function() {
      var _this = this;
      this._deferNoArgs(function() {
        _this._agent.clear();
      });
    };
    WindowsTerminal2.prototype.destroy = function() {
      var _this = this;
      this._deferNoArgs(function() {
        _this.kill();
      });
    };
    WindowsTerminal2.prototype.kill = function(signal) {
      var _this = this;
      this._deferNoArgs(function() {
        if (signal) {
          throw new Error("Signals not supported on windows.");
        }
        _this._close();
        _this._agent.kill();
      });
    };
    WindowsTerminal2.prototype._deferNoArgs = function(deferredFn) {
      var _this = this;
      if (this._isReady) {
        deferredFn.call(this);
        return;
      }
      this._deferreds.push({
        run: function() {
          return deferredFn.call(_this);
        }
      });
    };
    WindowsTerminal2.prototype._defer = function(deferredFn, arg) {
      var _this = this;
      if (this._isReady) {
        deferredFn.call(this, arg);
        return;
      }
      this._deferreds.push({
        run: function() {
          return deferredFn.call(_this, arg);
        }
      });
    };
    Object.defineProperty(WindowsTerminal2.prototype, "process", {
      get: function() {
        return this._name;
      },
      enumerable: true,
      configurable: true
    });
    Object.defineProperty(WindowsTerminal2.prototype, "master", {
      get: function() {
        throw new Error("master is not supported on Windows");
      },
      enumerable: true,
      configurable: true
    });
    Object.defineProperty(WindowsTerminal2.prototype, "slave", {
      get: function() {
        throw new Error("slave is not supported on Windows");
      },
      enumerable: true,
      configurable: true
    });
    return WindowsTerminal2;
  }(terminal_1.Terminal);
  exports.WindowsTerminal = WindowsTerminal;
});

// node_modules/@lydell/node-pty/unixTerminal.js
var require_unixTerminal = __commonJS((exports) => {
  var __extends = exports && exports.__extends || function() {
    var extendStatics = function(d, b) {
      extendStatics = Object.setPrototypeOf || { __proto__: [] } instanceof Array && function(d2, b2) {
        d2.__proto__ = b2;
      } || function(d2, b2) {
        for (var p in b2)
          if (b2.hasOwnProperty(p))
            d2[p] = b2[p];
      };
      return extendStatics(d, b);
    };
    return function(d, b) {
      extendStatics(d, b);
      function __() {
        this.constructor = d;
      }
      d.prototype = b === null ? Object.create(b) : (__.prototype = b.prototype, new __);
    };
  }();
  Object.defineProperty(exports, "__esModule", { value: true });
  var tty = __require("tty");
  var terminal_1 = require_terminal();
  var utils_1 = require_utils();
  var requireBinary_1 = require_requireBinary();
  var pty = requireBinary_1.requireBinary("pty.node");
  var helperPath = "@lydell/node-pty-" + process.platform + "-" + process.arch + "/spawn-helper";
  helperPath = process.platform === "darwin" ? __require.resolve(helperPath) : "spawn-helper-unused";
  helperPath = helperPath.replace("app.asar", "app.asar.unpacked");
  helperPath = helperPath.replace("node_modules.asar", "node_modules.asar.unpacked");
  var DEFAULT_FILE = "sh";
  var DEFAULT_NAME = "xterm";
  var DESTROY_SOCKET_TIMEOUT_MS = 200;
  var UnixTerminal = function(_super) {
    __extends(UnixTerminal2, _super);
    function UnixTerminal2(file, args, opt) {
      var _a, _b;
      var _this = _super.call(this, opt) || this;
      _this._boundClose = false;
      _this._emittedClose = false;
      if (typeof args === "string") {
        throw new Error("args as a string is not supported on unix.");
      }
      args = args || [];
      file = file || DEFAULT_FILE;
      opt = opt || {};
      opt.env = opt.env || process.env;
      _this._cols = opt.cols || terminal_1.DEFAULT_COLS;
      _this._rows = opt.rows || terminal_1.DEFAULT_ROWS;
      var uid = (_a = opt.uid) !== null && _a !== undefined ? _a : -1;
      var gid = (_b = opt.gid) !== null && _b !== undefined ? _b : -1;
      var env = utils_1.assign({}, opt.env);
      if (opt.env === process.env) {
        _this._sanitizeEnv(env);
      }
      var cwd = opt.cwd || process.cwd();
      env.PWD = cwd;
      var name = opt.name || env.TERM || DEFAULT_NAME;
      env.TERM = name;
      var parsedEnv = _this._parseEnv(env);
      var encoding = opt.encoding === undefined ? "utf8" : opt.encoding;
      var onexit = function(code, signal) {
        if (!_this._emittedClose) {
          if (_this._boundClose) {
            return;
          }
          _this._boundClose = true;
          var timeout_1 = setTimeout(function() {
            timeout_1 = null;
            _this._socket.destroy();
          }, DESTROY_SOCKET_TIMEOUT_MS);
          _this.once("close", function() {
            if (timeout_1 !== null) {
              clearTimeout(timeout_1);
            }
            _this.emit("exit", code, signal);
          });
          return;
        }
        _this.emit("exit", code, signal);
      };
      var term = pty.fork(file, args, parsedEnv, cwd, _this._cols, _this._rows, uid, gid, encoding === "utf8", helperPath, onexit);
      _this._socket = new tty.ReadStream(term.fd);
      if (encoding !== null) {
        _this._socket.setEncoding(encoding);
      }
      _this._socket.on("error", function(err) {
        if (err.code) {
          if (~err.code.indexOf("EAGAIN")) {
            return;
          }
        }
        _this._close();
        if (!_this._emittedClose) {
          _this._emittedClose = true;
          _this.emit("close");
        }
        if (err.code) {
          if (~err.code.indexOf("errno 5") || ~err.code.indexOf("EIO")) {
            return;
          }
        }
        if (_this.listeners("error").length < 2) {
          throw err;
        }
      });
      _this._pid = term.pid;
      _this._fd = term.fd;
      _this._pty = term.pty;
      _this._file = file;
      _this._name = name;
      _this._readable = true;
      _this._writable = true;
      _this._socket.on("close", function() {
        if (_this._emittedClose) {
          return;
        }
        _this._emittedClose = true;
        _this._close();
        _this.emit("close");
      });
      _this._forwardEvents();
      return _this;
    }
    Object.defineProperty(UnixTerminal2.prototype, "master", {
      get: function() {
        return this._master;
      },
      enumerable: true,
      configurable: true
    });
    Object.defineProperty(UnixTerminal2.prototype, "slave", {
      get: function() {
        return this._slave;
      },
      enumerable: true,
      configurable: true
    });
    UnixTerminal2.prototype._write = function(data) {
      this._socket.write(data);
    };
    Object.defineProperty(UnixTerminal2.prototype, "fd", {
      get: function() {
        return this._fd;
      },
      enumerable: true,
      configurable: true
    });
    Object.defineProperty(UnixTerminal2.prototype, "ptsName", {
      get: function() {
        return this._pty;
      },
      enumerable: true,
      configurable: true
    });
    UnixTerminal2.open = function(opt) {
      var self = Object.create(UnixTerminal2.prototype);
      opt = opt || {};
      if (arguments.length > 1) {
        opt = {
          cols: arguments[1],
          rows: arguments[2]
        };
      }
      var cols = opt.cols || terminal_1.DEFAULT_COLS;
      var rows = opt.rows || terminal_1.DEFAULT_ROWS;
      var encoding = opt.encoding === undefined ? "utf8" : opt.encoding;
      var term = pty.open(cols, rows);
      self._master = new tty.ReadStream(term.master);
      if (encoding !== null) {
        self._master.setEncoding(encoding);
      }
      self._master.resume();
      self._slave = new tty.ReadStream(term.slave);
      if (encoding !== null) {
        self._slave.setEncoding(encoding);
      }
      self._slave.resume();
      self._socket = self._master;
      self._pid = -1;
      self._fd = term.master;
      self._pty = term.pty;
      self._file = process.argv[0] || "node";
      self._name = process.env.TERM || "";
      self._readable = true;
      self._writable = true;
      self._socket.on("error", function(err) {
        self._close();
        if (self.listeners("error").length < 2) {
          throw err;
        }
      });
      self._socket.on("close", function() {
        self._close();
      });
      return self;
    };
    UnixTerminal2.prototype.destroy = function() {
      var _this = this;
      this._close();
      this._socket.once("close", function() {
        _this.kill("SIGHUP");
      });
      this._socket.destroy();
    };
    UnixTerminal2.prototype.kill = function(signal) {
      try {
        process.kill(this.pid, signal || "SIGHUP");
      } catch (e) {}
    };
    Object.defineProperty(UnixTerminal2.prototype, "process", {
      get: function() {
        if (process.platform === "darwin") {
          var title = pty.process(this._fd);
          return title !== "kernel_task" ? title : this._file;
        }
        return pty.process(this._fd, this._pty) || this._file;
      },
      enumerable: true,
      configurable: true
    });
    UnixTerminal2.prototype.resize = function(cols, rows) {
      if (cols <= 0 || rows <= 0 || isNaN(cols) || isNaN(rows) || cols === Infinity || rows === Infinity) {
        throw new Error("resizing must be done using positive cols and rows");
      }
      pty.resize(this._fd, cols, rows);
      this._cols = cols;
      this._rows = rows;
    };
    UnixTerminal2.prototype.clear = function() {};
    UnixTerminal2.prototype._sanitizeEnv = function(env) {
      delete env["TMUX"];
      delete env["TMUX_PANE"];
      delete env["STY"];
      delete env["WINDOW"];
      delete env["WINDOWID"];
      delete env["TERMCAP"];
      delete env["COLUMNS"];
      delete env["LINES"];
    };
    return UnixTerminal2;
  }(terminal_1.Terminal);
  exports.UnixTerminal = UnixTerminal;
});

// node_modules/@lydell/node-pty/index.js
var require_node_pty = __commonJS((exports) => {
  Object.defineProperty(exports, "__esModule", { value: true });
  var terminalCtor;
  if (process.platform === "win32") {
    terminalCtor = require_windowsTerminal().WindowsTerminal;
  } else {
    terminalCtor = require_unixTerminal().UnixTerminal;
  }
  function spawn(file, args, opt) {
    return new terminalCtor(file, args, opt);
  }
  exports.spawn = spawn;
  function fork(file, args, opt) {
    return new terminalCtor(file, args, opt);
  }
  exports.fork = fork;
  function createTerminal(file, args, opt) {
    return new terminalCtor(file, args, opt);
  }
  exports.createTerminal = createTerminal;
  function open(options) {
    return terminalCtor.open(options);
  }
  exports.open = open;
});

// node_modules/ws/lib/constants.js
var require_constants = __commonJS((exports, module) => {
  var BINARY_TYPES = ["nodebuffer", "arraybuffer", "fragments"];
  var hasBlob = typeof Blob !== "undefined";
  if (hasBlob)
    BINARY_TYPES.push("blob");
  module.exports = {
    BINARY_TYPES,
    CLOSE_TIMEOUT: 30000,
    EMPTY_BUFFER: Buffer.alloc(0),
    GUID: "258EAFA5-E914-47DA-95CA-C5AB0DC85B11",
    hasBlob,
    kForOnEventAttribute: Symbol("kIsForOnEventAttribute"),
    kListener: Symbol("kListener"),
    kStatusCode: Symbol("status-code"),
    kWebSocket: Symbol("websocket"),
    NOOP: () => {}
  };
});

// node_modules/ws/lib/buffer-util.js
var require_buffer_util = __commonJS((exports, module) => {
  var { EMPTY_BUFFER } = require_constants();
  var FastBuffer = Buffer[Symbol.species];
  function concat(list, totalLength) {
    if (list.length === 0)
      return EMPTY_BUFFER;
    if (list.length === 1)
      return list[0];
    const target = Buffer.allocUnsafe(totalLength);
    let offset = 0;
    for (let i = 0;i < list.length; i++) {
      const buf = list[i];
      target.set(buf, offset);
      offset += buf.length;
    }
    if (offset < totalLength) {
      return new FastBuffer(target.buffer, target.byteOffset, offset);
    }
    return target;
  }
  function _mask(source, mask, output, offset, length) {
    for (let i = 0;i < length; i++) {
      output[offset + i] = source[i] ^ mask[i & 3];
    }
  }
  function _unmask(buffer, mask) {
    for (let i = 0;i < buffer.length; i++) {
      buffer[i] ^= mask[i & 3];
    }
  }
  function toArrayBuffer(buf) {
    if (buf.length === buf.buffer.byteLength) {
      return buf.buffer;
    }
    return buf.buffer.slice(buf.byteOffset, buf.byteOffset + buf.length);
  }
  function toBuffer(data) {
    toBuffer.readOnly = true;
    if (Buffer.isBuffer(data))
      return data;
    let buf;
    if (data instanceof ArrayBuffer) {
      buf = new FastBuffer(data);
    } else if (ArrayBuffer.isView(data)) {
      buf = new FastBuffer(data.buffer, data.byteOffset, data.byteLength);
    } else {
      buf = Buffer.from(data);
      toBuffer.readOnly = false;
    }
    return buf;
  }
  module.exports = {
    concat,
    mask: _mask,
    toArrayBuffer,
    toBuffer,
    unmask: _unmask
  };
  if (!process.env.WS_NO_BUFFER_UTIL) {
    try {
      const bufferUtil = (()=>{throw new Error("Cannot require module "+"bufferutil");})();
      module.exports.mask = function(source, mask, output, offset, length) {
        if (length < 48)
          _mask(source, mask, output, offset, length);
        else
          bufferUtil.mask(source, mask, output, offset, length);
      };
      module.exports.unmask = function(buffer, mask) {
        if (buffer.length < 32)
          _unmask(buffer, mask);
        else
          bufferUtil.unmask(buffer, mask);
      };
    } catch (e) {}
  }
});

// node_modules/ws/lib/limiter.js
var require_limiter = __commonJS((exports, module) => {
  var kDone = Symbol("kDone");
  var kRun = Symbol("kRun");

  class Limiter {
    constructor(concurrency) {
      this[kDone] = () => {
        this.pending--;
        this[kRun]();
      };
      this.concurrency = concurrency || Infinity;
      this.jobs = [];
      this.pending = 0;
    }
    add(job) {
      this.jobs.push(job);
      this[kRun]();
    }
    [kRun]() {
      if (this.pending === this.concurrency)
        return;
      if (this.jobs.length) {
        const job = this.jobs.shift();
        this.pending++;
        job(this[kDone]);
      }
    }
  }
  module.exports = Limiter;
});

// node_modules/ws/lib/permessage-deflate.js
var require_permessage_deflate = __commonJS((exports, module) => {
  var zlib = __require("zlib");
  var bufferUtil = require_buffer_util();
  var Limiter = require_limiter();
  var { kStatusCode } = require_constants();
  var FastBuffer = Buffer[Symbol.species];
  var TRAILER = Buffer.from([0, 0, 255, 255]);
  var kPerMessageDeflate = Symbol("permessage-deflate");
  var kTotalLength = Symbol("total-length");
  var kCallback = Symbol("callback");
  var kBuffers = Symbol("buffers");
  var kError = Symbol("error");
  var zlibLimiter;

  class PerMessageDeflate {
    constructor(options, isServer, maxPayload) {
      this._maxPayload = maxPayload | 0;
      this._options = options || {};
      this._threshold = this._options.threshold !== undefined ? this._options.threshold : 1024;
      this._isServer = !!isServer;
      this._deflate = null;
      this._inflate = null;
      this.params = null;
      if (!zlibLimiter) {
        const concurrency = this._options.concurrencyLimit !== undefined ? this._options.concurrencyLimit : 10;
        zlibLimiter = new Limiter(concurrency);
      }
    }
    static get extensionName() {
      return "permessage-deflate";
    }
    offer() {
      const params = {};
      if (this._options.serverNoContextTakeover) {
        params.server_no_context_takeover = true;
      }
      if (this._options.clientNoContextTakeover) {
        params.client_no_context_takeover = true;
      }
      if (this._options.serverMaxWindowBits) {
        params.server_max_window_bits = this._options.serverMaxWindowBits;
      }
      if (this._options.clientMaxWindowBits) {
        params.client_max_window_bits = this._options.clientMaxWindowBits;
      } else if (this._options.clientMaxWindowBits == null) {
        params.client_max_window_bits = true;
      }
      return params;
    }
    accept(configurations) {
      configurations = this.normalizeParams(configurations);
      this.params = this._isServer ? this.acceptAsServer(configurations) : this.acceptAsClient(configurations);
      return this.params;
    }
    cleanup() {
      if (this._inflate) {
        this._inflate.close();
        this._inflate = null;
      }
      if (this._deflate) {
        const callback = this._deflate[kCallback];
        this._deflate.close();
        this._deflate = null;
        if (callback) {
          callback(new Error("The deflate stream was closed while data was being processed"));
        }
      }
    }
    acceptAsServer(offers) {
      const opts = this._options;
      const accepted = offers.find((params) => {
        if (opts.serverNoContextTakeover === false && params.server_no_context_takeover || params.server_max_window_bits && (opts.serverMaxWindowBits === false || typeof opts.serverMaxWindowBits === "number" && opts.serverMaxWindowBits > params.server_max_window_bits) || typeof opts.clientMaxWindowBits === "number" && !params.client_max_window_bits) {
          return false;
        }
        return true;
      });
      if (!accepted) {
        throw new Error("None of the extension offers can be accepted");
      }
      if (opts.serverNoContextTakeover) {
        accepted.server_no_context_takeover = true;
      }
      if (opts.clientNoContextTakeover) {
        accepted.client_no_context_takeover = true;
      }
      if (typeof opts.serverMaxWindowBits === "number") {
        accepted.server_max_window_bits = opts.serverMaxWindowBits;
      }
      if (typeof opts.clientMaxWindowBits === "number") {
        accepted.client_max_window_bits = opts.clientMaxWindowBits;
      } else if (accepted.client_max_window_bits === true || opts.clientMaxWindowBits === false) {
        delete accepted.client_max_window_bits;
      }
      return accepted;
    }
    acceptAsClient(response) {
      const params = response[0];
      if (this._options.clientNoContextTakeover === false && params.client_no_context_takeover) {
        throw new Error('Unexpected parameter "client_no_context_takeover"');
      }
      if (!params.client_max_window_bits) {
        if (typeof this._options.clientMaxWindowBits === "number") {
          params.client_max_window_bits = this._options.clientMaxWindowBits;
        }
      } else if (this._options.clientMaxWindowBits === false || typeof this._options.clientMaxWindowBits === "number" && params.client_max_window_bits > this._options.clientMaxWindowBits) {
        throw new Error('Unexpected or invalid parameter "client_max_window_bits"');
      }
      return params;
    }
    normalizeParams(configurations) {
      configurations.forEach((params) => {
        Object.keys(params).forEach((key) => {
          let value = params[key];
          if (value.length > 1) {
            throw new Error(`Parameter "${key}" must have only a single value`);
          }
          value = value[0];
          if (key === "client_max_window_bits") {
            if (value !== true) {
              const num = +value;
              if (!Number.isInteger(num) || num < 8 || num > 15) {
                throw new TypeError(`Invalid value for parameter "${key}": ${value}`);
              }
              value = num;
            } else if (!this._isServer) {
              throw new TypeError(`Invalid value for parameter "${key}": ${value}`);
            }
          } else if (key === "server_max_window_bits") {
            const num = +value;
            if (!Number.isInteger(num) || num < 8 || num > 15) {
              throw new TypeError(`Invalid value for parameter "${key}": ${value}`);
            }
            value = num;
          } else if (key === "client_no_context_takeover" || key === "server_no_context_takeover") {
            if (value !== true) {
              throw new TypeError(`Invalid value for parameter "${key}": ${value}`);
            }
          } else {
            throw new Error(`Unknown parameter "${key}"`);
          }
          params[key] = value;
        });
      });
      return configurations;
    }
    decompress(data, fin, callback) {
      zlibLimiter.add((done) => {
        this._decompress(data, fin, (err, result) => {
          done();
          callback(err, result);
        });
      });
    }
    compress(data, fin, callback) {
      zlibLimiter.add((done) => {
        this._compress(data, fin, (err, result) => {
          done();
          callback(err, result);
        });
      });
    }
    _decompress(data, fin, callback) {
      const endpoint = this._isServer ? "client" : "server";
      if (!this._inflate) {
        const key = `${endpoint}_max_window_bits`;
        const windowBits = typeof this.params[key] !== "number" ? zlib.Z_DEFAULT_WINDOWBITS : this.params[key];
        this._inflate = zlib.createInflateRaw({
          ...this._options.zlibInflateOptions,
          windowBits
        });
        this._inflate[kPerMessageDeflate] = this;
        this._inflate[kTotalLength] = 0;
        this._inflate[kBuffers] = [];
        this._inflate.on("error", inflateOnError);
        this._inflate.on("data", inflateOnData);
      }
      this._inflate[kCallback] = callback;
      this._inflate.write(data);
      if (fin)
        this._inflate.write(TRAILER);
      this._inflate.flush(() => {
        const err = this._inflate[kError];
        if (err) {
          this._inflate.close();
          this._inflate = null;
          callback(err);
          return;
        }
        const data2 = bufferUtil.concat(this._inflate[kBuffers], this._inflate[kTotalLength]);
        if (this._inflate._readableState.endEmitted) {
          this._inflate.close();
          this._inflate = null;
        } else {
          this._inflate[kTotalLength] = 0;
          this._inflate[kBuffers] = [];
          if (fin && this.params[`${endpoint}_no_context_takeover`]) {
            this._inflate.reset();
          }
        }
        callback(null, data2);
      });
    }
    _compress(data, fin, callback) {
      const endpoint = this._isServer ? "server" : "client";
      if (!this._deflate) {
        const key = `${endpoint}_max_window_bits`;
        const windowBits = typeof this.params[key] !== "number" ? zlib.Z_DEFAULT_WINDOWBITS : this.params[key];
        this._deflate = zlib.createDeflateRaw({
          ...this._options.zlibDeflateOptions,
          windowBits
        });
        this._deflate[kTotalLength] = 0;
        this._deflate[kBuffers] = [];
        this._deflate.on("data", deflateOnData);
      }
      this._deflate[kCallback] = callback;
      this._deflate.write(data);
      this._deflate.flush(zlib.Z_SYNC_FLUSH, () => {
        if (!this._deflate) {
          return;
        }
        let data2 = bufferUtil.concat(this._deflate[kBuffers], this._deflate[kTotalLength]);
        if (fin) {
          data2 = new FastBuffer(data2.buffer, data2.byteOffset, data2.length - 4);
        }
        this._deflate[kCallback] = null;
        this._deflate[kTotalLength] = 0;
        this._deflate[kBuffers] = [];
        if (fin && this.params[`${endpoint}_no_context_takeover`]) {
          this._deflate.reset();
        }
        callback(null, data2);
      });
    }
  }
  module.exports = PerMessageDeflate;
  function deflateOnData(chunk) {
    this[kBuffers].push(chunk);
    this[kTotalLength] += chunk.length;
  }
  function inflateOnData(chunk) {
    this[kTotalLength] += chunk.length;
    if (this[kPerMessageDeflate]._maxPayload < 1 || this[kTotalLength] <= this[kPerMessageDeflate]._maxPayload) {
      this[kBuffers].push(chunk);
      return;
    }
    this[kError] = new RangeError("Max payload size exceeded");
    this[kError].code = "WS_ERR_UNSUPPORTED_MESSAGE_LENGTH";
    this[kError][kStatusCode] = 1009;
    this.removeListener("data", inflateOnData);
    this.reset();
  }
  function inflateOnError(err) {
    this[kPerMessageDeflate]._inflate = null;
    if (this[kError]) {
      this[kCallback](this[kError]);
      return;
    }
    err[kStatusCode] = 1007;
    this[kCallback](err);
  }
});

// node_modules/ws/lib/validation.js
var require_validation = __commonJS((exports, module) => {
  var { isUtf8 } = __require("buffer");
  var { hasBlob } = require_constants();
  var tokenChars = [
    0,
    0,
    0,
    0,
    0,
    0,
    0,
    0,
    0,
    0,
    0,
    0,
    0,
    0,
    0,
    0,
    0,
    0,
    0,
    0,
    0,
    0,
    0,
    0,
    0,
    0,
    0,
    0,
    0,
    0,
    0,
    0,
    0,
    1,
    0,
    1,
    1,
    1,
    1,
    1,
    0,
    0,
    1,
    1,
    0,
    1,
    1,
    0,
    1,
    1,
    1,
    1,
    1,
    1,
    1,
    1,
    1,
    1,
    0,
    0,
    0,
    0,
    0,
    0,
    0,
    1,
    1,
    1,
    1,
    1,
    1,
    1,
    1,
    1,
    1,
    1,
    1,
    1,
    1,
    1,
    1,
    1,
    1,
    1,
    1,
    1,
    1,
    1,
    1,
    1,
    1,
    0,
    0,
    0,
    1,
    1,
    1,
    1,
    1,
    1,
    1,
    1,
    1,
    1,
    1,
    1,
    1,
    1,
    1,
    1,
    1,
    1,
    1,
    1,
    1,
    1,
    1,
    1,
    1,
    1,
    1,
    1,
    1,
    0,
    1,
    0,
    1,
    0
  ];
  function isValidStatusCode(code) {
    return code >= 1000 && code <= 1014 && code !== 1004 && code !== 1005 && code !== 1006 || code >= 3000 && code <= 4999;
  }
  function _isValidUTF8(buf) {
    const len = buf.length;
    let i = 0;
    while (i < len) {
      if ((buf[i] & 128) === 0) {
        i++;
      } else if ((buf[i] & 224) === 192) {
        if (i + 1 === len || (buf[i + 1] & 192) !== 128 || (buf[i] & 254) === 192) {
          return false;
        }
        i += 2;
      } else if ((buf[i] & 240) === 224) {
        if (i + 2 >= len || (buf[i + 1] & 192) !== 128 || (buf[i + 2] & 192) !== 128 || buf[i] === 224 && (buf[i + 1] & 224) === 128 || buf[i] === 237 && (buf[i + 1] & 224) === 160) {
          return false;
        }
        i += 3;
      } else if ((buf[i] & 248) === 240) {
        if (i + 3 >= len || (buf[i + 1] & 192) !== 128 || (buf[i + 2] & 192) !== 128 || (buf[i + 3] & 192) !== 128 || buf[i] === 240 && (buf[i + 1] & 240) === 128 || buf[i] === 244 && buf[i + 1] > 143 || buf[i] > 244) {
          return false;
        }
        i += 4;
      } else {
        return false;
      }
    }
    return true;
  }
  function isBlob(value) {
    return hasBlob && typeof value === "object" && typeof value.arrayBuffer === "function" && typeof value.type === "string" && typeof value.stream === "function" && (value[Symbol.toStringTag] === "Blob" || value[Symbol.toStringTag] === "File");
  }
  module.exports = {
    isBlob,
    isValidStatusCode,
    isValidUTF8: _isValidUTF8,
    tokenChars
  };
  if (isUtf8) {
    module.exports.isValidUTF8 = function(buf) {
      return buf.length < 24 ? _isValidUTF8(buf) : isUtf8(buf);
    };
  } else if (!process.env.WS_NO_UTF_8_VALIDATE) {
    try {
      const isValidUTF8 = (()=>{throw new Error("Cannot require module "+"utf-8-validate");})();
      module.exports.isValidUTF8 = function(buf) {
        return buf.length < 32 ? _isValidUTF8(buf) : isValidUTF8(buf);
      };
    } catch (e) {}
  }
});

// node_modules/ws/lib/receiver.js
var require_receiver = __commonJS((exports, module) => {
  var { Writable } = __require("stream");
  var PerMessageDeflate = require_permessage_deflate();
  var {
    BINARY_TYPES,
    EMPTY_BUFFER,
    kStatusCode,
    kWebSocket
  } = require_constants();
  var { concat, toArrayBuffer, unmask } = require_buffer_util();
  var { isValidStatusCode, isValidUTF8 } = require_validation();
  var FastBuffer = Buffer[Symbol.species];
  var GET_INFO = 0;
  var GET_PAYLOAD_LENGTH_16 = 1;
  var GET_PAYLOAD_LENGTH_64 = 2;
  var GET_MASK = 3;
  var GET_DATA = 4;
  var INFLATING = 5;
  var DEFER_EVENT = 6;

  class Receiver extends Writable {
    constructor(options = {}) {
      super();
      this._allowSynchronousEvents = options.allowSynchronousEvents !== undefined ? options.allowSynchronousEvents : true;
      this._binaryType = options.binaryType || BINARY_TYPES[0];
      this._extensions = options.extensions || {};
      this._isServer = !!options.isServer;
      this._maxPayload = options.maxPayload | 0;
      this._skipUTF8Validation = !!options.skipUTF8Validation;
      this[kWebSocket] = undefined;
      this._bufferedBytes = 0;
      this._buffers = [];
      this._compressed = false;
      this._payloadLength = 0;
      this._mask = undefined;
      this._fragmented = 0;
      this._masked = false;
      this._fin = false;
      this._opcode = 0;
      this._totalPayloadLength = 0;
      this._messageLength = 0;
      this._fragments = [];
      this._errored = false;
      this._loop = false;
      this._state = GET_INFO;
    }
    _write(chunk, encoding, cb) {
      if (this._opcode === 8 && this._state == GET_INFO)
        return cb();
      this._bufferedBytes += chunk.length;
      this._buffers.push(chunk);
      this.startLoop(cb);
    }
    consume(n) {
      this._bufferedBytes -= n;
      if (n === this._buffers[0].length)
        return this._buffers.shift();
      if (n < this._buffers[0].length) {
        const buf = this._buffers[0];
        this._buffers[0] = new FastBuffer(buf.buffer, buf.byteOffset + n, buf.length - n);
        return new FastBuffer(buf.buffer, buf.byteOffset, n);
      }
      const dst = Buffer.allocUnsafe(n);
      do {
        const buf = this._buffers[0];
        const offset = dst.length - n;
        if (n >= buf.length) {
          dst.set(this._buffers.shift(), offset);
        } else {
          dst.set(new Uint8Array(buf.buffer, buf.byteOffset, n), offset);
          this._buffers[0] = new FastBuffer(buf.buffer, buf.byteOffset + n, buf.length - n);
        }
        n -= buf.length;
      } while (n > 0);
      return dst;
    }
    startLoop(cb) {
      this._loop = true;
      do {
        switch (this._state) {
          case GET_INFO:
            this.getInfo(cb);
            break;
          case GET_PAYLOAD_LENGTH_16:
            this.getPayloadLength16(cb);
            break;
          case GET_PAYLOAD_LENGTH_64:
            this.getPayloadLength64(cb);
            break;
          case GET_MASK:
            this.getMask();
            break;
          case GET_DATA:
            this.getData(cb);
            break;
          case INFLATING:
          case DEFER_EVENT:
            this._loop = false;
            return;
        }
      } while (this._loop);
      if (!this._errored)
        cb();
    }
    getInfo(cb) {
      if (this._bufferedBytes < 2) {
        this._loop = false;
        return;
      }
      const buf = this.consume(2);
      if ((buf[0] & 48) !== 0) {
        const error = this.createError(RangeError, "RSV2 and RSV3 must be clear", true, 1002, "WS_ERR_UNEXPECTED_RSV_2_3");
        cb(error);
        return;
      }
      const compressed = (buf[0] & 64) === 64;
      if (compressed && !this._extensions[PerMessageDeflate.extensionName]) {
        const error = this.createError(RangeError, "RSV1 must be clear", true, 1002, "WS_ERR_UNEXPECTED_RSV_1");
        cb(error);
        return;
      }
      this._fin = (buf[0] & 128) === 128;
      this._opcode = buf[0] & 15;
      this._payloadLength = buf[1] & 127;
      if (this._opcode === 0) {
        if (compressed) {
          const error = this.createError(RangeError, "RSV1 must be clear", true, 1002, "WS_ERR_UNEXPECTED_RSV_1");
          cb(error);
          return;
        }
        if (!this._fragmented) {
          const error = this.createError(RangeError, "invalid opcode 0", true, 1002, "WS_ERR_INVALID_OPCODE");
          cb(error);
          return;
        }
        this._opcode = this._fragmented;
      } else if (this._opcode === 1 || this._opcode === 2) {
        if (this._fragmented) {
          const error = this.createError(RangeError, `invalid opcode ${this._opcode}`, true, 1002, "WS_ERR_INVALID_OPCODE");
          cb(error);
          return;
        }
        this._compressed = compressed;
      } else if (this._opcode > 7 && this._opcode < 11) {
        if (!this._fin) {
          const error = this.createError(RangeError, "FIN must be set", true, 1002, "WS_ERR_EXPECTED_FIN");
          cb(error);
          return;
        }
        if (compressed) {
          const error = this.createError(RangeError, "RSV1 must be clear", true, 1002, "WS_ERR_UNEXPECTED_RSV_1");
          cb(error);
          return;
        }
        if (this._payloadLength > 125 || this._opcode === 8 && this._payloadLength === 1) {
          const error = this.createError(RangeError, `invalid payload length ${this._payloadLength}`, true, 1002, "WS_ERR_INVALID_CONTROL_PAYLOAD_LENGTH");
          cb(error);
          return;
        }
      } else {
        const error = this.createError(RangeError, `invalid opcode ${this._opcode}`, true, 1002, "WS_ERR_INVALID_OPCODE");
        cb(error);
        return;
      }
      if (!this._fin && !this._fragmented)
        this._fragmented = this._opcode;
      this._masked = (buf[1] & 128) === 128;
      if (this._isServer) {
        if (!this._masked) {
          const error = this.createError(RangeError, "MASK must be set", true, 1002, "WS_ERR_EXPECTED_MASK");
          cb(error);
          return;
        }
      } else if (this._masked) {
        const error = this.createError(RangeError, "MASK must be clear", true, 1002, "WS_ERR_UNEXPECTED_MASK");
        cb(error);
        return;
      }
      if (this._payloadLength === 126)
        this._state = GET_PAYLOAD_LENGTH_16;
      else if (this._payloadLength === 127)
        this._state = GET_PAYLOAD_LENGTH_64;
      else
        this.haveLength(cb);
    }
    getPayloadLength16(cb) {
      if (this._bufferedBytes < 2) {
        this._loop = false;
        return;
      }
      this._payloadLength = this.consume(2).readUInt16BE(0);
      this.haveLength(cb);
    }
    getPayloadLength64(cb) {
      if (this._bufferedBytes < 8) {
        this._loop = false;
        return;
      }
      const buf = this.consume(8);
      const num = buf.readUInt32BE(0);
      if (num > Math.pow(2, 53 - 32) - 1) {
        const error = this.createError(RangeError, "Unsupported WebSocket frame: payload length > 2^53 - 1", false, 1009, "WS_ERR_UNSUPPORTED_DATA_PAYLOAD_LENGTH");
        cb(error);
        return;
      }
      this._payloadLength = num * Math.pow(2, 32) + buf.readUInt32BE(4);
      this.haveLength(cb);
    }
    haveLength(cb) {
      if (this._payloadLength && this._opcode < 8) {
        this._totalPayloadLength += this._payloadLength;
        if (this._totalPayloadLength > this._maxPayload && this._maxPayload > 0) {
          const error = this.createError(RangeError, "Max payload size exceeded", false, 1009, "WS_ERR_UNSUPPORTED_MESSAGE_LENGTH");
          cb(error);
          return;
        }
      }
      if (this._masked)
        this._state = GET_MASK;
      else
        this._state = GET_DATA;
    }
    getMask() {
      if (this._bufferedBytes < 4) {
        this._loop = false;
        return;
      }
      this._mask = this.consume(4);
      this._state = GET_DATA;
    }
    getData(cb) {
      let data = EMPTY_BUFFER;
      if (this._payloadLength) {
        if (this._bufferedBytes < this._payloadLength) {
          this._loop = false;
          return;
        }
        data = this.consume(this._payloadLength);
        if (this._masked && (this._mask[0] | this._mask[1] | this._mask[2] | this._mask[3]) !== 0) {
          unmask(data, this._mask);
        }
      }
      if (this._opcode > 7) {
        this.controlMessage(data, cb);
        return;
      }
      if (this._compressed) {
        this._state = INFLATING;
        this.decompress(data, cb);
        return;
      }
      if (data.length) {
        this._messageLength = this._totalPayloadLength;
        this._fragments.push(data);
      }
      this.dataMessage(cb);
    }
    decompress(data, cb) {
      const perMessageDeflate = this._extensions[PerMessageDeflate.extensionName];
      perMessageDeflate.decompress(data, this._fin, (err, buf) => {
        if (err)
          return cb(err);
        if (buf.length) {
          this._messageLength += buf.length;
          if (this._messageLength > this._maxPayload && this._maxPayload > 0) {
            const error = this.createError(RangeError, "Max payload size exceeded", false, 1009, "WS_ERR_UNSUPPORTED_MESSAGE_LENGTH");
            cb(error);
            return;
          }
          this._fragments.push(buf);
        }
        this.dataMessage(cb);
        if (this._state === GET_INFO)
          this.startLoop(cb);
      });
    }
    dataMessage(cb) {
      if (!this._fin) {
        this._state = GET_INFO;
        return;
      }
      const messageLength = this._messageLength;
      const fragments = this._fragments;
      this._totalPayloadLength = 0;
      this._messageLength = 0;
      this._fragmented = 0;
      this._fragments = [];
      if (this._opcode === 2) {
        let data;
        if (this._binaryType === "nodebuffer") {
          data = concat(fragments, messageLength);
        } else if (this._binaryType === "arraybuffer") {
          data = toArrayBuffer(concat(fragments, messageLength));
        } else if (this._binaryType === "blob") {
          data = new Blob(fragments);
        } else {
          data = fragments;
        }
        if (this._allowSynchronousEvents) {
          this.emit("message", data, true);
          this._state = GET_INFO;
        } else {
          this._state = DEFER_EVENT;
          setImmediate(() => {
            this.emit("message", data, true);
            this._state = GET_INFO;
            this.startLoop(cb);
          });
        }
      } else {
        const buf = concat(fragments, messageLength);
        if (!this._skipUTF8Validation && !isValidUTF8(buf)) {
          const error = this.createError(Error, "invalid UTF-8 sequence", true, 1007, "WS_ERR_INVALID_UTF8");
          cb(error);
          return;
        }
        if (this._state === INFLATING || this._allowSynchronousEvents) {
          this.emit("message", buf, false);
          this._state = GET_INFO;
        } else {
          this._state = DEFER_EVENT;
          setImmediate(() => {
            this.emit("message", buf, false);
            this._state = GET_INFO;
            this.startLoop(cb);
          });
        }
      }
    }
    controlMessage(data, cb) {
      if (this._opcode === 8) {
        if (data.length === 0) {
          this._loop = false;
          this.emit("conclude", 1005, EMPTY_BUFFER);
          this.end();
        } else {
          const code = data.readUInt16BE(0);
          if (!isValidStatusCode(code)) {
            const error = this.createError(RangeError, `invalid status code ${code}`, true, 1002, "WS_ERR_INVALID_CLOSE_CODE");
            cb(error);
            return;
          }
          const buf = new FastBuffer(data.buffer, data.byteOffset + 2, data.length - 2);
          if (!this._skipUTF8Validation && !isValidUTF8(buf)) {
            const error = this.createError(Error, "invalid UTF-8 sequence", true, 1007, "WS_ERR_INVALID_UTF8");
            cb(error);
            return;
          }
          this._loop = false;
          this.emit("conclude", code, buf);
          this.end();
        }
        this._state = GET_INFO;
        return;
      }
      if (this._allowSynchronousEvents) {
        this.emit(this._opcode === 9 ? "ping" : "pong", data);
        this._state = GET_INFO;
      } else {
        this._state = DEFER_EVENT;
        setImmediate(() => {
          this.emit(this._opcode === 9 ? "ping" : "pong", data);
          this._state = GET_INFO;
          this.startLoop(cb);
        });
      }
    }
    createError(ErrorCtor, message, prefix, statusCode, errorCode) {
      this._loop = false;
      this._errored = true;
      const err = new ErrorCtor(prefix ? `Invalid WebSocket frame: ${message}` : message);
      Error.captureStackTrace(err, this.createError);
      err.code = errorCode;
      err[kStatusCode] = statusCode;
      return err;
    }
  }
  module.exports = Receiver;
});

// node_modules/ws/lib/sender.js
var require_sender = __commonJS((exports, module) => {
  var { Duplex } = __require("stream");
  var { randomFillSync } = __require("crypto");
  var PerMessageDeflate = require_permessage_deflate();
  var { EMPTY_BUFFER, kWebSocket, NOOP } = require_constants();
  var { isBlob, isValidStatusCode } = require_validation();
  var { mask: applyMask, toBuffer } = require_buffer_util();
  var kByteLength = Symbol("kByteLength");
  var maskBuffer = Buffer.alloc(4);
  var RANDOM_POOL_SIZE = 8 * 1024;
  var randomPool;
  var randomPoolPointer = RANDOM_POOL_SIZE;
  var DEFAULT = 0;
  var DEFLATING = 1;
  var GET_BLOB_DATA = 2;

  class Sender {
    constructor(socket, extensions, generateMask) {
      this._extensions = extensions || {};
      if (generateMask) {
        this._generateMask = generateMask;
        this._maskBuffer = Buffer.alloc(4);
      }
      this._socket = socket;
      this._firstFragment = true;
      this._compress = false;
      this._bufferedBytes = 0;
      this._queue = [];
      this._state = DEFAULT;
      this.onerror = NOOP;
      this[kWebSocket] = undefined;
    }
    static frame(data, options) {
      let mask;
      let merge = false;
      let offset = 2;
      let skipMasking = false;
      if (options.mask) {
        mask = options.maskBuffer || maskBuffer;
        if (options.generateMask) {
          options.generateMask(mask);
        } else {
          if (randomPoolPointer === RANDOM_POOL_SIZE) {
            if (randomPool === undefined) {
              randomPool = Buffer.alloc(RANDOM_POOL_SIZE);
            }
            randomFillSync(randomPool, 0, RANDOM_POOL_SIZE);
            randomPoolPointer = 0;
          }
          mask[0] = randomPool[randomPoolPointer++];
          mask[1] = randomPool[randomPoolPointer++];
          mask[2] = randomPool[randomPoolPointer++];
          mask[3] = randomPool[randomPoolPointer++];
        }
        skipMasking = (mask[0] | mask[1] | mask[2] | mask[3]) === 0;
        offset = 6;
      }
      let dataLength;
      if (typeof data === "string") {
        if ((!options.mask || skipMasking) && options[kByteLength] !== undefined) {
          dataLength = options[kByteLength];
        } else {
          data = Buffer.from(data);
          dataLength = data.length;
        }
      } else {
        dataLength = data.length;
        merge = options.mask && options.readOnly && !skipMasking;
      }
      let payloadLength = dataLength;
      if (dataLength >= 65536) {
        offset += 8;
        payloadLength = 127;
      } else if (dataLength > 125) {
        offset += 2;
        payloadLength = 126;
      }
      const target = Buffer.allocUnsafe(merge ? dataLength + offset : offset);
      target[0] = options.fin ? options.opcode | 128 : options.opcode;
      if (options.rsv1)
        target[0] |= 64;
      target[1] = payloadLength;
      if (payloadLength === 126) {
        target.writeUInt16BE(dataLength, 2);
      } else if (payloadLength === 127) {
        target[2] = target[3] = 0;
        target.writeUIntBE(dataLength, 4, 6);
      }
      if (!options.mask)
        return [target, data];
      target[1] |= 128;
      target[offset - 4] = mask[0];
      target[offset - 3] = mask[1];
      target[offset - 2] = mask[2];
      target[offset - 1] = mask[3];
      if (skipMasking)
        return [target, data];
      if (merge) {
        applyMask(data, mask, target, offset, dataLength);
        return [target];
      }
      applyMask(data, mask, data, 0, dataLength);
      return [target, data];
    }
    close(code, data, mask, cb) {
      let buf;
      if (code === undefined) {
        buf = EMPTY_BUFFER;
      } else if (typeof code !== "number" || !isValidStatusCode(code)) {
        throw new TypeError("First argument must be a valid error code number");
      } else if (data === undefined || !data.length) {
        buf = Buffer.allocUnsafe(2);
        buf.writeUInt16BE(code, 0);
      } else {
        const length = Buffer.byteLength(data);
        if (length > 123) {
          throw new RangeError("The message must not be greater than 123 bytes");
        }
        buf = Buffer.allocUnsafe(2 + length);
        buf.writeUInt16BE(code, 0);
        if (typeof data === "string") {
          buf.write(data, 2);
        } else {
          buf.set(data, 2);
        }
      }
      const options = {
        [kByteLength]: buf.length,
        fin: true,
        generateMask: this._generateMask,
        mask,
        maskBuffer: this._maskBuffer,
        opcode: 8,
        readOnly: false,
        rsv1: false
      };
      if (this._state !== DEFAULT) {
        this.enqueue([this.dispatch, buf, false, options, cb]);
      } else {
        this.sendFrame(Sender.frame(buf, options), cb);
      }
    }
    ping(data, mask, cb) {
      let byteLength;
      let readOnly;
      if (typeof data === "string") {
        byteLength = Buffer.byteLength(data);
        readOnly = false;
      } else if (isBlob(data)) {
        byteLength = data.size;
        readOnly = false;
      } else {
        data = toBuffer(data);
        byteLength = data.length;
        readOnly = toBuffer.readOnly;
      }
      if (byteLength > 125) {
        throw new RangeError("The data size must not be greater than 125 bytes");
      }
      const options = {
        [kByteLength]: byteLength,
        fin: true,
        generateMask: this._generateMask,
        mask,
        maskBuffer: this._maskBuffer,
        opcode: 9,
        readOnly,
        rsv1: false
      };
      if (isBlob(data)) {
        if (this._state !== DEFAULT) {
          this.enqueue([this.getBlobData, data, false, options, cb]);
        } else {
          this.getBlobData(data, false, options, cb);
        }
      } else if (this._state !== DEFAULT) {
        this.enqueue([this.dispatch, data, false, options, cb]);
      } else {
        this.sendFrame(Sender.frame(data, options), cb);
      }
    }
    pong(data, mask, cb) {
      let byteLength;
      let readOnly;
      if (typeof data === "string") {
        byteLength = Buffer.byteLength(data);
        readOnly = false;
      } else if (isBlob(data)) {
        byteLength = data.size;
        readOnly = false;
      } else {
        data = toBuffer(data);
        byteLength = data.length;
        readOnly = toBuffer.readOnly;
      }
      if (byteLength > 125) {
        throw new RangeError("The data size must not be greater than 125 bytes");
      }
      const options = {
        [kByteLength]: byteLength,
        fin: true,
        generateMask: this._generateMask,
        mask,
        maskBuffer: this._maskBuffer,
        opcode: 10,
        readOnly,
        rsv1: false
      };
      if (isBlob(data)) {
        if (this._state !== DEFAULT) {
          this.enqueue([this.getBlobData, data, false, options, cb]);
        } else {
          this.getBlobData(data, false, options, cb);
        }
      } else if (this._state !== DEFAULT) {
        this.enqueue([this.dispatch, data, false, options, cb]);
      } else {
        this.sendFrame(Sender.frame(data, options), cb);
      }
    }
    send(data, options, cb) {
      const perMessageDeflate = this._extensions[PerMessageDeflate.extensionName];
      let opcode = options.binary ? 2 : 1;
      let rsv1 = options.compress;
      let byteLength;
      let readOnly;
      if (typeof data === "string") {
        byteLength = Buffer.byteLength(data);
        readOnly = false;
      } else if (isBlob(data)) {
        byteLength = data.size;
        readOnly = false;
      } else {
        data = toBuffer(data);
        byteLength = data.length;
        readOnly = toBuffer.readOnly;
      }
      if (this._firstFragment) {
        this._firstFragment = false;
        if (rsv1 && perMessageDeflate && perMessageDeflate.params[perMessageDeflate._isServer ? "server_no_context_takeover" : "client_no_context_takeover"]) {
          rsv1 = byteLength >= perMessageDeflate._threshold;
        }
        this._compress = rsv1;
      } else {
        rsv1 = false;
        opcode = 0;
      }
      if (options.fin)
        this._firstFragment = true;
      const opts = {
        [kByteLength]: byteLength,
        fin: options.fin,
        generateMask: this._generateMask,
        mask: options.mask,
        maskBuffer: this._maskBuffer,
        opcode,
        readOnly,
        rsv1
      };
      if (isBlob(data)) {
        if (this._state !== DEFAULT) {
          this.enqueue([this.getBlobData, data, this._compress, opts, cb]);
        } else {
          this.getBlobData(data, this._compress, opts, cb);
        }
      } else if (this._state !== DEFAULT) {
        this.enqueue([this.dispatch, data, this._compress, opts, cb]);
      } else {
        this.dispatch(data, this._compress, opts, cb);
      }
    }
    getBlobData(blob, compress, options, cb) {
      this._bufferedBytes += options[kByteLength];
      this._state = GET_BLOB_DATA;
      blob.arrayBuffer().then((arrayBuffer) => {
        if (this._socket.destroyed) {
          const err = new Error("The socket was closed while the blob was being read");
          process.nextTick(callCallbacks, this, err, cb);
          return;
        }
        this._bufferedBytes -= options[kByteLength];
        const data = toBuffer(arrayBuffer);
        if (!compress) {
          this._state = DEFAULT;
          this.sendFrame(Sender.frame(data, options), cb);
          this.dequeue();
        } else {
          this.dispatch(data, compress, options, cb);
        }
      }).catch((err) => {
        process.nextTick(onError, this, err, cb);
      });
    }
    dispatch(data, compress, options, cb) {
      if (!compress) {
        this.sendFrame(Sender.frame(data, options), cb);
        return;
      }
      const perMessageDeflate = this._extensions[PerMessageDeflate.extensionName];
      this._bufferedBytes += options[kByteLength];
      this._state = DEFLATING;
      perMessageDeflate.compress(data, options.fin, (_, buf) => {
        if (this._socket.destroyed) {
          const err = new Error("The socket was closed while data was being compressed");
          callCallbacks(this, err, cb);
          return;
        }
        this._bufferedBytes -= options[kByteLength];
        this._state = DEFAULT;
        options.readOnly = false;
        this.sendFrame(Sender.frame(buf, options), cb);
        this.dequeue();
      });
    }
    dequeue() {
      while (this._state === DEFAULT && this._queue.length) {
        const params = this._queue.shift();
        this._bufferedBytes -= params[3][kByteLength];
        Reflect.apply(params[0], this, params.slice(1));
      }
    }
    enqueue(params) {
      this._bufferedBytes += params[3][kByteLength];
      this._queue.push(params);
    }
    sendFrame(list, cb) {
      if (list.length === 2) {
        this._socket.cork();
        this._socket.write(list[0]);
        this._socket.write(list[1], cb);
        this._socket.uncork();
      } else {
        this._socket.write(list[0], cb);
      }
    }
  }
  module.exports = Sender;
  function callCallbacks(sender, err, cb) {
    if (typeof cb === "function")
      cb(err);
    for (let i = 0;i < sender._queue.length; i++) {
      const params = sender._queue[i];
      const callback = params[params.length - 1];
      if (typeof callback === "function")
        callback(err);
    }
  }
  function onError(sender, err, cb) {
    callCallbacks(sender, err, cb);
    sender.onerror(err);
  }
});

// node_modules/ws/lib/event-target.js
var require_event_target = __commonJS((exports, module) => {
  var { kForOnEventAttribute, kListener } = require_constants();
  var kCode = Symbol("kCode");
  var kData = Symbol("kData");
  var kError = Symbol("kError");
  var kMessage = Symbol("kMessage");
  var kReason = Symbol("kReason");
  var kTarget = Symbol("kTarget");
  var kType = Symbol("kType");
  var kWasClean = Symbol("kWasClean");

  class Event {
    constructor(type) {
      this[kTarget] = null;
      this[kType] = type;
    }
    get target() {
      return this[kTarget];
    }
    get type() {
      return this[kType];
    }
  }
  Object.defineProperty(Event.prototype, "target", { enumerable: true });
  Object.defineProperty(Event.prototype, "type", { enumerable: true });

  class CloseEvent extends Event {
    constructor(type, options = {}) {
      super(type);
      this[kCode] = options.code === undefined ? 0 : options.code;
      this[kReason] = options.reason === undefined ? "" : options.reason;
      this[kWasClean] = options.wasClean === undefined ? false : options.wasClean;
    }
    get code() {
      return this[kCode];
    }
    get reason() {
      return this[kReason];
    }
    get wasClean() {
      return this[kWasClean];
    }
  }
  Object.defineProperty(CloseEvent.prototype, "code", { enumerable: true });
  Object.defineProperty(CloseEvent.prototype, "reason", { enumerable: true });
  Object.defineProperty(CloseEvent.prototype, "wasClean", { enumerable: true });

  class ErrorEvent extends Event {
    constructor(type, options = {}) {
      super(type);
      this[kError] = options.error === undefined ? null : options.error;
      this[kMessage] = options.message === undefined ? "" : options.message;
    }
    get error() {
      return this[kError];
    }
    get message() {
      return this[kMessage];
    }
  }
  Object.defineProperty(ErrorEvent.prototype, "error", { enumerable: true });
  Object.defineProperty(ErrorEvent.prototype, "message", { enumerable: true });

  class MessageEvent extends Event {
    constructor(type, options = {}) {
      super(type);
      this[kData] = options.data === undefined ? null : options.data;
    }
    get data() {
      return this[kData];
    }
  }
  Object.defineProperty(MessageEvent.prototype, "data", { enumerable: true });
  var EventTarget = {
    addEventListener(type, handler, options = {}) {
      for (const listener of this.listeners(type)) {
        if (!options[kForOnEventAttribute] && listener[kListener] === handler && !listener[kForOnEventAttribute]) {
          return;
        }
      }
      let wrapper;
      if (type === "message") {
        wrapper = function onMessage(data, isBinary) {
          const event = new MessageEvent("message", {
            data: isBinary ? data : data.toString()
          });
          event[kTarget] = this;
          callListener(handler, this, event);
        };
      } else if (type === "close") {
        wrapper = function onClose(code, message) {
          const event = new CloseEvent("close", {
            code,
            reason: message.toString(),
            wasClean: this._closeFrameReceived && this._closeFrameSent
          });
          event[kTarget] = this;
          callListener(handler, this, event);
        };
      } else if (type === "error") {
        wrapper = function onError(error) {
          const event = new ErrorEvent("error", {
            error,
            message: error.message
          });
          event[kTarget] = this;
          callListener(handler, this, event);
        };
      } else if (type === "open") {
        wrapper = function onOpen() {
          const event = new Event("open");
          event[kTarget] = this;
          callListener(handler, this, event);
        };
      } else {
        return;
      }
      wrapper[kForOnEventAttribute] = !!options[kForOnEventAttribute];
      wrapper[kListener] = handler;
      if (options.once) {
        this.once(type, wrapper);
      } else {
        this.on(type, wrapper);
      }
    },
    removeEventListener(type, handler) {
      for (const listener of this.listeners(type)) {
        if (listener[kListener] === handler && !listener[kForOnEventAttribute]) {
          this.removeListener(type, listener);
          break;
        }
      }
    }
  };
  module.exports = {
    CloseEvent,
    ErrorEvent,
    Event,
    EventTarget,
    MessageEvent
  };
  function callListener(listener, thisArg, event) {
    if (typeof listener === "object" && listener.handleEvent) {
      listener.handleEvent.call(listener, event);
    } else {
      listener.call(thisArg, event);
    }
  }
});

// node_modules/ws/lib/extension.js
var require_extension = __commonJS((exports, module) => {
  var { tokenChars } = require_validation();
  function push(dest, name, elem) {
    if (dest[name] === undefined)
      dest[name] = [elem];
    else
      dest[name].push(elem);
  }
  function parse(header) {
    const offers = Object.create(null);
    let params = Object.create(null);
    let mustUnescape = false;
    let isEscaping = false;
    let inQuotes = false;
    let extensionName;
    let paramName;
    let start = -1;
    let code = -1;
    let end = -1;
    let i = 0;
    for (;i < header.length; i++) {
      code = header.charCodeAt(i);
      if (extensionName === undefined) {
        if (end === -1 && tokenChars[code] === 1) {
          if (start === -1)
            start = i;
        } else if (i !== 0 && (code === 32 || code === 9)) {
          if (end === -1 && start !== -1)
            end = i;
        } else if (code === 59 || code === 44) {
          if (start === -1) {
            throw new SyntaxError(`Unexpected character at index ${i}`);
          }
          if (end === -1)
            end = i;
          const name = header.slice(start, end);
          if (code === 44) {
            push(offers, name, params);
            params = Object.create(null);
          } else {
            extensionName = name;
          }
          start = end = -1;
        } else {
          throw new SyntaxError(`Unexpected character at index ${i}`);
        }
      } else if (paramName === undefined) {
        if (end === -1 && tokenChars[code] === 1) {
          if (start === -1)
            start = i;
        } else if (code === 32 || code === 9) {
          if (end === -1 && start !== -1)
            end = i;
        } else if (code === 59 || code === 44) {
          if (start === -1) {
            throw new SyntaxError(`Unexpected character at index ${i}`);
          }
          if (end === -1)
            end = i;
          push(params, header.slice(start, end), true);
          if (code === 44) {
            push(offers, extensionName, params);
            params = Object.create(null);
            extensionName = undefined;
          }
          start = end = -1;
        } else if (code === 61 && start !== -1 && end === -1) {
          paramName = header.slice(start, i);
          start = end = -1;
        } else {
          throw new SyntaxError(`Unexpected character at index ${i}`);
        }
      } else {
        if (isEscaping) {
          if (tokenChars[code] !== 1) {
            throw new SyntaxError(`Unexpected character at index ${i}`);
          }
          if (start === -1)
            start = i;
          else if (!mustUnescape)
            mustUnescape = true;
          isEscaping = false;
        } else if (inQuotes) {
          if (tokenChars[code] === 1) {
            if (start === -1)
              start = i;
          } else if (code === 34 && start !== -1) {
            inQuotes = false;
            end = i;
          } else if (code === 92) {
            isEscaping = true;
          } else {
            throw new SyntaxError(`Unexpected character at index ${i}`);
          }
        } else if (code === 34 && header.charCodeAt(i - 1) === 61) {
          inQuotes = true;
        } else if (end === -1 && tokenChars[code] === 1) {
          if (start === -1)
            start = i;
        } else if (start !== -1 && (code === 32 || code === 9)) {
          if (end === -1)
            end = i;
        } else if (code === 59 || code === 44) {
          if (start === -1) {
            throw new SyntaxError(`Unexpected character at index ${i}`);
          }
          if (end === -1)
            end = i;
          let value = header.slice(start, end);
          if (mustUnescape) {
            value = value.replace(/\\/g, "");
            mustUnescape = false;
          }
          push(params, paramName, value);
          if (code === 44) {
            push(offers, extensionName, params);
            params = Object.create(null);
            extensionName = undefined;
          }
          paramName = undefined;
          start = end = -1;
        } else {
          throw new SyntaxError(`Unexpected character at index ${i}`);
        }
      }
    }
    if (start === -1 || inQuotes || code === 32 || code === 9) {
      throw new SyntaxError("Unexpected end of input");
    }
    if (end === -1)
      end = i;
    const token = header.slice(start, end);
    if (extensionName === undefined) {
      push(offers, token, params);
    } else {
      if (paramName === undefined) {
        push(params, token, true);
      } else if (mustUnescape) {
        push(params, paramName, token.replace(/\\/g, ""));
      } else {
        push(params, paramName, token);
      }
      push(offers, extensionName, params);
    }
    return offers;
  }
  function format(extensions) {
    return Object.keys(extensions).map((extension) => {
      let configurations = extensions[extension];
      if (!Array.isArray(configurations))
        configurations = [configurations];
      return configurations.map((params) => {
        return [extension].concat(Object.keys(params).map((k) => {
          let values = params[k];
          if (!Array.isArray(values))
            values = [values];
          return values.map((v) => v === true ? k : `${k}=${v}`).join("; ");
        })).join("; ");
      }).join(", ");
    }).join(", ");
  }
  module.exports = { format, parse };
});

// node_modules/ws/lib/websocket.js
var require_websocket = __commonJS((exports, module) => {
  var EventEmitter = __require("events");
  var https = __require("https");
  var http = __require("http");
  var net = __require("net");
  var tls = __require("tls");
  var { randomBytes, createHash } = __require("crypto");
  var { Duplex, Readable } = __require("stream");
  var { URL: URL2 } = __require("url");
  var PerMessageDeflate = require_permessage_deflate();
  var Receiver = require_receiver();
  var Sender = require_sender();
  var { isBlob } = require_validation();
  var {
    BINARY_TYPES,
    CLOSE_TIMEOUT,
    EMPTY_BUFFER,
    GUID,
    kForOnEventAttribute,
    kListener,
    kStatusCode,
    kWebSocket,
    NOOP
  } = require_constants();
  var {
    EventTarget: { addEventListener, removeEventListener }
  } = require_event_target();
  var { format, parse } = require_extension();
  var { toBuffer } = require_buffer_util();
  var kAborted = Symbol("kAborted");
  var protocolVersions = [8, 13];
  var readyStates = ["CONNECTING", "OPEN", "CLOSING", "CLOSED"];
  var subprotocolRegex = /^[!#$%&'*+\-.0-9A-Z^_`|a-z~]+$/;

  class WebSocket extends EventEmitter {
    constructor(address, protocols, options) {
      super();
      this._binaryType = BINARY_TYPES[0];
      this._closeCode = 1006;
      this._closeFrameReceived = false;
      this._closeFrameSent = false;
      this._closeMessage = EMPTY_BUFFER;
      this._closeTimer = null;
      this._errorEmitted = false;
      this._extensions = {};
      this._paused = false;
      this._protocol = "";
      this._readyState = WebSocket.CONNECTING;
      this._receiver = null;
      this._sender = null;
      this._socket = null;
      if (address !== null) {
        this._bufferedAmount = 0;
        this._isServer = false;
        this._redirects = 0;
        if (protocols === undefined) {
          protocols = [];
        } else if (!Array.isArray(protocols)) {
          if (typeof protocols === "object" && protocols !== null) {
            options = protocols;
            protocols = [];
          } else {
            protocols = [protocols];
          }
        }
        initAsClient(this, address, protocols, options);
      } else {
        this._autoPong = options.autoPong;
        this._closeTimeout = options.closeTimeout;
        this._isServer = true;
      }
    }
    get binaryType() {
      return this._binaryType;
    }
    set binaryType(type) {
      if (!BINARY_TYPES.includes(type))
        return;
      this._binaryType = type;
      if (this._receiver)
        this._receiver._binaryType = type;
    }
    get bufferedAmount() {
      if (!this._socket)
        return this._bufferedAmount;
      return this._socket._writableState.length + this._sender._bufferedBytes;
    }
    get extensions() {
      return Object.keys(this._extensions).join();
    }
    get isPaused() {
      return this._paused;
    }
    get onclose() {
      return null;
    }
    get onerror() {
      return null;
    }
    get onopen() {
      return null;
    }
    get onmessage() {
      return null;
    }
    get protocol() {
      return this._protocol;
    }
    get readyState() {
      return this._readyState;
    }
    get url() {
      return this._url;
    }
    setSocket(socket, head, options) {
      const receiver = new Receiver({
        allowSynchronousEvents: options.allowSynchronousEvents,
        binaryType: this.binaryType,
        extensions: this._extensions,
        isServer: this._isServer,
        maxPayload: options.maxPayload,
        skipUTF8Validation: options.skipUTF8Validation
      });
      const sender = new Sender(socket, this._extensions, options.generateMask);
      this._receiver = receiver;
      this._sender = sender;
      this._socket = socket;
      receiver[kWebSocket] = this;
      sender[kWebSocket] = this;
      socket[kWebSocket] = this;
      receiver.on("conclude", receiverOnConclude);
      receiver.on("drain", receiverOnDrain);
      receiver.on("error", receiverOnError);
      receiver.on("message", receiverOnMessage);
      receiver.on("ping", receiverOnPing);
      receiver.on("pong", receiverOnPong);
      sender.onerror = senderOnError;
      if (socket.setTimeout)
        socket.setTimeout(0);
      if (socket.setNoDelay)
        socket.setNoDelay();
      if (head.length > 0)
        socket.unshift(head);
      socket.on("close", socketOnClose);
      socket.on("data", socketOnData);
      socket.on("end", socketOnEnd);
      socket.on("error", socketOnError);
      this._readyState = WebSocket.OPEN;
      this.emit("open");
    }
    emitClose() {
      if (!this._socket) {
        this._readyState = WebSocket.CLOSED;
        this.emit("close", this._closeCode, this._closeMessage);
        return;
      }
      if (this._extensions[PerMessageDeflate.extensionName]) {
        this._extensions[PerMessageDeflate.extensionName].cleanup();
      }
      this._receiver.removeAllListeners();
      this._readyState = WebSocket.CLOSED;
      this.emit("close", this._closeCode, this._closeMessage);
    }
    close(code, data) {
      if (this.readyState === WebSocket.CLOSED)
        return;
      if (this.readyState === WebSocket.CONNECTING) {
        const msg = "WebSocket was closed before the connection was established";
        abortHandshake(this, this._req, msg);
        return;
      }
      if (this.readyState === WebSocket.CLOSING) {
        if (this._closeFrameSent && (this._closeFrameReceived || this._receiver._writableState.errorEmitted)) {
          this._socket.end();
        }
        return;
      }
      this._readyState = WebSocket.CLOSING;
      this._sender.close(code, data, !this._isServer, (err) => {
        if (err)
          return;
        this._closeFrameSent = true;
        if (this._closeFrameReceived || this._receiver._writableState.errorEmitted) {
          this._socket.end();
        }
      });
      setCloseTimer(this);
    }
    pause() {
      if (this.readyState === WebSocket.CONNECTING || this.readyState === WebSocket.CLOSED) {
        return;
      }
      this._paused = true;
      this._socket.pause();
    }
    ping(data, mask, cb) {
      if (this.readyState === WebSocket.CONNECTING) {
        throw new Error("WebSocket is not open: readyState 0 (CONNECTING)");
      }
      if (typeof data === "function") {
        cb = data;
        data = mask = undefined;
      } else if (typeof mask === "function") {
        cb = mask;
        mask = undefined;
      }
      if (typeof data === "number")
        data = data.toString();
      if (this.readyState !== WebSocket.OPEN) {
        sendAfterClose(this, data, cb);
        return;
      }
      if (mask === undefined)
        mask = !this._isServer;
      this._sender.ping(data || EMPTY_BUFFER, mask, cb);
    }
    pong(data, mask, cb) {
      if (this.readyState === WebSocket.CONNECTING) {
        throw new Error("WebSocket is not open: readyState 0 (CONNECTING)");
      }
      if (typeof data === "function") {
        cb = data;
        data = mask = undefined;
      } else if (typeof mask === "function") {
        cb = mask;
        mask = undefined;
      }
      if (typeof data === "number")
        data = data.toString();
      if (this.readyState !== WebSocket.OPEN) {
        sendAfterClose(this, data, cb);
        return;
      }
      if (mask === undefined)
        mask = !this._isServer;
      this._sender.pong(data || EMPTY_BUFFER, mask, cb);
    }
    resume() {
      if (this.readyState === WebSocket.CONNECTING || this.readyState === WebSocket.CLOSED) {
        return;
      }
      this._paused = false;
      if (!this._receiver._writableState.needDrain)
        this._socket.resume();
    }
    send(data, options, cb) {
      if (this.readyState === WebSocket.CONNECTING) {
        throw new Error("WebSocket is not open: readyState 0 (CONNECTING)");
      }
      if (typeof options === "function") {
        cb = options;
        options = {};
      }
      if (typeof data === "number")
        data = data.toString();
      if (this.readyState !== WebSocket.OPEN) {
        sendAfterClose(this, data, cb);
        return;
      }
      const opts = {
        binary: typeof data !== "string",
        mask: !this._isServer,
        compress: true,
        fin: true,
        ...options
      };
      if (!this._extensions[PerMessageDeflate.extensionName]) {
        opts.compress = false;
      }
      this._sender.send(data || EMPTY_BUFFER, opts, cb);
    }
    terminate() {
      if (this.readyState === WebSocket.CLOSED)
        return;
      if (this.readyState === WebSocket.CONNECTING) {
        const msg = "WebSocket was closed before the connection was established";
        abortHandshake(this, this._req, msg);
        return;
      }
      if (this._socket) {
        this._readyState = WebSocket.CLOSING;
        this._socket.destroy();
      }
    }
  }
  Object.defineProperty(WebSocket, "CONNECTING", {
    enumerable: true,
    value: readyStates.indexOf("CONNECTING")
  });
  Object.defineProperty(WebSocket.prototype, "CONNECTING", {
    enumerable: true,
    value: readyStates.indexOf("CONNECTING")
  });
  Object.defineProperty(WebSocket, "OPEN", {
    enumerable: true,
    value: readyStates.indexOf("OPEN")
  });
  Object.defineProperty(WebSocket.prototype, "OPEN", {
    enumerable: true,
    value: readyStates.indexOf("OPEN")
  });
  Object.defineProperty(WebSocket, "CLOSING", {
    enumerable: true,
    value: readyStates.indexOf("CLOSING")
  });
  Object.defineProperty(WebSocket.prototype, "CLOSING", {
    enumerable: true,
    value: readyStates.indexOf("CLOSING")
  });
  Object.defineProperty(WebSocket, "CLOSED", {
    enumerable: true,
    value: readyStates.indexOf("CLOSED")
  });
  Object.defineProperty(WebSocket.prototype, "CLOSED", {
    enumerable: true,
    value: readyStates.indexOf("CLOSED")
  });
  [
    "binaryType",
    "bufferedAmount",
    "extensions",
    "isPaused",
    "protocol",
    "readyState",
    "url"
  ].forEach((property) => {
    Object.defineProperty(WebSocket.prototype, property, { enumerable: true });
  });
  ["open", "error", "close", "message"].forEach((method) => {
    Object.defineProperty(WebSocket.prototype, `on${method}`, {
      enumerable: true,
      get() {
        for (const listener of this.listeners(method)) {
          if (listener[kForOnEventAttribute])
            return listener[kListener];
        }
        return null;
      },
      set(handler) {
        for (const listener of this.listeners(method)) {
          if (listener[kForOnEventAttribute]) {
            this.removeListener(method, listener);
            break;
          }
        }
        if (typeof handler !== "function")
          return;
        this.addEventListener(method, handler, {
          [kForOnEventAttribute]: true
        });
      }
    });
  });
  WebSocket.prototype.addEventListener = addEventListener;
  WebSocket.prototype.removeEventListener = removeEventListener;
  module.exports = WebSocket;
  function initAsClient(websocket, address, protocols, options) {
    const opts = {
      allowSynchronousEvents: true,
      autoPong: true,
      closeTimeout: CLOSE_TIMEOUT,
      protocolVersion: protocolVersions[1],
      maxPayload: 100 * 1024 * 1024,
      skipUTF8Validation: false,
      perMessageDeflate: true,
      followRedirects: false,
      maxRedirects: 10,
      ...options,
      socketPath: undefined,
      hostname: undefined,
      protocol: undefined,
      timeout: undefined,
      method: "GET",
      host: undefined,
      path: undefined,
      port: undefined
    };
    websocket._autoPong = opts.autoPong;
    websocket._closeTimeout = opts.closeTimeout;
    if (!protocolVersions.includes(opts.protocolVersion)) {
      throw new RangeError(`Unsupported protocol version: ${opts.protocolVersion} ` + `(supported versions: ${protocolVersions.join(", ")})`);
    }
    let parsedUrl;
    if (address instanceof URL2) {
      parsedUrl = address;
    } else {
      try {
        parsedUrl = new URL2(address);
      } catch (e) {
        throw new SyntaxError(`Invalid URL: ${address}`);
      }
    }
    if (parsedUrl.protocol === "http:") {
      parsedUrl.protocol = "ws:";
    } else if (parsedUrl.protocol === "https:") {
      parsedUrl.protocol = "wss:";
    }
    websocket._url = parsedUrl.href;
    const isSecure = parsedUrl.protocol === "wss:";
    const isIpcUrl = parsedUrl.protocol === "ws+unix:";
    let invalidUrlMessage;
    if (parsedUrl.protocol !== "ws:" && !isSecure && !isIpcUrl) {
      invalidUrlMessage = `The URL's protocol must be one of "ws:", "wss:", ` + '"http:", "https:", or "ws+unix:"';
    } else if (isIpcUrl && !parsedUrl.pathname) {
      invalidUrlMessage = "The URL's pathname is empty";
    } else if (parsedUrl.hash) {
      invalidUrlMessage = "The URL contains a fragment identifier";
    }
    if (invalidUrlMessage) {
      const err = new SyntaxError(invalidUrlMessage);
      if (websocket._redirects === 0) {
        throw err;
      } else {
        emitErrorAndClose(websocket, err);
        return;
      }
    }
    const defaultPort = isSecure ? 443 : 80;
    const key = randomBytes(16).toString("base64");
    const request = isSecure ? https.request : http.request;
    const protocolSet = new Set;
    let perMessageDeflate;
    opts.createConnection = opts.createConnection || (isSecure ? tlsConnect : netConnect);
    opts.defaultPort = opts.defaultPort || defaultPort;
    opts.port = parsedUrl.port || defaultPort;
    opts.host = parsedUrl.hostname.startsWith("[") ? parsedUrl.hostname.slice(1, -1) : parsedUrl.hostname;
    opts.headers = {
      ...opts.headers,
      "Sec-WebSocket-Version": opts.protocolVersion,
      "Sec-WebSocket-Key": key,
      Connection: "Upgrade",
      Upgrade: "websocket"
    };
    opts.path = parsedUrl.pathname + parsedUrl.search;
    opts.timeout = opts.handshakeTimeout;
    if (opts.perMessageDeflate) {
      perMessageDeflate = new PerMessageDeflate(opts.perMessageDeflate !== true ? opts.perMessageDeflate : {}, false, opts.maxPayload);
      opts.headers["Sec-WebSocket-Extensions"] = format({
        [PerMessageDeflate.extensionName]: perMessageDeflate.offer()
      });
    }
    if (protocols.length) {
      for (const protocol of protocols) {
        if (typeof protocol !== "string" || !subprotocolRegex.test(protocol) || protocolSet.has(protocol)) {
          throw new SyntaxError("An invalid or duplicated subprotocol was specified");
        }
        protocolSet.add(protocol);
      }
      opts.headers["Sec-WebSocket-Protocol"] = protocols.join(",");
    }
    if (opts.origin) {
      if (opts.protocolVersion < 13) {
        opts.headers["Sec-WebSocket-Origin"] = opts.origin;
      } else {
        opts.headers.Origin = opts.origin;
      }
    }
    if (parsedUrl.username || parsedUrl.password) {
      opts.auth = `${parsedUrl.username}:${parsedUrl.password}`;
    }
    if (isIpcUrl) {
      const parts = opts.path.split(":");
      opts.socketPath = parts[0];
      opts.path = parts[1];
    }
    let req;
    if (opts.followRedirects) {
      if (websocket._redirects === 0) {
        websocket._originalIpc = isIpcUrl;
        websocket._originalSecure = isSecure;
        websocket._originalHostOrSocketPath = isIpcUrl ? opts.socketPath : parsedUrl.host;
        const headers = options && options.headers;
        options = { ...options, headers: {} };
        if (headers) {
          for (const [key2, value] of Object.entries(headers)) {
            options.headers[key2.toLowerCase()] = value;
          }
        }
      } else if (websocket.listenerCount("redirect") === 0) {
        const isSameHost = isIpcUrl ? websocket._originalIpc ? opts.socketPath === websocket._originalHostOrSocketPath : false : websocket._originalIpc ? false : parsedUrl.host === websocket._originalHostOrSocketPath;
        if (!isSameHost || websocket._originalSecure && !isSecure) {
          delete opts.headers.authorization;
          delete opts.headers.cookie;
          if (!isSameHost)
            delete opts.headers.host;
          opts.auth = undefined;
        }
      }
      if (opts.auth && !options.headers.authorization) {
        options.headers.authorization = "Basic " + Buffer.from(opts.auth).toString("base64");
      }
      req = websocket._req = request(opts);
      if (websocket._redirects) {
        websocket.emit("redirect", websocket.url, req);
      }
    } else {
      req = websocket._req = request(opts);
    }
    if (opts.timeout) {
      req.on("timeout", () => {
        abortHandshake(websocket, req, "Opening handshake has timed out");
      });
    }
    req.on("error", (err) => {
      if (req === null || req[kAborted])
        return;
      req = websocket._req = null;
      emitErrorAndClose(websocket, err);
    });
    req.on("response", (res) => {
      const location = res.headers.location;
      const statusCode = res.statusCode;
      if (location && opts.followRedirects && statusCode >= 300 && statusCode < 400) {
        if (++websocket._redirects > opts.maxRedirects) {
          abortHandshake(websocket, req, "Maximum redirects exceeded");
          return;
        }
        req.abort();
        let addr;
        try {
          addr = new URL2(location, address);
        } catch (e) {
          const err = new SyntaxError(`Invalid URL: ${location}`);
          emitErrorAndClose(websocket, err);
          return;
        }
        initAsClient(websocket, addr, protocols, options);
      } else if (!websocket.emit("unexpected-response", req, res)) {
        abortHandshake(websocket, req, `Unexpected server response: ${res.statusCode}`);
      }
    });
    req.on("upgrade", (res, socket, head) => {
      websocket.emit("upgrade", res);
      if (websocket.readyState !== WebSocket.CONNECTING)
        return;
      req = websocket._req = null;
      const upgrade = res.headers.upgrade;
      if (upgrade === undefined || upgrade.toLowerCase() !== "websocket") {
        abortHandshake(websocket, socket, "Invalid Upgrade header");
        return;
      }
      const digest = createHash("sha1").update(key + GUID).digest("base64");
      if (res.headers["sec-websocket-accept"] !== digest) {
        abortHandshake(websocket, socket, "Invalid Sec-WebSocket-Accept header");
        return;
      }
      const serverProt = res.headers["sec-websocket-protocol"];
      let protError;
      if (serverProt !== undefined) {
        if (!protocolSet.size) {
          protError = "Server sent a subprotocol but none was requested";
        } else if (!protocolSet.has(serverProt)) {
          protError = "Server sent an invalid subprotocol";
        }
      } else if (protocolSet.size) {
        protError = "Server sent no subprotocol";
      }
      if (protError) {
        abortHandshake(websocket, socket, protError);
        return;
      }
      if (serverProt)
        websocket._protocol = serverProt;
      const secWebSocketExtensions = res.headers["sec-websocket-extensions"];
      if (secWebSocketExtensions !== undefined) {
        if (!perMessageDeflate) {
          const message = "Server sent a Sec-WebSocket-Extensions header but no extension " + "was requested";
          abortHandshake(websocket, socket, message);
          return;
        }
        let extensions;
        try {
          extensions = parse(secWebSocketExtensions);
        } catch (err) {
          const message = "Invalid Sec-WebSocket-Extensions header";
          abortHandshake(websocket, socket, message);
          return;
        }
        const extensionNames = Object.keys(extensions);
        if (extensionNames.length !== 1 || extensionNames[0] !== PerMessageDeflate.extensionName) {
          const message = "Server indicated an extension that was not requested";
          abortHandshake(websocket, socket, message);
          return;
        }
        try {
          perMessageDeflate.accept(extensions[PerMessageDeflate.extensionName]);
        } catch (err) {
          const message = "Invalid Sec-WebSocket-Extensions header";
          abortHandshake(websocket, socket, message);
          return;
        }
        websocket._extensions[PerMessageDeflate.extensionName] = perMessageDeflate;
      }
      websocket.setSocket(socket, head, {
        allowSynchronousEvents: opts.allowSynchronousEvents,
        generateMask: opts.generateMask,
        maxPayload: opts.maxPayload,
        skipUTF8Validation: opts.skipUTF8Validation
      });
    });
    if (opts.finishRequest) {
      opts.finishRequest(req, websocket);
    } else {
      req.end();
    }
  }
  function emitErrorAndClose(websocket, err) {
    websocket._readyState = WebSocket.CLOSING;
    websocket._errorEmitted = true;
    websocket.emit("error", err);
    websocket.emitClose();
  }
  function netConnect(options) {
    options.path = options.socketPath;
    return net.connect(options);
  }
  function tlsConnect(options) {
    options.path = undefined;
    if (!options.servername && options.servername !== "") {
      options.servername = net.isIP(options.host) ? "" : options.host;
    }
    return tls.connect(options);
  }
  function abortHandshake(websocket, stream, message) {
    websocket._readyState = WebSocket.CLOSING;
    const err = new Error(message);
    Error.captureStackTrace(err, abortHandshake);
    if (stream.setHeader) {
      stream[kAborted] = true;
      stream.abort();
      if (stream.socket && !stream.socket.destroyed) {
        stream.socket.destroy();
      }
      process.nextTick(emitErrorAndClose, websocket, err);
    } else {
      stream.destroy(err);
      stream.once("error", websocket.emit.bind(websocket, "error"));
      stream.once("close", websocket.emitClose.bind(websocket));
    }
  }
  function sendAfterClose(websocket, data, cb) {
    if (data) {
      const length = isBlob(data) ? data.size : toBuffer(data).length;
      if (websocket._socket)
        websocket._sender._bufferedBytes += length;
      else
        websocket._bufferedAmount += length;
    }
    if (cb) {
      const err = new Error(`WebSocket is not open: readyState ${websocket.readyState} ` + `(${readyStates[websocket.readyState]})`);
      process.nextTick(cb, err);
    }
  }
  function receiverOnConclude(code, reason) {
    const websocket = this[kWebSocket];
    websocket._closeFrameReceived = true;
    websocket._closeMessage = reason;
    websocket._closeCode = code;
    if (websocket._socket[kWebSocket] === undefined)
      return;
    websocket._socket.removeListener("data", socketOnData);
    process.nextTick(resume, websocket._socket);
    if (code === 1005)
      websocket.close();
    else
      websocket.close(code, reason);
  }
  function receiverOnDrain() {
    const websocket = this[kWebSocket];
    if (!websocket.isPaused)
      websocket._socket.resume();
  }
  function receiverOnError(err) {
    const websocket = this[kWebSocket];
    if (websocket._socket[kWebSocket] !== undefined) {
      websocket._socket.removeListener("data", socketOnData);
      process.nextTick(resume, websocket._socket);
      websocket.close(err[kStatusCode]);
    }
    if (!websocket._errorEmitted) {
      websocket._errorEmitted = true;
      websocket.emit("error", err);
    }
  }
  function receiverOnFinish() {
    this[kWebSocket].emitClose();
  }
  function receiverOnMessage(data, isBinary) {
    this[kWebSocket].emit("message", data, isBinary);
  }
  function receiverOnPing(data) {
    const websocket = this[kWebSocket];
    if (websocket._autoPong)
      websocket.pong(data, !this._isServer, NOOP);
    websocket.emit("ping", data);
  }
  function receiverOnPong(data) {
    this[kWebSocket].emit("pong", data);
  }
  function resume(stream) {
    stream.resume();
  }
  function senderOnError(err) {
    const websocket = this[kWebSocket];
    if (websocket.readyState === WebSocket.CLOSED)
      return;
    if (websocket.readyState === WebSocket.OPEN) {
      websocket._readyState = WebSocket.CLOSING;
      setCloseTimer(websocket);
    }
    this._socket.end();
    if (!websocket._errorEmitted) {
      websocket._errorEmitted = true;
      websocket.emit("error", err);
    }
  }
  function setCloseTimer(websocket) {
    websocket._closeTimer = setTimeout(websocket._socket.destroy.bind(websocket._socket), websocket._closeTimeout);
  }
  function socketOnClose() {
    const websocket = this[kWebSocket];
    this.removeListener("close", socketOnClose);
    this.removeListener("data", socketOnData);
    this.removeListener("end", socketOnEnd);
    websocket._readyState = WebSocket.CLOSING;
    if (!this._readableState.endEmitted && !websocket._closeFrameReceived && !websocket._receiver._writableState.errorEmitted && this._readableState.length !== 0) {
      const chunk = this.read(this._readableState.length);
      websocket._receiver.write(chunk);
    }
    websocket._receiver.end();
    this[kWebSocket] = undefined;
    clearTimeout(websocket._closeTimer);
    if (websocket._receiver._writableState.finished || websocket._receiver._writableState.errorEmitted) {
      websocket.emitClose();
    } else {
      websocket._receiver.on("error", receiverOnFinish);
      websocket._receiver.on("finish", receiverOnFinish);
    }
  }
  function socketOnData(chunk) {
    if (!this[kWebSocket]._receiver.write(chunk)) {
      this.pause();
    }
  }
  function socketOnEnd() {
    const websocket = this[kWebSocket];
    websocket._readyState = WebSocket.CLOSING;
    websocket._receiver.end();
    this.end();
  }
  function socketOnError() {
    const websocket = this[kWebSocket];
    this.removeListener("error", socketOnError);
    this.on("error", NOOP);
    if (websocket) {
      websocket._readyState = WebSocket.CLOSING;
      this.destroy();
    }
  }
});

// node_modules/ws/lib/stream.js
var require_stream = __commonJS((exports, module) => {
  var WebSocket = require_websocket();
  var { Duplex } = __require("stream");
  function emitClose(stream) {
    stream.emit("close");
  }
  function duplexOnEnd() {
    if (!this.destroyed && this._writableState.finished) {
      this.destroy();
    }
  }
  function duplexOnError(err) {
    this.removeListener("error", duplexOnError);
    this.destroy();
    if (this.listenerCount("error") === 0) {
      this.emit("error", err);
    }
  }
  function createWebSocketStream(ws, options) {
    let terminateOnDestroy = true;
    const duplex = new Duplex({
      ...options,
      autoDestroy: false,
      emitClose: false,
      objectMode: false,
      writableObjectMode: false
    });
    ws.on("message", function message(msg, isBinary) {
      const data = !isBinary && duplex._readableState.objectMode ? msg.toString() : msg;
      if (!duplex.push(data))
        ws.pause();
    });
    ws.once("error", function error(err) {
      if (duplex.destroyed)
        return;
      terminateOnDestroy = false;
      duplex.destroy(err);
    });
    ws.once("close", function close() {
      if (duplex.destroyed)
        return;
      duplex.push(null);
    });
    duplex._destroy = function(err, callback) {
      if (ws.readyState === ws.CLOSED) {
        callback(err);
        process.nextTick(emitClose, duplex);
        return;
      }
      let called = false;
      ws.once("error", function error(err2) {
        called = true;
        callback(err2);
      });
      ws.once("close", function close() {
        if (!called)
          callback(err);
        process.nextTick(emitClose, duplex);
      });
      if (terminateOnDestroy)
        ws.terminate();
    };
    duplex._final = function(callback) {
      if (ws.readyState === ws.CONNECTING) {
        ws.once("open", function open() {
          duplex._final(callback);
        });
        return;
      }
      if (ws._socket === null)
        return;
      if (ws._socket._writableState.finished) {
        callback();
        if (duplex._readableState.endEmitted)
          duplex.destroy();
      } else {
        ws._socket.once("finish", function finish() {
          callback();
        });
        ws.close();
      }
    };
    duplex._read = function() {
      if (ws.isPaused)
        ws.resume();
    };
    duplex._write = function(chunk, encoding, callback) {
      if (ws.readyState === ws.CONNECTING) {
        ws.once("open", function open() {
          duplex._write(chunk, encoding, callback);
        });
        return;
      }
      ws.send(chunk, callback);
    };
    duplex.on("end", duplexOnEnd);
    duplex.on("error", duplexOnError);
    return duplex;
  }
  module.exports = createWebSocketStream;
});

// node_modules/ws/lib/subprotocol.js
var require_subprotocol = __commonJS((exports, module) => {
  var { tokenChars } = require_validation();
  function parse(header) {
    const protocols = new Set;
    let start = -1;
    let end = -1;
    let i = 0;
    for (i;i < header.length; i++) {
      const code = header.charCodeAt(i);
      if (end === -1 && tokenChars[code] === 1) {
        if (start === -1)
          start = i;
      } else if (i !== 0 && (code === 32 || code === 9)) {
        if (end === -1 && start !== -1)
          end = i;
      } else if (code === 44) {
        if (start === -1) {
          throw new SyntaxError(`Unexpected character at index ${i}`);
        }
        if (end === -1)
          end = i;
        const protocol2 = header.slice(start, end);
        if (protocols.has(protocol2)) {
          throw new SyntaxError(`The "${protocol2}" subprotocol is duplicated`);
        }
        protocols.add(protocol2);
        start = end = -1;
      } else {
        throw new SyntaxError(`Unexpected character at index ${i}`);
      }
    }
    if (start === -1 || end !== -1) {
      throw new SyntaxError("Unexpected end of input");
    }
    const protocol = header.slice(start, i);
    if (protocols.has(protocol)) {
      throw new SyntaxError(`The "${protocol}" subprotocol is duplicated`);
    }
    protocols.add(protocol);
    return protocols;
  }
  module.exports = { parse };
});

// node_modules/ws/lib/websocket-server.js
var require_websocket_server = __commonJS((exports, module) => {
  var EventEmitter = __require("events");
  var http = __require("http");
  var { Duplex } = __require("stream");
  var { createHash } = __require("crypto");
  var extension = require_extension();
  var PerMessageDeflate = require_permessage_deflate();
  var subprotocol = require_subprotocol();
  var WebSocket = require_websocket();
  var { CLOSE_TIMEOUT, GUID, kWebSocket } = require_constants();
  var keyRegex = /^[+/0-9A-Za-z]{22}==$/;
  var RUNNING = 0;
  var CLOSING = 1;
  var CLOSED = 2;

  class WebSocketServer extends EventEmitter {
    constructor(options, callback) {
      super();
      options = {
        allowSynchronousEvents: true,
        autoPong: true,
        maxPayload: 100 * 1024 * 1024,
        skipUTF8Validation: false,
        perMessageDeflate: false,
        handleProtocols: null,
        clientTracking: true,
        closeTimeout: CLOSE_TIMEOUT,
        verifyClient: null,
        noServer: false,
        backlog: null,
        server: null,
        host: null,
        path: null,
        port: null,
        WebSocket,
        ...options
      };
      if (options.port == null && !options.server && !options.noServer || options.port != null && (options.server || options.noServer) || options.server && options.noServer) {
        throw new TypeError('One and only one of the "port", "server", or "noServer" options ' + "must be specified");
      }
      if (options.port != null) {
        this._server = http.createServer((req, res) => {
          const body = http.STATUS_CODES[426];
          res.writeHead(426, {
            "Content-Length": body.length,
            "Content-Type": "text/plain"
          });
          res.end(body);
        });
        this._server.listen(options.port, options.host, options.backlog, callback);
      } else if (options.server) {
        this._server = options.server;
      }
      if (this._server) {
        const emitConnection = this.emit.bind(this, "connection");
        this._removeListeners = addListeners(this._server, {
          listening: this.emit.bind(this, "listening"),
          error: this.emit.bind(this, "error"),
          upgrade: (req, socket, head) => {
            this.handleUpgrade(req, socket, head, emitConnection);
          }
        });
      }
      if (options.perMessageDeflate === true)
        options.perMessageDeflate = {};
      if (options.clientTracking) {
        this.clients = new Set;
        this._shouldEmitClose = false;
      }
      this.options = options;
      this._state = RUNNING;
    }
    address() {
      if (this.options.noServer) {
        throw new Error('The server is operating in "noServer" mode');
      }
      if (!this._server)
        return null;
      return this._server.address();
    }
    close(cb) {
      if (this._state === CLOSED) {
        if (cb) {
          this.once("close", () => {
            cb(new Error("The server is not running"));
          });
        }
        process.nextTick(emitClose, this);
        return;
      }
      if (cb)
        this.once("close", cb);
      if (this._state === CLOSING)
        return;
      this._state = CLOSING;
      if (this.options.noServer || this.options.server) {
        if (this._server) {
          this._removeListeners();
          this._removeListeners = this._server = null;
        }
        if (this.clients) {
          if (!this.clients.size) {
            process.nextTick(emitClose, this);
          } else {
            this._shouldEmitClose = true;
          }
        } else {
          process.nextTick(emitClose, this);
        }
      } else {
        const server = this._server;
        this._removeListeners();
        this._removeListeners = this._server = null;
        server.close(() => {
          emitClose(this);
        });
      }
    }
    shouldHandle(req) {
      if (this.options.path) {
        const index = req.url.indexOf("?");
        const pathname = index !== -1 ? req.url.slice(0, index) : req.url;
        if (pathname !== this.options.path)
          return false;
      }
      return true;
    }
    handleUpgrade(req, socket, head, cb) {
      socket.on("error", socketOnError);
      const key = req.headers["sec-websocket-key"];
      const upgrade = req.headers.upgrade;
      const version = +req.headers["sec-websocket-version"];
      if (req.method !== "GET") {
        const message = "Invalid HTTP method";
        abortHandshakeOrEmitwsClientError(this, req, socket, 405, message);
        return;
      }
      if (upgrade === undefined || upgrade.toLowerCase() !== "websocket") {
        const message = "Invalid Upgrade header";
        abortHandshakeOrEmitwsClientError(this, req, socket, 400, message);
        return;
      }
      if (key === undefined || !keyRegex.test(key)) {
        const message = "Missing or invalid Sec-WebSocket-Key header";
        abortHandshakeOrEmitwsClientError(this, req, socket, 400, message);
        return;
      }
      if (version !== 13 && version !== 8) {
        const message = "Missing or invalid Sec-WebSocket-Version header";
        abortHandshakeOrEmitwsClientError(this, req, socket, 400, message, {
          "Sec-WebSocket-Version": "13, 8"
        });
        return;
      }
      if (!this.shouldHandle(req)) {
        abortHandshake(socket, 400);
        return;
      }
      const secWebSocketProtocol = req.headers["sec-websocket-protocol"];
      let protocols = new Set;
      if (secWebSocketProtocol !== undefined) {
        try {
          protocols = subprotocol.parse(secWebSocketProtocol);
        } catch (err) {
          const message = "Invalid Sec-WebSocket-Protocol header";
          abortHandshakeOrEmitwsClientError(this, req, socket, 400, message);
          return;
        }
      }
      const secWebSocketExtensions = req.headers["sec-websocket-extensions"];
      const extensions = {};
      if (this.options.perMessageDeflate && secWebSocketExtensions !== undefined) {
        const perMessageDeflate = new PerMessageDeflate(this.options.perMessageDeflate, true, this.options.maxPayload);
        try {
          const offers = extension.parse(secWebSocketExtensions);
          if (offers[PerMessageDeflate.extensionName]) {
            perMessageDeflate.accept(offers[PerMessageDeflate.extensionName]);
            extensions[PerMessageDeflate.extensionName] = perMessageDeflate;
          }
        } catch (err) {
          const message = "Invalid or unacceptable Sec-WebSocket-Extensions header";
          abortHandshakeOrEmitwsClientError(this, req, socket, 400, message);
          return;
        }
      }
      if (this.options.verifyClient) {
        const info = {
          origin: req.headers[`${version === 8 ? "sec-websocket-origin" : "origin"}`],
          secure: !!(req.socket.authorized || req.socket.encrypted),
          req
        };
        if (this.options.verifyClient.length === 2) {
          this.options.verifyClient(info, (verified, code, message, headers) => {
            if (!verified) {
              return abortHandshake(socket, code || 401, message, headers);
            }
            this.completeUpgrade(extensions, key, protocols, req, socket, head, cb);
          });
          return;
        }
        if (!this.options.verifyClient(info))
          return abortHandshake(socket, 401);
      }
      this.completeUpgrade(extensions, key, protocols, req, socket, head, cb);
    }
    completeUpgrade(extensions, key, protocols, req, socket, head, cb) {
      if (!socket.readable || !socket.writable)
        return socket.destroy();
      if (socket[kWebSocket]) {
        throw new Error("server.handleUpgrade() was called more than once with the same " + "socket, possibly due to a misconfiguration");
      }
      if (this._state > RUNNING)
        return abortHandshake(socket, 503);
      const digest = createHash("sha1").update(key + GUID).digest("base64");
      const headers = [
        "HTTP/1.1 101 Switching Protocols",
        "Upgrade: websocket",
        "Connection: Upgrade",
        `Sec-WebSocket-Accept: ${digest}`
      ];
      const ws = new this.options.WebSocket(null, undefined, this.options);
      if (protocols.size) {
        const protocol = this.options.handleProtocols ? this.options.handleProtocols(protocols, req) : protocols.values().next().value;
        if (protocol) {
          headers.push(`Sec-WebSocket-Protocol: ${protocol}`);
          ws._protocol = protocol;
        }
      }
      if (extensions[PerMessageDeflate.extensionName]) {
        const params = extensions[PerMessageDeflate.extensionName].params;
        const value = extension.format({
          [PerMessageDeflate.extensionName]: [params]
        });
        headers.push(`Sec-WebSocket-Extensions: ${value}`);
        ws._extensions = extensions;
      }
      this.emit("headers", headers, req);
      socket.write(headers.concat(`\r
`).join(`\r
`));
      socket.removeListener("error", socketOnError);
      ws.setSocket(socket, head, {
        allowSynchronousEvents: this.options.allowSynchronousEvents,
        maxPayload: this.options.maxPayload,
        skipUTF8Validation: this.options.skipUTF8Validation
      });
      if (this.clients) {
        this.clients.add(ws);
        ws.on("close", () => {
          this.clients.delete(ws);
          if (this._shouldEmitClose && !this.clients.size) {
            process.nextTick(emitClose, this);
          }
        });
      }
      cb(ws, req);
    }
  }
  module.exports = WebSocketServer;
  function addListeners(server, map) {
    for (const event of Object.keys(map))
      server.on(event, map[event]);
    return function removeListeners() {
      for (const event of Object.keys(map)) {
        server.removeListener(event, map[event]);
      }
    };
  }
  function emitClose(server) {
    server._state = CLOSED;
    server.emit("close");
  }
  function socketOnError() {
    this.destroy();
  }
  function abortHandshake(socket, code, message, headers) {
    message = message || http.STATUS_CODES[code];
    headers = {
      Connection: "close",
      "Content-Type": "text/html",
      "Content-Length": Buffer.byteLength(message),
      ...headers
    };
    socket.once("finish", socket.destroy);
    socket.end(`HTTP/1.1 ${code} ${http.STATUS_CODES[code]}\r
` + Object.keys(headers).map((h) => `${h}: ${headers[h]}`).join(`\r
`) + `\r
\r
` + message);
  }
  function abortHandshakeOrEmitwsClientError(server, req, socket, code, message, headers) {
    if (server.listenerCount("wsClientError")) {
      const err = new Error(message);
      Error.captureStackTrace(err, abortHandshakeOrEmitwsClientError);
      server.emit("wsClientError", err, socket, req);
    } else {
      abortHandshake(socket, code, message, headers);
    }
  }
});

// src/pilot.ts
var import_node_pty = __toESM(require_node_pty(), 1);
import fs from "fs";
import http from "http";
import path from "path";
import { execFile } from "child_process";
import { promisify } from "util";
import { createRequire as createRequire2 } from "module";

// node_modules/ws/wrapper.mjs
var import_stream = __toESM(require_stream(), 1);
var import_receiver = __toESM(require_receiver(), 1);
var import_sender = __toESM(require_sender(), 1);
var import_websocket = __toESM(require_websocket(), 1);
var import_websocket_server = __toESM(require_websocket_server(), 1);

// src/pilot.ts
var execFileAsync = promisify(execFile);
var require2 = createRequire2(import.meta.url);
function parsePilotArgs(argv) {
  const args = {};
  for (let i = 2;i < argv.length; i++) {
    const a = argv[i];
    if (a.startsWith("--") && i + 1 < argv.length && !argv[i + 1].startsWith("--")) {
      args[a.slice(2)] = argv[++i];
    }
  }
  return {
    port: Number.parseInt(args.port || process.env.PORT || "10170", 10),
    token: args.token || process.env.PILOT_TOKEN || "",
    ttl: Number.parseInt(args.ttl || "1800", 10),
    sessions: (args.sessions || "").split(",").filter(Boolean)
  };
}
var CLI_ARGS = parsePilotArgs(process.argv);
var PORT = CLI_ARGS.port;
var PILOT_TOKEN = CLI_ARGS.token;
var ACTIVE_CONNECTIONS = new Map;
var ENABLED_SESSIONS = new Map;
var SESSION_TIMERS = new Map;
var PILOT_TIMEOUT_MS = 30 * 60 * 1000;
var GRID_SESSIONS = new Map;
var _idleTimer = null;
function resetIdleTimer() {
  if (CLI_ARGS.ttl <= 0)
    return;
  if (_idleTimer)
    clearTimeout(_idleTimer);
  _idleTimer = setTimeout(() => {
    console.log(`[pilot] TTL expired after ${CLI_ARGS.ttl}s of inactivity — shutting down`);
    shutdown();
  }, CLI_ARGS.ttl * 1000);
}
for (const entry of CLI_ARGS.sessions) {
  const [name, host] = entry.includes("@") ? entry.split("@", 2) : [entry, null];
  if (name) {
    ENABLED_SESSIONS.set(name, { host: host || null });
    console.log(`[pilot] pre-enabled session: ${name}${host ? ` (host: ${host})` : ""}`);
  }
}
function enableSession(sessionName, host) {
  ENABLED_SESSIONS.set(sessionName, { host: host || null });
  refreshSessionTimer(sessionName);
}
function disableSession(sessionName) {
  ENABLED_SESSIONS.delete(sessionName);
  const timer = SESSION_TIMERS.get(sessionName);
  if (timer) {
    clearTimeout(timer);
    SESSION_TIMERS.delete(sessionName);
  }
  for (const [ws, ptyProc] of ACTIVE_CONNECTIONS.entries()) {
    const sessionTag = ws.__pilotSession;
    if (sessionTag === sessionName) {
      try {
        ptyProc.kill();
      } catch {}
      try {
        ws.close(4002, "Session disabled");
      } catch {}
      ACTIVE_CONNECTIONS.delete(ws);
    }
  }
}
function refreshSessionTimer(sessionName) {
  const existing = SESSION_TIMERS.get(sessionName);
  if (existing)
    clearTimeout(existing);
  SESSION_TIMERS.set(sessionName, setTimeout(() => {
    console.log(`[pilot] auto-disabling ${sessionName} (5min idle)`);
    disableSession(sessionName);
  }, PILOT_TIMEOUT_MS));
}
var GRID_STATE_FILE = "/tmp/claudecode-telegram/pilot-grid-sessions.json";
function persistGridSessions() {
  try {
    const data = {};
    for (const [slug, gs] of GRID_SESSIONS) {
      data[slug] = { sessions: gs.sessions, createdAt: gs.createdAt, expiresAt: gs.expiresAt };
    }
    const dir = path.dirname(GRID_STATE_FILE);
    fs.mkdirSync(dir, { recursive: true });
    fs.writeFileSync(GRID_STATE_FILE, JSON.stringify(data));
  } catch {}
}
function restoreGridSessions() {
  try {
    if (!fs.existsSync(GRID_STATE_FILE))
      return;
    const data = JSON.parse(fs.readFileSync(GRID_STATE_FILE, "utf-8"));
    const now = Date.now();
    let restored = 0;
    for (const [slug, gs] of Object.entries(data)) {
      if (gs.expiresAt <= now)
        continue;
      const remaining = gs.expiresAt - now;
      const timer = setTimeout(() => {
        console.log(`[pilot] grid session '${slug}' expired`);
        GRID_SESSIONS.delete(slug);
        persistGridSessions();
      }, remaining);
      GRID_SESSIONS.set(slug, { sessions: gs.sessions, createdAt: gs.createdAt, expiresAt: gs.expiresAt, timer });
      restored++;
    }
    if (restored)
      console.log(`[pilot] restored ${restored} grid session(s) from disk`);
  } catch {}
}
function createGridSession(slug, sessionNames, ttlMs) {
  const existing = GRID_SESSIONS.get(slug);
  if (existing && existing.timer)
    clearTimeout(existing.timer);
  const now = Date.now();
  const expiresAt = now + ttlMs;
  const timer = setTimeout(() => {
    console.log(`[pilot] grid session '${slug}' expired`);
    GRID_SESSIONS.delete(slug);
    persistGridSessions();
  }, ttlMs);
  GRID_SESSIONS.set(slug, { sessions: sessionNames, createdAt: now, expiresAt, timer });
  persistGridSessions();
  return { slug, expiresAt };
}
function getGridSession(slug) {
  const gs = GRID_SESSIONS.get(slug);
  if (!gs)
    return null;
  if (Date.now() >= gs.expiresAt) {
    GRID_SESSIONS.delete(slug);
    persistGridSessions();
    return null;
  }
  return gs;
}
var MIME_TYPES = {
  ".css": "text/css; charset=utf-8",
  ".html": "text/html; charset=utf-8",
  ".js": "application/javascript; charset=utf-8",
  ".json": "application/json; charset=utf-8",
  ".mjs": "application/javascript; charset=utf-8",
  ".wasm": "application/wasm"
};
function resolveGhosttyAssets() {
  const resolved = require2.resolve("ghostty-web");
  const packageRoot = resolved.replace(/[/\\]dist[/\\].*$/, "");
  const distPath = path.join(packageRoot, "dist");
  const wasmPath = path.join(packageRoot, "ghostty-vt.wasm");
  if (!fs.existsSync(path.join(distPath, "ghostty-web.js")) || !fs.existsSync(wasmPath)) {
    throw new Error("ghostty-web assets were not found in node_modules");
  }
  return { distPath, wasmPath };
}
var { distPath, wasmPath } = resolveGhosttyAssets();
function escapeHtml(value) {
  return value.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;").replace(/'/g, "&#39;");
}
function parseIntOrDefault(value, fallback) {
  const parsed = Number.parseInt(value || "", 10);
  if (!Number.isFinite(parsed) || parsed <= 0) {
    return fallback;
  }
  return Math.min(parsed, 500);
}
function isAllowedSessionName(name) {
  return typeof name === "string" && /^claude-[A-Za-z0-9._:-]+$/.test(name);
}
function isAuthorized(urlObj) {
  if (!PILOT_TOKEN)
    return false;
  return urlObj.searchParams.get("token") === PILOT_TOKEN;
}
function getTokenForTemplates(urlObj) {
  if (!PILOT_TOKEN) {
    return "";
  }
  return urlObj.searchParams.get("token") || "";
}
function tokenSearch(token) {
  return token ? `?token=${encodeURIComponent(token)}` : "";
}
async function diagnoseRemote(host, sessionName) {
  try {
    await execFileAsync("ssh", ["-o", "ConnectTimeout=3", host, "true"], { timeout: 5000 });
  } catch {
    return `Cannot reach ${host} — SSH connection failed`;
  }
  try {
    await execFileAsync("ssh", ["-o", "ConnectTimeout=3", host, `tmux has-session -t '${sessionName.replace(/'/g, "'\\''")}'`], { timeout: 5000 });
  } catch {
    return `Session ${sessionName} does not exist on ${host}`;
  }
  try {
    const { stdout } = await execFileAsync("ssh", [
      "-o",
      "ConnectTimeout=3",
      host,
      'ptys=$(sysctl -n kern.tty.ptmx_max 2>/dev/null || echo 0); used=$(lsof /dev/ptmx 2>/dev/null | tail -n+2 | wc -l | tr -d " "); echo "$used/$ptys"'
    ], { timeout: 5000 });
    const parts = stdout.trim().split("/");
    if (parts.length === 2) {
      const used = parseInt(parts[0], 10);
      const max = parseInt(parts[1], 10);
      if (max > 0 && used >= max) {
        return `PTY exhaustion on ${host}: ${used}/${max} PTYs in use — cannot allocate terminal (try killing Terminal.app or leaked processes)`;
      }
    }
  } catch {}
  return null;
}
function isValidHost(host) {
  return /^[A-Za-z0-9._-]+$/.test(host) && !host.startsWith("-");
}
async function execTmux(args, host) {
  if (host) {
    if (!isValidHost(host))
      throw new Error(`Invalid host: ${host}`);
    const quotedArgs = args.map((a) => `'${a.replace(/'/g, "'\\''")}'`);
    const remoteCmd = `tmux ${quotedArgs.join(" ")}`;
    return execFileAsync("ssh", ["-o", "ConnectTimeout=3", "--", host, remoteCmd]);
  }
  return execFileAsync("tmux", args);
}
async function listSessionsFrom(host) {
  let stdout = "";
  try {
    ({ stdout } = await execTmux([
      "list-sessions",
      "-F",
      "#{session_name}:#{session_attached}:#{session_activity}"
    ], host));
  } catch (error) {
    const stderr = `${error.stderr || ""}`.toLowerCase();
    if (stderr.includes("no server running")) {
      return [];
    }
    throw error;
  }
  return stdout.split(`
`).map((line) => line.trim()).filter(Boolean).map((line) => {
    const [name = "", attachedRaw = "0", activityRaw = "0"] = line.split(":");
    return {
      name,
      attached: attachedRaw === "1",
      activity: Number.parseInt(activityRaw, 10) || 0
    };
  }).filter((session) => isAllowedSessionName(session.name));
}
async function listSessions() {
  const local = await listSessionsFrom(null);
  const remoteHosts = new Set;
  for (const [, entry] of ENABLED_SESSIONS) {
    if (entry.host)
      remoteHosts.add(entry.host);
  }
  const remoteResults = await Promise.all([...remoteHosts].map((host) => listSessionsFrom(host).catch((err) => {
    console.log(`[pilot] remote list-sessions from ${host} failed: ${err.message}`);
    return [];
  })));
  const localNames = new Set(local.map((s) => s.name));
  for (const remoteSessions of remoteResults) {
    for (const session of remoteSessions) {
      if (!localNames.has(session.name)) {
        local.push(session);
      }
    }
  }
  return local;
}
function isEnabledSessionName(sessionName) {
  return ENABLED_SESSIONS.has(sessionName);
}
function getSessionHost(sessionName) {
  const entry = ENABLED_SESSIONS.get(sessionName);
  return entry ? entry.host : null;
}
function filterEnabledSessions(sessions) {
  return sessions.filter((session) => isEnabledSessionName(session.name));
}
async function sessionExists(sessionName) {
  try {
    const host = getSessionHost(sessionName);
    await execTmux(["has-session", "-t", sessionName], host);
    return true;
  } catch {
    return false;
  }
}
function sendJson(res, statusCode, data) {
  res.writeHead(statusCode, {
    "Content-Type": "application/json; charset=utf-8",
    "Cache-Control": "no-store"
  });
  res.end(JSON.stringify(data));
}
function sendText(res, statusCode, body) {
  res.writeHead(statusCode, { "Content-Type": "text/plain; charset=utf-8" });
  res.end(body);
}
function sendHtml(res, statusCode, html) {
  res.writeHead(statusCode, { "Content-Type": "text/html; charset=utf-8" });
  res.end(html);
}
async function serveFile(filePath, res) {
  const ext = path.extname(filePath);
  const contentType = MIME_TYPES[ext] || "application/octet-stream";
  try {
    const data = await fs.promises.readFile(filePath);
    res.writeHead(200, { "Content-Type": contentType });
    res.end(data);
  } catch {
    sendText(res, 404, "Not Found");
  }
}
function renderIndexPage({ sessions, token, enabledCount }) {
  const tokenQuery = tokenSearch(token);
  const hasEnabledSessions = enabledCount > 0;
  const emptyMessage = hasEnabledSessions ? "No enabled sessions are currently running." : "No sessions enabled.";
  const hintMessage = hasEnabledSessions ? "Open a session to attach. Use readonly mode to observe without sending input." : "Use /pilot &lt;name&gt; in Telegram to enable a session.";
  const listHtml = sessions.length ? sessions.map((session) => {
    const safeName = escapeHtml(session.name);
    const sessionHref = `/session/${encodeURIComponent(session.name)}${tokenQuery}`;
    const readonlyHref = `${sessionHref}${tokenQuery ? "&" : "?"}readonly=1`;
    return `<li class="session-item">
            <a class="session-link" href="${sessionHref}">
              <span class="name">${safeName}</span>
              <span class="meta">${session.attached ? "attached" : "detached"}</span>
            </a>
            <a class="readonly-link" href="${readonlyHref}">readonly</a>
          </li>`;
  }).join(`
`) : `<li class="empty">${emptyMessage}</li>`;
  return `<!doctype html>
<html lang="en">
  <head>
    <meta charset="utf-8" />
    <meta name="viewport" content="width=device-width, initial-scale=1" />
    <title>Pilot Session Picker</title>
    <style>
      :root {
        --bg: #0b1020;
        --card: #101a35;
        --line: #263456;
        --text: #eaf1ff;
        --muted: #94a3c6;
        --accent: #5fd0ff;
      }
      * { box-sizing: border-box; }
      body {
        margin: 0;
        min-height: 100vh;
        font-family: ui-sans-serif, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
        background: radial-gradient(100% 120% at 80% 0%, #14326d 0%, var(--bg) 60%);
        color: var(--text);
        padding: 24px;
      }
      .card {
        max-width: 860px;
        margin: 0 auto;
        background: color-mix(in oklab, var(--card) 85%, #000 15%);
        border: 1px solid var(--line);
        border-radius: 16px;
        overflow: hidden;
      }
      .header {
        display: flex;
        align-items: center;
        justify-content: space-between;
        gap: 12px;
        padding: 16px 20px;
        border-bottom: 1px solid var(--line);
      }
      h1 {
        margin: 0;
        font-size: 20px;
      }
      .btn {
        border: 1px solid var(--line);
        border-radius: 10px;
        background: transparent;
        color: var(--text);
        padding: 8px 12px;
        cursor: pointer;
      }
      ul {
        margin: 0;
        padding: 0;
        list-style: none;
      }
      .session-item {
        padding: 14px 20px;
        border-bottom: 1px solid var(--line);
        display: flex;
        align-items: center;
        justify-content: space-between;
        gap: 10px;
      }
      .session-link {
        color: inherit;
        text-decoration: none;
        display: inline-flex;
        flex-direction: column;
        gap: 4px;
      }
      .name {
        font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
      }
      .meta {
        color: var(--muted);
        font-size: 12px;
      }
      .readonly-link {
        color: var(--accent);
        text-decoration: none;
        font-size: 13px;
      }
      .empty {
        padding: 20px;
        color: var(--muted);
      }
      .hint {
        padding: 12px 20px 18px;
        color: var(--muted);
        font-size: 13px;
      }
      @media (max-width: 700px) {
        body { padding: 12px; }
        .session-item { padding: 12px; }
      }
    </style>
  </head>
  <body>
    <main class="card">
      <header class="header">
        <h1>Pilot Sessions</h1>
        <button class="btn" id="refresh">Refresh</button>
      </header>
      <ul id="session-list">
        ${listHtml}
      </ul>
      <div class="hint" id="hint">${hintMessage}</div>
    </main>
    <script type="module">
      const token = ${JSON.stringify(token)};
      let enabledCount = ${Number.isFinite(enabledCount) ? enabledCount : 0};
      const list = document.getElementById('session-list');
      const hint = document.getElementById('hint');
      const refresh = document.getElementById('refresh');
      const query = token ? ('?' + new URLSearchParams({ token }).toString()) : '';

      function emptyMessage() {
        return enabledCount > 0
          ? 'No enabled sessions are currently running.'
          : 'No sessions enabled.';
      }

      function hintMessage() {
        return enabledCount > 0
          ? 'Open a session to attach. Use readonly mode to observe without sending input.'
          : 'Use /pilot &lt;name&gt; in Telegram to enable a session.';
      }

      function render(items) {
        if (!Array.isArray(items) || items.length === 0) {
          list.innerHTML = '<li class="empty">' + emptyMessage() + '</li>';
          hint.textContent = hintMessage();
          return;
        }
        list.innerHTML = items.map((session) => {
          const safeName = String(session.name || '').replaceAll('&', '&amp;').replaceAll('<', '&lt;').replaceAll('>', '&gt;').replaceAll('"', '&quot;').replaceAll("'", '&#39;');
          const sessionHref = '/session/' + encodeURIComponent(session.name) + query;
          const readonlyHref = sessionHref + (query ? '&' : '?') + 'readonly=1';
          return '<li class="session-item">' +
            '<a class="session-link" href="' + sessionHref + '">' +
            '<span class="name">' + safeName + '</span>' +
            '<span class="meta">' + (session.attached ? 'attached' : 'detached') + '</span>' +
            '</a>' +
            '<a class="readonly-link" href="' + readonlyHref + '">readonly</a>' +
            '</li>';
        }).join('');
        hint.textContent = hintMessage();
      }

      async function loadSessions() {
        const [sessionsResponse, enabledResponse] = await Promise.all([
          fetch('/api/sessions' + query),
          fetch('/api/enabled' + query),
        ]);
        if (!sessionsResponse.ok || !enabledResponse.ok) {
          return;
        }
        const enabled = await enabledResponse.json();
        enabledCount = Array.isArray(enabled) ? enabled.length : 0;
        render(await sessionsResponse.json());
      }

      refresh.addEventListener('click', () => {
        loadSessions().catch(() => {});
      });
      loadSessions().catch(() => {});
    </script>
  </body>
</html>`;
}
function renderSessionPage({ sessionName, token, readonly, embed, hideheader }) {
  const hideChrome = embed ? "display:none !important;" : "";
  const hideBar = embed || hideheader ? "display:none !important;" : "";
  return `<!doctype html>
<html lang="en">
  <head>
    <meta charset="utf-8" />
    <meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover" />
    <title>Pilot: ${escapeHtml(sessionName)}</title>
    <style>
      :root {
        --bg: #04070f;
        --panel: #0f1528;
        --line: #293554;
        --text: #edf2ff;
        --muted: #90a0c2;
        --good: #4ade80;
        --warn: #f59e0b;
        --bad: #ef4444;
      }
      * { box-sizing: border-box; }
      body {
        margin: 0;
        min-height: 100vh;
        background: linear-gradient(180deg, #071026 0%, var(--bg) 100%);
        color: var(--text);
        font-family: ui-sans-serif, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      }
      .window {
        min-height: 100vh;
        display: flex;
        flex-direction: column;
      }
      .bar {
        display: flex;
        align-items: center;
        gap: 10px;
        padding: 10px 12px;
        background: var(--panel);
        border-bottom: 1px solid var(--line);
        ${hideBar}
      }
      .back {
        color: var(--text);
        text-decoration: none;
        font-size: 13px;
        border: 1px solid var(--line);
        border-radius: 8px;
        padding: 5px 9px;
      }
      .session {
        font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
        font-size: 13px;
      }
      .status {
        margin-left: auto;
        display: inline-flex;
        align-items: center;
        gap: 8px;
        color: var(--muted);
        font-size: 12px;
      }
      .dot {
        width: 10px;
        height: 10px;
        border-radius: 999px;
        background: var(--warn);
      }
      .dot.connected { background: var(--good); }
      .dot.disconnected { background: var(--bad); }
      .badge {
        margin-left: 8px;
        border: 1px solid var(--line);
        border-radius: 999px;
        padding: 2px 8px;
        font-size: 11px;
        color: var(--muted);
      }
      #terminal {
        flex: 1;
        min-height: 0;
        padding: ${embed ? "0" : "10px"};
        position: relative;
        ${embed ? "overflow: hidden;" : ""}
      }
      ${embed ? `
      #terminal *, #terminal ::-webkit-scrollbar { scrollbar-width: none; }
      #terminal *::-webkit-scrollbar { display: none; }
      html, body { overflow: hidden; }
      ` : ""}
      #mobile-input {
        position: absolute;
        top: 0; left: 0;
        width: 100%; height: 100%;
        opacity: 0;
        z-index: 10;
        font-size: 16px;
        resize: none;
        caret-color: transparent;
        -webkit-user-select: none;
        user-select: none;
      }
      .toolbar {
        display: none;
        gap: 6px;
        padding: 8px 10px;
        background: var(--panel);
        border-top: 1px solid var(--line);
        flex-wrap: wrap;
        position: fixed;
        bottom: 0;
        left: 0;
        right: 0;
        z-index: 20;
        ${hideChrome}
      }
      .toolbar button {
        background: #1a2744;
        color: var(--text);
        border: 1px solid var(--line);
        border-radius: 8px;
        padding: 10px 14px;
        font-size: 14px;
        font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
        cursor: pointer;
        -webkit-tap-highlight-color: transparent;
      }
      .toolbar button:active {
        background: #2a3f66;
      }
      .toolbar .key-enter { background: #1a3a2a; border-color: #2a5a3a; }
      #ctrl-btn.armed { background: #5f3a1a; border-color: #8a5a2a; }
      #select-overlay {
        position: fixed;
        inset: 0;
        display: none;
        background: rgba(3, 6, 13, 0.85);
        z-index: 30;
        padding: 14px;
        ${hideChrome}
      }
      .select-panel {
        width: 100%;
        height: 100%;
        background: #0a1326;
        border: 1px solid var(--line);
        border-radius: 12px;
        overflow: hidden;
        display: flex;
        flex-direction: column;
      }
      .select-header {
        display: flex;
        align-items: center;
        justify-content: flex-end;
        gap: 8px;
        padding: 10px;
        border-bottom: 1px solid var(--line);
        background: var(--panel);
      }
      .select-header button {
        background: #1a2744;
        color: var(--text);
        border: 1px solid var(--line);
        border-radius: 8px;
        padding: 8px 12px;
        font-size: 13px;
        font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
        cursor: pointer;
      }
      #select-text {
        margin: 0;
        padding: 14px;
        overflow: auto;
        flex: 1;
        white-space: pre-wrap;
        word-break: break-word;
        color: var(--text);
        font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
        font-size: 13px;
        line-height: 1.35;
        -webkit-user-select: text;
        user-select: text;
      }
      @media (pointer: coarse) {
        .toolbar { display: flex; }
        #mobile-input { pointer-events: none; }
        #terminal { padding-bottom: 56px; }
      }
      @media (pointer: fine) {
        #mobile-input { display: none; }
      }
    </style>
  </head>
  <body>
    <main class="window">
      <header class="bar">
        <a class="back" href="/${tokenSearch(token)}">sessions</a>
        <span class="session">${escapeHtml(sessionName)}</span>
        ${readonly ? '<span class="badge">readonly</span>' : ""}
        <span class="status"><span class="dot" id="status-dot"></span><span id="status-text">connecting</span></span>
      </header>
      <section id="terminal">
        <textarea id="mobile-input" autocapitalize="off" autocomplete="off" autocorrect="off" spellcheck="false" enterkeyhint="send"></textarea>
      </section>
      <nav class="toolbar" id="toolbar">
        <button class="key-enter" data-seq="cr">Enter</button>
        <button id="ctrl-btn">Ctrl</button>
        <button data-seq="esc">Esc</button>
        <button data-seq="up">▲</button>
        <button data-seq="down">▼</button>
        <button data-seq="pageup">PgUp</button>
        <button data-seq="pagedn">PgDn</button>
        <button data-seq="first">First</button>
        <button data-seq="last">Last</button>
        <button id="select-btn">Select</button>
      </nav>
    </main>
    <div id="select-overlay" style="display:none">
      <div class="select-panel">
        <div class="select-header">
          <button id="select-close">Close</button>
          <button id="select-copy-all">Copy All</button>
        </div>
        <pre id="select-text"></pre>
      </div>
    </div>

    <script type="module">
      try {
      const sessionName = ${JSON.stringify(sessionName)};
      const token = ${JSON.stringify(token)};
      const readonly = ${readonly ? "true" : "false"};
      const embed = ${embed ? "true" : "false"};
      const statusDot = document.getElementById('status-dot');
      const statusText = document.getElementById('status-text');

      function setStatus(type, text) {
        statusDot.className = 'dot ' + type;
        statusText.textContent = text;
      }

      const originalFetch = window.fetch.bind(window);
      window.fetch = (input, init) => {
        if (!token) {
          return originalFetch(input, init);
        }

        const url = new URL(typeof input === 'string' ? input : input.url, window.location.origin);
        if (url.origin === window.location.origin && !url.searchParams.has('token')) {
          url.searchParams.set('token', token);
        }
        if (typeof input === 'string') {
          return originalFetch(url.toString(), init);
        }
        return originalFetch(new Request(url.toString(), input), init);
      };

      let terminal, fitAddon;
      try {
        setStatus('', 'loading terminal...');
        const moduleUrl = '/dist/ghostty-web.js' + (token ? ('?' + new URLSearchParams({ token }).toString()) : '');
        const { init, Terminal, FitAddon } = await import(moduleUrl);
        await init();

        terminal = new Terminal({
          cols: 80,
          rows: 24,
          fontFamily: 'JetBrains Mono, Menlo, Monaco, monospace',
          fontSize: embed ? 13 : 14,
        });
        fitAddon = new FitAddon();
        terminal.loadAddon(fitAddon);

        await terminal.open(document.getElementById('terminal'));
        fitAddon.fit();
        fitAddon.observeResize();
      } catch (err) {
        setStatus('disconnected', 'terminal load failed');
        document.getElementById('terminal').innerHTML =
          '<pre style="color:#ef4444;padding:20px;font-size:14px;">Failed to load ghostty-web:\\n' +
          err.message + '\\n\\nThis may be a browser compatibility issue.\\nTry Chrome/Safari desktop.</pre>';
        throw err;
      }

      let ws;
      let reconnectTimer = null;
      let reconnectAttempts = 0;
      let reconnectStopped = false;

      function websocketUrl() {
        const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
        const url = new URL(protocol + '//' + window.location.host + '/ws');
        url.searchParams.set('session', sessionName);
        url.searchParams.set('cols', String(terminal.cols));
        url.searchParams.set('rows', String(terminal.rows));
        if (readonly) {
          url.searchParams.set('readonly', '1');
        }
        if (token) {
          url.searchParams.set('token', token);
        }
        return url.toString();
      }

      function connect() {
        if (reconnectStopped) return;
        setStatus('connecting', 'connecting');
        ws = new WebSocket(websocketUrl());

        ws.onopen = () => { setStatus('connected', 'connected'); reconnectAttempts = 0; };
        ws.onmessage = (event) => terminal.write(event.data);
        ws.onerror = () => setStatus('disconnected', 'error');
        ws.onclose = (event) => {
          setStatus('disconnected', 'disconnected');
          // code 4000 = diagnostic failure (PTY exhaustion, host unreachable, etc.)
          if (event.code === 4000) {
            reconnectStopped = true;
            terminal.write('\\r\\n\\x1b[33mReconnect stopped — fix the issue above, then refresh the page.\\x1b[0m\\r\\n');
            return;
          }
          reconnectAttempts++;
          const delay = Math.min(2000 * Math.pow(2, reconnectAttempts - 1), 30000);
          reconnectTimer = setTimeout(connect, delay);
        };
      }

      connect();

      terminal.onData((data) => {
        if (readonly) {
          return;
        }
        if (ws && ws.readyState === WebSocket.OPEN) {
          ws.send(data);
        }
      });

      terminal.onResize(({ cols, rows }) => {
        if (ws && ws.readyState === WebSocket.OPEN) {
          ws.send(JSON.stringify({ type: 'resize', cols, rows }));
        }
      });

      window.addEventListener('resize', () => {
        fitAddon.fit();
      });

      // Toolbar buttons (touch devices)
      const SEQ = { cr: '\\r', esc: '\\x1b', up: '\\x1b[A', down: '\\x1b[B', pageup: '\\x02\\x1b[5~', pagedn: '\\x1b[6~', last: 'q' };
      // First needs split sends (Ctrl+B [ then M-<) with delay so terminal parses correctly
      const MULTI = { first: ['\\x02[', '\\x1b<'] };
      document.querySelectorAll('#toolbar button[data-seq]').forEach((btn) => {
        btn.addEventListener('click', (e) => {
          e.preventDefault();
          if (readonly) return;
          if (!ws || ws.readyState !== WebSocket.OPEN) return;
          const key = btn.dataset.seq;
          if (MULTI[key]) {
            ws.send(MULTI[key][0]);
            setTimeout(() => ws.send(MULTI[key][1]), 150);
          } else if (SEQ[key]) {
            ws.send(SEQ[key]);
          }
        });
      });

      // Select mode overlay — fetches pane content for native text selection
      const selectOverlay = document.getElementById('select-overlay');
      const selectText = document.getElementById('select-text');
      const selectButton = document.getElementById('select-btn');
      const selectClose = document.getElementById('select-close');
      const selectCopyAll = document.getElementById('select-copy-all');
      let selectedPaneText = '';

      function closeSelectOverlay() {
        if (!selectOverlay) return;
        selectOverlay.style.display = 'none';
        document.body.style.overflow = '';
      }

      selectButton?.addEventListener('click', async (e) => {
        e.preventDefault();
        const btn = e.currentTarget;
        try {
          const r = await fetch('/api/capture?session=' + encodeURIComponent(sessionName));
          if (!r.ok) {
            throw new Error('capture failed');
          }
          const payload = await r.json();
          selectedPaneText = typeof payload.text === 'string' ? payload.text : '';
          if (selectText) {
            selectText.textContent = selectedPaneText;
          }
          if (selectOverlay) {
            selectOverlay.style.display = 'block';
            document.body.style.overflow = 'hidden';
          }
        } catch {
          btn.textContent = 'Failed';
          setTimeout(() => { btn.textContent = 'Select'; }, 1500);
        }
      });

      selectClose?.addEventListener('click', (e) => {
        e.preventDefault();
        closeSelectOverlay();
      });

      selectOverlay?.addEventListener('click', (e) => {
        if (e.target === selectOverlay) {
          closeSelectOverlay();
        }
      });

      selectCopyAll?.addEventListener('click', async (e) => {
        e.preventDefault();
        const btn = e.currentTarget;
        try {
          await navigator.clipboard.writeText(selectedPaneText);
          btn.textContent = 'Copied!';
        } catch {
          btn.textContent = 'Failed';
        }
        setTimeout(() => { btn.textContent = 'Copy All'; }, 1500);
      });

      // Tap terminal to bring up keyboard (touch devices)
      const mobileInput = document.getElementById('mobile-input');
      if (mobileInput) {
        document.getElementById('terminal')?.addEventListener('click', () => {
          mobileInput.style.pointerEvents = 'auto';
          mobileInput.focus();
          setTimeout(() => { mobileInput.style.pointerEvents = 'none'; }, 500);
        });
        mobileInput.addEventListener('input', (e) => {
          if (readonly) return;
          const text = e.target.value;
          if (text && ws && ws.readyState === WebSocket.OPEN) {
            if (ctrlArmed) {
              // Send Ctrl+<char> for each character
              for (const ch of text) {
                const code = ch.toUpperCase().charCodeAt(0);
                if (code >= 64 && code <= 95) {
                  ws.send(String.fromCharCode(code - 64));
                }
              }
              disarmCtrl();
            } else {
              ws.send(text);
            }
          }
          e.target.value = '';
        });
        mobileInput.addEventListener('keydown', (e) => {
          if (readonly) return;
          if (e.key === 'Enter') {
            e.preventDefault();
            if (ws && ws.readyState === WebSocket.OPEN) {
              ws.send('\\r');
            }
            if (ctrlArmed) disarmCtrl();
          }
        });
      }

      // Ctrl modifier button
      const ctrlBtn = document.getElementById('ctrl-btn');
      let ctrlArmed = false;
      function disarmCtrl() {
        ctrlArmed = false;
        if (ctrlBtn) ctrlBtn.classList.remove('armed');
      }
      ctrlBtn?.addEventListener('click', (e) => {
        e.preventDefault();
        ctrlArmed = !ctrlArmed;
        ctrlBtn.classList.toggle('armed', ctrlArmed);
        // Focus input so next keypress goes through
        if (ctrlArmed && mobileInput) {
          mobileInput.style.pointerEvents = 'auto';
          mobileInput.focus();
          setTimeout(() => { mobileInput.style.pointerEvents = 'none'; }, 500);
        }
      });

      // Handle iPad keyboard resize
      if (window.visualViewport) {
        const termEl = document.getElementById('terminal');
        const barEl = document.querySelector('.bar');
        const toolbarEl = document.getElementById('toolbar');
        window.visualViewport.addEventListener('resize', () => {
          const vh = window.visualViewport.height;
          const barH = barEl ? barEl.offsetHeight : 0;
          const tbH = toolbarEl ? toolbarEl.offsetHeight : 0;
          termEl.style.height = (vh - barH - tbH) + 'px';
          if (fitAddon) fitAddon.fit();
        });
      }

      window.addEventListener('beforeunload', () => {
        if (reconnectTimer) {
          clearTimeout(reconnectTimer);
        }
        if (ws && ws.readyState === WebSocket.OPEN) {
          ws.close();
        }
      });

      } catch (e) {
        const dot = document.getElementById('status-dot');
        const txt = document.getElementById('status-text');
        if (dot) dot.className = 'dot disconnected';
        if (txt) txt.textContent = 'error';
        const el = document.getElementById('terminal');
        if (el) el.innerHTML = '<pre style="color:#ef4444;padding:20px;font-size:14px;white-space:pre-wrap;">JS Error:\\n' + e.message + '\\n\\n' + (e.stack || '') + '</pre>';
      }
    </script>
  </body>
</html>`;
}
function renderGridPage({ token, slug, gridSession }) {
  const expiresAt = gridSession ? gridSession.expiresAt : 0;
  const filterSessions = gridSession ? JSON.stringify(gridSession.sessions) : "null";
  return `<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>Pilot${slug ? " — " + escapeHtml(slug) : ""}</title>
  <style>
    :root {
      --bg: #0a0a08;
      --surface: #1a1814;
      --border: #3d362e;
      --text: #40d870;
      --text-muted: #2a7a40;
      --green: #40d870;
      --accent: #60ff90;
      --hover: #252018;
    }
    * { box-sizing: border-box; margin: 0; padding: 0; }
    body {
      min-height: 100vh;
      background: var(--bg);
      color: var(--text);
      font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, monospace;
      overflow-x: hidden;
    }

    .topbar {
      display: flex;
      align-items: center;
      gap: 12px;
      padding: 10px 14px;
      background: var(--surface);
      border-bottom: 1px solid var(--border);
      position: sticky;
      top: 0;
      z-index: 10;
    }
    .topbar h1 {
      font-size: 14px;
      font-weight: 600;
      text-transform: uppercase;
      letter-spacing: 2px;
      text-shadow: 0 0 8px rgba(64, 216, 112, 0.5);
    }
    .worker-count { color: var(--text-muted); font-size: 12px; }
    .countdown {
      font-size: 11px;
      color: var(--text-muted);
      font-variant-numeric: tabular-nums;
    }
    .countdown.urgent { color: #f59e0b; text-shadow: 0 0 4px rgba(245, 158, 11, 0.4); }
    .countdown.expired { color: #ef4444; }
    .topbar-actions { margin-left: auto; }

    .btn {
      background: transparent;
      border: 1px solid var(--border);
      color: var(--text);
      border-radius: 4px;
      padding: 4px 10px;
      cursor: pointer;
      font-size: 11px;
      font-family: inherit;
      text-transform: uppercase;
      letter-spacing: 1px;
    }
    .btn:hover { background: var(--hover); text-shadow: 0 0 6px rgba(64, 216, 112, 0.4); }

    .grid {
      display: grid;
      grid-template-columns: repeat(auto-fill, minmax(min(480px, 100%), 1fr));
      gap: 10px;
      padding: 10px;
    }

    .panel {
      background: var(--surface);
      border: 1px solid var(--border);
      border-radius: 6px;
      overflow: hidden;
      cursor: pointer;
      transition: border-color 0.15s;
      display: flex;
      flex-direction: column;
    }
    .panel:hover { border-color: var(--accent); }

    .panel-header {
      display: flex;
      align-items: center;
      gap: 8px;
      padding: 6px 10px;
      border-bottom: 1px solid var(--border);
      flex-shrink: 0;
    }
    .status-dot {
      width: 7px;
      height: 7px;
      border-radius: 50%;
      background: var(--green);
      flex-shrink: 0;
      box-shadow: 0 0 4px var(--green);
      animation: pulse 2s ease-in-out infinite;
    }
    @keyframes pulse {
      0%, 100% { opacity: 1; }
      50% { opacity: 0.6; }
    }
    .worker-name {
      font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
      font-size: 13px;
      font-weight: 600;
      color: var(--green);
      text-shadow: 0 0 6px rgba(74, 222, 128, 0.3);
    }

    .panel-terminal {
      height: 260px;
      position: relative;
      background: #001200;
      border-radius: 12px;
      overflow: hidden;
      margin: 6px;
    }
    .panel-terminal iframe {
      width: 100%;
      height: 100%;
      border: none;
      pointer-events: none;
      filter: saturate(0.5) brightness(1.1);
    }
    /* CRT scanlines */
    .panel-terminal::after {
      content: '';
      position: absolute;
      inset: 0;
      background: repeating-linear-gradient(
        0deg,
        rgba(0,0,0,0.3) 0px,
        rgba(0,0,0,0.3) 1px,
        transparent 1px,
        transparent 2px
      );
      pointer-events: none;
      z-index: 2;
      border-radius: 12px;
    }
    /* CRT screen glow + heavy vignette */
    .panel-terminal::before {
      content: '';
      position: absolute;
      inset: 0;
      background: radial-gradient(
        ellipse 80% 80% at center,
        rgba(50, 200, 80, 0.06) 0%,
        rgba(30, 140, 50, 0.03) 40%,
        rgba(0, 0, 0, 0.7) 100%
      );
      pointer-events: none;
      z-index: 3;
      border-radius: 12px;
    }
    .panel {
      background: #001200;
      border: 1px solid #1a3a1a;
      border-radius: 8px;
      box-shadow: 0 0 12px rgba(50, 200, 80, 0.08);
    }
    .panel:hover {
      border-color: var(--green);
      box-shadow: 0 0 20px rgba(50, 200, 80, 0.15);
    }
    .panel-header {
      background: rgba(0, 18, 0, 0.8);
      border-bottom: 1px solid #1a3a1a;
    }
    /* Flicker animation */
    @keyframes flicker {
      0% { opacity: 1; }
      3% { opacity: 0.97; }
      6% { opacity: 1; }
      92% { opacity: 1; }
      94% { opacity: 0.96; }
      96% { opacity: 1; }
    }
    .panel-terminal iframe {
      animation: flicker 4s linear infinite;
    }

    .focus-overlay {
      position: fixed;
      inset: 0;
      background: var(--bg);
      z-index: 50;
      display: none;
      flex-direction: column;
    }
    .focus-overlay.active { display: flex; }

    .focus-bar {
      display: flex;
      align-items: center;
      gap: 12px;
      padding: 8px 14px;
      background: var(--surface);
      border-bottom: 1px solid var(--border);
    }
    .focus-worker-name {
      font-size: 13px;
      color: var(--green);
      text-shadow: 0 0 6px rgba(64, 216, 112, 0.4);
    }
    .focus-overlay iframe {
      flex: 1;
      border: none;
      width: 100%;
      background: #001200;
    }

    /* Solo mode — single worker, full screen */
    .solo {
      position: fixed;
      top: 38px;
      left: 0;
      right: 0;
      bottom: 0;
    }
    .solo iframe {
      width: 100%;
      height: 100%;
      border: none;
      background: #001200;
    }

    .empty-state {
      text-align: center;
      color: var(--text-muted);
      padding: 80px 20px;
      font-size: 14px;
    }

    @media (max-width: 500px) {
      .grid { grid-template-columns: 1fr; gap: 4px; padding: 4px; }
      .panel-terminal { height: 200px; }
    }
  </style>
</head>
<body>
  <header class="topbar">
    <h1>Pilot</h1>
    <span class="worker-count" id="worker-count"></span>
    <span class="countdown" id="countdown"></span>
    <div class="topbar-actions">
      <button class="btn" id="refresh-button">Refresh</button>
    </div>
  </header>

  <div class="grid" id="grid-container"><div class="empty-state">Loading...</div></div>

  <div class="focus-overlay" id="focus-overlay">
    <div class="focus-bar">
      <button class="btn" id="back-to-grid">&larr; Grid</button>
      <span class="focus-worker-name" id="focus-worker-name"></span>
    </div>
    <iframe id="focus-iframe" src="about:blank"></iframe>
  </div>

  <script>
    window.onerror = function(msg, src, line) {
      document.getElementById('grid-container').innerHTML =
        '<div class="empty-state">JS Error: ' + msg + ' (line ' + line + ')</div>';
    };
    var authToken = ${JSON.stringify(token)};
    var tokenQuery = authToken ? '?token=' + encodeURIComponent(authToken) : '';
    var expiresAt = ${expiresAt};
    var filterSessions = ${filterSessions};

    var gridContainer = document.getElementById('grid-container');
    var workerCountEl = document.getElementById('worker-count');
    var countdownEl = document.getElementById('countdown');
    var focusOverlay = document.getElementById('focus-overlay');
    var focusIframe = document.getElementById('focus-iframe');
    var focusWorkerName = document.getElementById('focus-worker-name');

    var sessions = [];

    function updateCountdown() {
      if (!expiresAt) return;
      var remaining = Math.max(0, Math.floor((expiresAt - Date.now()) / 1000));
      var min = Math.floor(remaining / 60);
      var sec = remaining % 60;
      countdownEl.textContent = min + ':' + (sec < 10 ? '0' : '') + sec;
      if (remaining <= 0) {
        countdownEl.textContent = 'expired';
        countdownEl.className = 'countdown expired';
      } else if (remaining <= 60) {
        countdownEl.className = 'countdown urgent';
      }
    }
    if (expiresAt) {
      updateCountdown();
      setInterval(updateCountdown, 1000);
    }

    function extractWorkerName(sessionName) {
      return sessionName.replace(/^claude-[^-]+-/, '');
    }

    function escapeHtml(str) {
      return String(str)
        .replace(/&/g, '&amp;')
        .replace(/</g, '&lt;')
        .replace(/>/g, '&gt;')
        .replace(/"/g, '&quot;');
    }

    function sessionUrl(sessionName, opts) {
      var url = '/session/' + encodeURIComponent(sessionName) + tokenQuery;
      var sep = tokenQuery ? '&' : '?';
      if (opts && opts.readonly) { url += sep + 'readonly=1'; sep = '&'; }
      if (opts && opts.embed) { url += sep + 'embed=1'; sep = '&'; }
      if (opts && opts.hideheader) { url += sep + 'hideheader=1'; sep = '&'; }
      return url;
    }

    function buildGrid() {
      if (!sessions.length) {
        var msg = filterSessions
          ? 'Sessions expired. Reconnecting...<br><small>Or run /pilot again in Telegram.</small>'
          : 'No sessions enabled.<br>Use /pilot name1 name2 in Telegram.';
        gridContainer.innerHTML = '<div class="empty-state">' + msg + '</div>';
        workerCountEl.textContent = '';
        return;
      }

      if (sessions.length === 1) {
        workerCountEl.textContent = extractWorkerName(sessions[0].name);
        gridContainer.className = 'solo';
        gridContainer.innerHTML =
          '<iframe src="' + escapeHtml(sessionUrl(sessions[0].name, { hideheader: true })) + '"></iframe>';
        return;
      }

      gridContainer.className = 'grid';
      workerCountEl.textContent = sessions.length + ' workers';

      gridContainer.innerHTML = sessions.map(function(session) {
        var name = extractWorkerName(session.name);
        var src = sessionUrl(session.name, { readonly: true, embed: true });
        return '<div class="panel" data-session="' + escapeHtml(session.name) + '">' +
          '<div class="panel-header">' +
          '<span class="status-dot"></span>' +
          '<span class="worker-name">' + escapeHtml(name) + '</span>' +
          '</div>' +
          '<div class="panel-terminal">' +
          '<iframe src="' + escapeHtml(src) + '" loading="lazy"></iframe>' +
          '</div>' +
          '</div>';
      }).join('');
    }

    function openFocusView(sessionName) {
      focusWorkerName.textContent = extractWorkerName(sessionName);
      focusIframe.src = sessionUrl(sessionName, {});
      focusOverlay.classList.add('active');
    }

    function closeFocusView() {
      focusOverlay.classList.remove('active');
      focusIframe.src = 'about:blank';
    }

    gridContainer.addEventListener('click', function(event) {
      if (sessions.length <= 1) return;
      var panel = event.target.closest('.panel');
      if (panel) openFocusView(panel.getAttribute('data-session'));
    });

    document.getElementById('back-to-grid').addEventListener('click', closeFocusView);

    document.addEventListener('keydown', function(event) {
      if (event.key === 'Escape' && focusOverlay.classList.contains('active')) {
        closeFocusView();
      }
    });

    document.getElementById('refresh-button').addEventListener('click', function() {
      loadEnabledSessions();
    });

    function cacheBust(url) {
      return url + (url.indexOf('?') === -1 ? '?' : '&') + '_t=' + Date.now();
    }

    async function loadEnabledSessions() {
      try {
        var response = await fetch(cacheBust('/api/enabled' + tokenQuery));
        if (!response.ok) return;
        var newSessions = await response.json();
        if (filterSessions) {
          newSessions = newSessions.filter(function(s) {
            return filterSessions.indexOf(s.name) !== -1;
          });
        }
        var oldKeys = sessions.map(function(s) { return s.name; }).sort().join(',');
        var newKeys = newSessions.map(function(s) { return s.name; }).sort().join(',');
        sessions = newSessions;
        if (oldKeys !== newKeys) buildGrid();
      } catch (err) { /* retry next interval */ }
    }

    // Keep sessions alive while the grid page is open
    var knownHosts = {}; // session name -> host, remembered from /api/enabled responses
    async function keepAlive() {
      if (!filterSessions) return;
      var wanted = JSON.parse(filterSessions);
      var enabledNames = sessions.map(function(s) { return s.name; });
      // Remember host info from currently enabled sessions
      for (var j = 0; j < sessions.length; j++) {
        if (sessions[j].host) knownHosts[sessions[j].name] = sessions[j].host;
      }
      for (var i = 0; i < wanted.length; i++) {
        var name = wanted[i];
        if (enabledNames.indexOf(name) !== -1) continue; // already enabled
        try {
          var url = '/api/pilot?session=' + encodeURIComponent(name);
          if (knownHosts[name]) url += '&host=' + encodeURIComponent(knownHosts[name]);
          await fetch(url + (tokenQuery ? '&' + tokenQuery.substring(1) : ''), { method: 'POST' });
        } catch (err) { /* best effort */ }
      }
    }

    (async function init() {
      try {
        await loadEnabledSessions();
        if (!sessions.length && filterSessions) {
          // Sessions not enabled yet (pilot restart or idle expiry) — re-enable immediately
          await keepAlive();
          await loadEnabledSessions();
        }
        buildGrid();
        setInterval(loadEnabledSessions, 15000);
        setInterval(keepAlive, 60000); // re-enable expired sessions every 60s
      } catch (err) {
        gridContainer.innerHTML = '<div class="empty-state">Error: ' + err.message + '</div>';
      }
    })();
  </script>
</body>
</html>`;
}
async function handleHttp(req, res) {
  const urlObj = new URL(req.url || "/", `http://${req.headers.host || "localhost"}`);
  const pathname = urlObj.pathname;
  if (!isAuthorized(urlObj)) {
    sendText(res, 401, "Unauthorized");
    return;
  }
  resetIdleTimer();
  if (pathname === "/debug") {
    sendHtml(res, 200, `<!doctype html>
<html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Pilot Debug</title>
<style>body{background:#111;color:#eee;font:14px monospace;padding:20px}#log{white-space:pre-wrap}.ok{color:#4ade80}.err{color:#ef4444}.warn{color:#f59e0b}</style>
</head><body><h2>Pilot Debug</h2><div id="log"></div>
<script type="module">
const log = document.getElementById('log');
function L(cls, msg) { log.innerHTML += '<div class="'+cls+'">[' + new Date().toISOString().substr(11,8) + '] ' + msg + '</div>'; }

L('ok', '1. Page loaded');
L('ok', '2. UserAgent: ' + navigator.userAgent);

// Test fetch
try {
  const r = await fetch('/api/sessions');
  const d = await r.json();
  L('ok', '3. /api/sessions: ' + d.length + ' sessions');
} catch(e) { L('err', '3. /api/sessions FAILED: ' + e.message); }

// Test WASM fetch
try {
  const r = await fetch('/ghostty-vt.wasm');
  L(r.ok ? 'ok' : 'err', '4. WASM fetch: HTTP ' + r.status + ' (' + r.headers.get('content-type') + ', ' + r.headers.get('content-length') + ' bytes)');
} catch(e) { L('err', '4. WASM fetch FAILED: ' + e.message); }

// Test ghostty-web JS module load
try {
  L('warn', '5. Loading ghostty-web module...');
  const mod = await import('/dist/ghostty-web.js');
  L('ok', '5. Module loaded, exports: ' + Object.keys(mod).join(', '));
  L('warn', '6. Calling init()...');
  await mod.init();
  L('ok', '6. init() OK');
} catch(e) { L('err', '5/6. ghostty-web FAILED: ' + e.message + '\\n' + e.stack); }

// Test WebSocket
try {
  L('warn', '7. Connecting WebSocket...');
  const ws = new WebSocket('ws://' + location.host + '/ws?session=claude-prod-lee&cols=80&rows=24');
  ws.onopen = () => { L('ok', '7. WebSocket OPEN'); ws.close(); };
  ws.onerror = (e) => L('err', '7. WebSocket ERROR');
  ws.onclose = (e) => L(e.code === 1000 ? 'ok' : 'warn', '7. WebSocket CLOSE code=' + e.code + ' reason=' + e.reason);
  setTimeout(() => { if (ws.readyState < 2) { L('err', '7. WebSocket TIMEOUT (5s)'); ws.close(); } }, 5000);
} catch(e) { L('err', '7. WebSocket FAILED: ' + e.message); }
</script></body></html>`);
    return;
  }
  if (pathname === "/" || pathname === "/index.html") {
    const sessions = filterEnabledSessions(await listSessions());
    sendHtml(res, 200, renderIndexPage({
      sessions,
      token: getTokenForTemplates(urlObj),
      enabledCount: ENABLED_SESSIONS.size
    }));
    return;
  }
  if (pathname === "/grid" || pathname.startsWith("/grid/")) {
    const slug = pathname === "/grid" ? null : decodeURIComponent(pathname.slice("/grid/".length));
    const gs = slug ? getGridSession(slug) : null;
    if (slug && !gs) {
      sendHtml(res, 404, `<!doctype html><html><body style="background:#0a0a08;color:#40d870;font-family:monospace;padding:40px;text-align:center">
<h2>Session expired</h2>
<p>This pilot session has ended (30min TTL).</p>
<p>Run <code>/pilot</code> again from Telegram to start a new session.</p>
<p style="margin-top:20px"><a href="/grid" style="color:#60ff90;text-decoration:underline">View all enabled sessions →</a></p>
</body></html>`);
      return;
    }
    sendHtml(res, 200, renderGridPage({
      token: getTokenForTemplates(urlObj),
      slug: slug || null,
      gridSession: gs
    }));
    return;
  }
  if (req.method === "POST" && pathname === "/api/grid-session") {
    try {
      const body = await new Promise((resolve, reject) => {
        let data = "";
        req.on("data", (c) => {
          data += c;
          if (data.length > 1e5)
            reject(new Error("too large"));
        });
        req.on("end", () => resolve(data));
        req.on("error", reject);
      });
      const { slug, sessions: sessionNames, ttl } = JSON.parse(body);
      if (!slug || !Array.isArray(sessionNames) || !sessionNames.length) {
        sendJson(res, 400, { error: "slug and sessions[] required" });
        return;
      }
      const ttlMs = Math.min((ttl || 300) * 1000, 30 * 60 * 1000);
      const result = createGridSession(slug, sessionNames, ttlMs);
      sendJson(res, 200, { ok: true, slug: result.slug, expiresAt: result.expiresAt });
    } catch (e) {
      sendJson(res, 400, { error: e.message });
    }
    return;
  }
  if (pathname === "/api/sessions") {
    const sessions = filterEnabledSessions(await listSessions());
    sendJson(res, 200, sessions);
    return;
  }
  if (pathname === "/api/enabled") {
    const entries = [...ENABLED_SESSIONS.keys()].sort().map((name) => {
      const entry = ENABLED_SESSIONS.get(name);
      return { name, host: entry ? entry.host : null };
    });
    sendJson(res, 200, entries);
    return;
  }
  if (pathname === "/api/pilot") {
    if (req.method !== "POST" && req.method !== "DELETE") {
      res.writeHead(405, { Allow: "POST, DELETE" });
      res.end("Method Not Allowed");
      return;
    }
    const sessionName = urlObj.searchParams.get("session") || "";
    const host = urlObj.searchParams.get("host") || null;
    if (!isAllowedSessionName(sessionName)) {
      sendText(res, 400, "Invalid session name");
      return;
    }
    if (host) {
      try {
        await execTmux(["has-session", "-t", sessionName], host);
      } catch {
        sendText(res, 404, "Session not found");
        return;
      }
    } else if (!await sessionExists(sessionName)) {
      sendText(res, 404, "Session not found");
      return;
    }
    if (req.method === "DELETE") {
      disableSession(sessionName);
      sendJson(res, 200, { ok: true, enabled: false });
    } else {
      enableSession(sessionName, host);
      sendJson(res, 200, { ok: true, enabled: true });
    }
    return;
  }
  if (pathname === "/api/capture") {
    const sessionName = urlObj.searchParams.get("session") || "";
    if (!isAllowedSessionName(sessionName) || !isEnabledSessionName(sessionName) || !await sessionExists(sessionName)) {
      sendText(res, 404, "Session not found");
      return;
    }
    try {
      refreshSessionTimer(sessionName);
      const host = getSessionHost(sessionName);
      const { stdout } = await execTmux(["capture-pane", "-t", sessionName, "-p"], host);
      sendJson(res, 200, { text: stdout });
    } catch {
      sendText(res, 500, "Capture failed");
    }
    return;
  }
  if (pathname.startsWith("/session/")) {
    let sessionName = "";
    try {
      sessionName = decodeURIComponent(pathname.slice("/session/".length));
    } catch {
      sendText(res, 404, "Session not found");
      return;
    }
    if (!isAllowedSessionName(sessionName)) {
      sendText(res, 404, "Session not found");
      return;
    }
    if (!isEnabledSessionName(sessionName)) {
      sendText(res, 404, "Session not found");
      return;
    }
    if (!await sessionExists(sessionName)) {
      sendText(res, 404, "Session not found");
      return;
    }
    refreshSessionTimer(sessionName);
    sendHtml(res, 200, renderSessionPage({
      sessionName,
      token: getTokenForTemplates(urlObj),
      readonly: urlObj.searchParams.get("readonly") === "1",
      embed: urlObj.searchParams.get("embed") === "1",
      hideheader: urlObj.searchParams.get("hideheader") === "1"
    }));
    return;
  }
  if (pathname.startsWith("/dist/")) {
    const relativePath = pathname.slice("/dist/".length);
    const requestedPath = path.resolve(distPath, relativePath);
    if (requestedPath !== distPath && !requestedPath.startsWith(`${distPath}${path.sep}`)) {
      sendText(res, 404, "Not Found");
      return;
    }
    await serveFile(requestedPath, res);
    return;
  }
  if (pathname === "/ghostty-vt.wasm") {
    await serveFile(wasmPath, res);
    return;
  }
  sendText(res, 404, "Not Found");
}
var httpServer = http.createServer((req, res) => {
  handleHttp(req, res).catch((error) => {
    console.error("HTTP error:", error);
    sendText(res, 500, "Internal Server Error");
  });
});
function sendUpgradeError(socket, statusCode, reason) {
  if (socket.destroyed) {
    return;
  }
  socket.write(`HTTP/1.1 ${statusCode} ${reason}\r
` + `Connection: close\r
` + `Content-Type: text/plain; charset=utf-8\r
` + `\r
` + `${reason}`);
  socket.destroy();
}
var wss = new import_websocket_server.default({ noServer: true });
async function handleUpgrade(req, socket, head) {
  const urlObj = new URL(req.url || "/", `http://${req.headers.host || "localhost"}`);
  if (urlObj.pathname !== "/ws") {
    sendUpgradeError(socket, 404, "Not Found");
    return;
  }
  if (!isAuthorized(urlObj)) {
    sendUpgradeError(socket, 401, "Unauthorized");
    return;
  }
  const sessionName = urlObj.searchParams.get("session") || "";
  if (!isAllowedSessionName(sessionName)) {
    sendUpgradeError(socket, 404, "Session not found");
    return;
  }
  if (!isEnabledSessionName(sessionName)) {
    sendUpgradeError(socket, 404, "Session not found");
    return;
  }
  if (!await sessionExists(sessionName)) {
    sendUpgradeError(socket, 404, "Session not found");
    return;
  }
  wss.handleUpgrade(req, socket, head, (ws) => {
    wss.emit("connection", ws, req);
  });
}
httpServer.on("upgrade", (req, socket, head) => {
  handleUpgrade(req, socket, head).catch((error) => {
    console.error("Upgrade error:", error);
    sendUpgradeError(socket, 500, "Internal Server Error");
  });
});
function closeConnection(ws) {
  const ptyProcess = ACTIVE_CONNECTIONS.get(ws);
  if (!ptyProcess) {
    return;
  }
  ACTIVE_CONNECTIONS.delete(ws);
  try {
    ptyProcess.kill();
  } catch {}
}
wss.on("connection", async (ws, req) => {
  const urlObj = new URL(req.url || "/", `http://${req.headers.host || "localhost"}`);
  const sessionName = urlObj.searchParams.get("session") || "";
  const readonly = urlObj.searchParams.get("readonly") === "1";
  const cols = parseIntOrDefault(urlObj.searchParams.get("cols"), 80);
  const rows = parseIntOrDefault(urlObj.searchParams.get("rows"), 24);
  console.log(`[ws] connect session=${sessionName} readonly=${readonly} cols=${cols} rows=${rows}`);
  resetIdleTimer();
  if (!isEnabledSessionName(sessionName)) {
    console.log(`[ws] session not enabled: ${sessionName}`);
    ws.send(`\r
\x1B[31mSession expired — run /pilot ${sessionName.replace(/^claude-prod-/, "")} again from Telegram\x1B[0m\r
`);
    ws.close(4000, "session not enabled");
    return;
  }
  refreshSessionTimer(sessionName);
  const host = getSessionHost(sessionName);
  if (host) {
    try {
      const diag = await diagnoseRemote(host, sessionName);
      if (diag) {
        console.log(`[ws] pre-flight failed session=${sessionName}: ${diag}`);
        ws.send(`\r
\x1B[31m${diag}\x1B[0m\r
`);
        ws.close(4000, diag);
        return;
      }
    } catch (err) {
      console.log(`[ws] pre-flight error session=${sessionName}: ${err.message}`);
    }
  }
  const ptyCmd = host ? { file: "ssh", args: ["-t", "-o", "ConnectTimeout=3", "--", host, "tmux", "attach-session", "-t", sessionName] } : { file: "tmux", args: ["attach-session", "-t", sessionName] };
  let ptyProcess;
  try {
    ptyProcess = import_node_pty.default.spawn(ptyCmd.file, ptyCmd.args, {
      name: "xterm-256color",
      cols,
      rows,
      env: {
        ...process.env,
        TERM: "xterm-256color",
        COLORTERM: "truecolor"
      }
    });
  } catch (err) {
    console.error(`[ws] PTY spawn failed session=${sessionName}: ${err.message}`);
    ws.send(`\r
\x1B[31mPTY allocation failed: ${err.message}\x1B[0m\r
`);
    ws.close(4001, "PTY spawn failed");
    return;
  }
  setTimeout(() => {
    execTmux(["resize-window", "-t", sessionName, "-x", String(cols), "-y", String(rows)], host).catch(() => {});
  }, 300);
  ACTIVE_CONNECTIONS.set(ws, ptyProcess);
  ws.__pilotSession = sessionName;
  ptyProcess.onData((data) => {
    if (ws.readyState === ws.OPEN) {
      ws.send(data);
    }
  });
  ptyProcess.onExit(({ exitCode }) => {
    console.log(`[ws] pty exit session=${sessionName} code=${exitCode}`);
    ACTIVE_CONNECTIONS.delete(ws);
    if (ws.readyState === ws.OPEN) {
      if (exitCode !== 0 && host) {
        diagnoseRemote(host, sessionName).then((reason) => {
          const msg = reason || `Session exited (code ${exitCode})`;
          ws.send(`\r
\x1B[31m${msg}\x1B[0m\r
`);
          ws.close(4000, msg);
        }).catch(() => {
          ws.send(`\r
\x1B[33mSession exited (code ${exitCode})\x1B[0m\r
`);
          ws.close();
        });
      } else {
        ws.send(`\r
\x1B[33mSession exited (code ${exitCode})\x1B[0m\r
`);
        ws.close();
      }
    }
  });
  ws.on("message", (raw) => {
    refreshSessionTimer(sessionName);
    resetIdleTimer();
    const message = raw.toString("utf8");
    if (message.startsWith("{")) {
      try {
        const payload = JSON.parse(message);
        if (payload.type === "resize") {
          const nextCols = parseIntOrDefault(String(payload.cols || ""), cols);
          const nextRows = parseIntOrDefault(String(payload.rows || ""), rows);
          ptyProcess.resize(nextCols, nextRows);
          execTmux(["resize-window", "-t", sessionName, "-x", String(nextCols), "-y", String(nextRows)], host).catch(() => {});
          return;
        }
      } catch {}
    }
    if (!readonly) {
      ptyProcess.write(message);
    }
  });
  ws.on("close", (code) => {
    console.log(`[ws] close session=${sessionName} code=${code}`);
    closeConnection(ws);
  });
  ws.on("error", (err) => {
    console.log(`[ws] error session=${sessionName}: ${err.message}`);
    closeConnection(ws);
  });
});
var shuttingDown = false;
function shutdown() {
  if (shuttingDown) {
    return;
  }
  shuttingDown = true;
  for (const [ws, ptyProcess] of ACTIVE_CONNECTIONS.entries()) {
    ACTIVE_CONNECTIONS.delete(ws);
    try {
      ptyProcess.kill();
    } catch {}
    try {
      ws.close();
    } catch {}
  }
  wss.close(() => {
    httpServer.close(() => {
      process.exit(0);
    });
  });
  setTimeout(() => process.exit(0), 1000).unref();
}
process.on("SIGINT", shutdown);
process.on("SIGTERM", shutdown);
if (!PILOT_TOKEN) {
  console.error("[pilot] ERROR: --token is required (auth fails closed without it)");
  process.exit(1);
}
restoreGridSessions();
var PILOT_HOST = (() => {
  const pubUrl = process.env.BRIDGE_PUBLIC_URL;
  if (pubUrl)
    try {
      return new URL(pubUrl).hostname;
    } catch {}
  return "127.0.0.1";
})();
httpServer.listen(PORT, PILOT_HOST, () => {
  console.log(`[pilot] listening on http://${PILOT_HOST}:${PORT} (TTL: ${CLI_ARGS.ttl}s, sessions: ${CLI_ARGS.sessions.length || "dynamic"})`);
  resetIdleTimer();
});
