package connector

import (
	"bytes"
	"fmt"
	"html/template"
	"strings"
)

func renderUserMsg(text string) string {
	var buf bytes.Buffer
	userMsgTmpl.Execute(&buf, map[string]template.HTML{"Body": RenderMarkdown(text)})
	return buf.String()
}

func renderWorkerMsg(worker, text string) string {
	var buf bytes.Buffer
	workerMsgTmpl.Execute(&buf, map[string]any{
		"From": worker,
		"Body": RenderMarkdown(text),
	})
	return buf.String()
}

func renderAuthCard(msgID, worker, url string) string {
	var buf bytes.Buffer
	authCardTmpl.Execute(&buf, map[string]string{
		"MessageID": msgID,
		"Worker":    worker,
		"URL":       url,
	})
	return buf.String()
}

func renderAuthStatus(status, detail string) string {
	icon := map[string]string{
		"submitting": "⏳",
		"verifying":  "🔄",
		"success":    "✓",
		"failed":     "✗",
	}[status]
	cls := "status"
	if status == "success" {
		cls = "status success"
	} else if status == "failed" {
		cls = "status error"
	}
	var buf bytes.Buffer
	authStatusTmpl.Execute(&buf, map[string]string{
		"Icon":   icon,
		"Detail": detail,
		"Class":  cls,
	})
	return buf.String()
}

func renderCommandBar(commands []CommandSpec) template.HTML {
	if len(commands) == 0 {
		return ""
	}
	var buf bytes.Buffer
	cmdBarTmpl.Execute(&buf, commands)
	return template.HTML(buf.String())
}

func writeSSETo(w interface{ Write([]byte) (int, error) }, event, data string) {
	data = strings.ReplaceAll(data, "\n", "")
	fmt.Fprintf(w, "event: %s\ndata: %s\n\n", event, data)
}

var pageTmpl = template.Must(template.New("page").Parse(`<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{{.Name}} — Forge</title>
<script src="https://unpkg.com/htmx.org@2.0.4/dist/htmx.min.js"></script>
<script src="https://unpkg.com/htmx-ext-sse@2.2.2/sse.js"></script>
<style>
*{margin:0;padding:0;box-sizing:border-box}
:root{--bg:#0d1117;--surface:#161b22;--border:#30363d;--text:#e6edf3;--muted:#8b949e;--accent:#58a6ff;--user-bg:#1f6feb;--worker-bg:#21262d;--auth-bg:#2d1b00;--auth-border:#9e6a03;--green:#3fb950;--red:#f85149;--font:-apple-system,BlinkMacSystemFont,"Segoe UI",Helvetica,Arial,sans-serif;--mono:ui-monospace,SFMono-Regular,"SF Mono",Menlo,Consolas,monospace}
html,body{height:100%;font-family:var(--font);background:var(--bg);color:var(--text)}
#app{display:flex;flex-direction:column;height:100%;max-width:800px;margin:0 auto}
header{padding:12px 16px;border-bottom:1px solid var(--border);display:flex;align-items:center;gap:8px}
header h1{font-size:14px;font-weight:600}
header .dot{width:8px;height:8px;border-radius:50%;background:var(--green)}
header .tag{font-size:11px;color:var(--muted);background:var(--surface);padding:2px 8px;border-radius:10px;border:1px solid var(--border)}
#cmd-bar{padding:6px 16px;border-bottom:1px solid var(--border);display:flex;flex-wrap:wrap;gap:6px}
#cmd-bar button{background:var(--surface);color:var(--muted);border:1px solid var(--border);padding:4px 10px;border-radius:6px;cursor:pointer;font-size:12px;font-family:var(--mono);transition:all .15s}
#cmd-bar button:hover{color:var(--text);border-color:var(--accent);background:rgba(88,166,255,.1)}
#messages{flex:1;overflow-y:auto;padding:16px;display:flex;flex-direction:column;gap:8px}
.msg{max-width:85%;padding:8px 12px;border-radius:12px;font-size:14px;line-height:1.5;word-wrap:break-word}
.msg a{color:var(--accent);text-decoration:none;word-break:break-all}
.msg a:hover{text-decoration:underline}
.msg code{font-family:var(--mono);font-size:13px;background:rgba(110,118,129,.4);padding:1px 4px;border-radius:4px}
.msg pre{background:rgba(0,0,0,.3);padding:8px;border-radius:6px;overflow-x:auto;margin:4px 0}
.msg pre code{background:none;padding:0}
.msg.user{align-self:flex-end;background:var(--user-bg);border-bottom-right-radius:4px}
.msg.worker{align-self:flex-start;background:var(--worker-bg);border:1px solid var(--border);border-bottom-left-radius:4px}
.msg.auth{align-self:flex-start;background:var(--auth-bg);border:1px solid var(--auth-border);border-bottom-left-radius:4px;max-width:95%}
.msg.status{align-self:center;background:var(--surface);border:1px solid var(--border);border-radius:8px;font-size:13px;color:var(--muted);padding:6px 14px;max-width:90%;text-align:center}
.msg.status.success{color:var(--green);border-color:var(--green)}
.msg.status.error{color:var(--red);border-color:var(--red)}
.msg .from{font-size:11px;color:var(--muted);margin-bottom:2px}
.auth-body{margin:8px 0}
.auth-link{margin:8px 0;word-break:break-all}
.auth-form{display:flex;gap:8px;margin-top:10px}
.auth-form input[type=text]{flex:1;background:var(--bg);border:1px solid var(--border);color:var(--text);padding:6px 8px;border-radius:6px;font-size:13px;font-family:var(--mono)}
.auth-form button{background:var(--accent);color:#fff;border:none;padding:6px 14px;border-radius:6px;cursor:pointer;font-size:13px;font-weight:500}
.auth-form button:hover{opacity:.85}
.auth-form button:disabled{opacity:.5;cursor:not-allowed}
.auth-form .submitted{color:var(--green);font-size:13px;padding:6px 0}
#input-bar{padding:12px 16px;border-top:1px solid var(--border)}
#input-bar form{display:flex;gap:8px}
#input-bar input{flex:1;background:var(--surface);border:1px solid var(--border);color:var(--text);padding:10px 14px;border-radius:8px;font-size:14px;outline:none}
#input-bar input:focus{border-color:var(--accent)}
#input-bar button{background:var(--accent);color:#fff;border:none;padding:10px 20px;border-radius:8px;cursor:pointer;font-size:14px;font-weight:500}
#input-bar button:hover{opacity:.85}
.empty{color:var(--muted);text-align:center;margin:auto;font-size:14px}
.htmx-indicator{display:none}
.htmx-request .htmx-indicator{display:inline}
.htmx-request .hide-on-request{display:none}
</style>
</head>
<body>
<div id="app">
<header>
  <span class="dot"></span>
  <h1>{{.Name}}</h1>
  <span class="tag">forge web</span>
</header>
{{.CommandBar}}
<div id="messages" hx-ext="sse" sse-connect="/events" sse-swap="message" hx-swap="beforeend scroll:bottom">
  <div class="empty" id="empty-msg">Send a message to start</div>
</div>
<div id="input-bar">
  <form hx-post="/api/send" hx-target="#messages" hx-swap="beforeend scroll:bottom" hx-on::after-request="this.reset();var e=document.getElementById('empty-msg');if(e)e.remove()">
    <input type="text" name="text" placeholder="Type a message or /command..." autocomplete="off" autofocus>
    <button type="submit">Send</button>
  </form>
</div>
</div>
<script>
document.body.addEventListener('htmx:sseBeforeMessage',function(e){
  var empty=document.getElementById('empty-msg');
  if(empty)empty.remove();
});
document.body.addEventListener('authCodeSubmitted',function(e){
  var id=e.detail.value;
  var form=document.getElementById('auth-form-'+id);
  if(form){form.innerHTML='<div class="submitted">✓ Code submitted</div>';}
});
function sendCmd(cmd){
  var input=document.querySelector('#input-bar input[name=text]');
  if(input){input.value='/'+cmd;input.closest('form').dispatchEvent(new Event('submit',{bubbles:true}));}
}
</script>
</body>
</html>`))

var userMsgTmpl = template.Must(template.New("user-msg").Parse(
	`<div class="msg user">{{.Body}}</div>`))

var workerMsgTmpl = template.Must(template.New("worker-msg").Parse(
	`<div class="msg worker"><div class="from">{{.From}}</div>{{.Body}}</div>`))

var authCardTmpl = template.Must(template.New("auth-card").Parse(
	`<div class="msg auth"><div class="from">auth required — {{.Worker}}</div><div class="auth-body">Open this URL to authenticate:</div><div class="auth-link"><a href="{{.URL}}" target="_blank" rel="noopener">{{.URL}}</a></div><form id="auth-form-{{.MessageID}}" class="auth-form" hx-post="/api/send" hx-target="#messages" hx-swap="beforeend scroll:bottom"><input type="hidden" name="reply_to_id" value="{{.MessageID}}"><input type="text" name="text" placeholder="Paste auth code here..." autocomplete="off"><button type="submit"><span class="hide-on-request">Submit</span><span class="htmx-indicator">Sending...</span></button></form></div>`))

var authStatusTmpl = template.Must(template.New("auth-status").Parse(
	`<div class="msg {{.Class}}">{{.Icon}} {{.Detail}}</div>`))

var cmdBarTmpl = template.Must(template.New("cmd-bar").Parse(
	`<div id="cmd-bar">{{range .}}<button onclick="sendCmd('{{.Name}}')" title="{{.Description}}">/{{.Name}}</button>{{end}}</div>`))
