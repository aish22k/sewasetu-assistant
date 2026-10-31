# -*- coding: utf-8 -*-
"""dashboard.py - the /eval-dashboard page. Reads the results that run_evals.py saved."""
import os
import json
import html

RESULTS_PATH = os.environ.get("EVAL_RESULTS_PATH", "/var/sewasetu/evals/results.json")

CSS = """
:root{--bg:#f6f7f9;--card:#fff;--text:#1d2733;--muted:#5b6673;--line:#dde2e8;--ok:#14733a;--bad:#b3261e;--accent:#1a5fb4}
@media (prefers-color-scheme:dark){:root{--bg:#14181d;--card:#1d232a;--text:#e8ecf0;--muted:#9aa6b2;--line:#2d3640;--ok:#5fd08a;--bad:#ff8a80;--accent:#7db3ff}}
*{box-sizing:border-box}
body{margin:0;font-family:Arial,Helvetica,sans-serif;background:var(--bg);color:var(--text);line-height:1.5}
.wrap{max-width:980px;margin:0 auto;padding:16px}
h1{font-size:26px;margin:8px 0 4px} h2{font-size:20px;margin:24px 0 8px} h3{font-size:17px;margin:0}
.muted{color:var(--muted);font-size:14px}
.card{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:14px;margin:12px 0}
.top{display:flex;gap:12px;flex-wrap:wrap}
.stat{flex:1;min-width:150px;background:var(--card);border:1px solid var(--line);border-radius:10px;padding:12px}
.stat b{font-size:28px;display:block}
.pill{display:inline-block;border-radius:99px;padding:2px 10px;font-size:13px;font-weight:bold;border:1px solid}
.pill.ok{color:var(--ok);border-color:var(--ok)} .pill.bad{color:var(--bad);border-color:var(--bad)}
.head{display:flex;justify-content:space-between;gap:8px;align-items:flex-start;flex-wrap:wrap}
ul{margin:6px 0 6px 18px;padding:0}
li.ok::marker{color:var(--ok)} li.bad::marker{color:var(--bad)}
.ok-t{color:var(--ok);font-weight:bold} .bad-t{color:var(--bad);font-weight:bold}
details{margin-top:8px} summary{cursor:pointer;color:var(--accent);font-weight:bold}
.msg{margin:6px 0;padding:8px 10px;border-radius:8px;border:1px solid var(--line);white-space:pre-wrap;word-wrap:break-word;font-size:14px}
.msg.citizen{border-left:4px solid #b36b00} .msg.assistant{border-left:4px solid var(--accent)}
.msg.tool{font-family:Consolas,monospace;font-size:12px;border-left:4px solid var(--muted);color:var(--muted)}
.msg.event{font-style:italic;color:var(--muted)}
.who{font-size:12px;color:var(--muted);text-transform:uppercase}
table{border-collapse:collapse;width:100%;font-size:14px;background:var(--card)}
td,th{border:1px solid var(--line);padding:6px 8px;text-align:left}
"""


def e(x):
    return html.escape(str(x if x is not None else ""))


def load():
    try:
        with open(RESULTS_PATH, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {"runs": []}


def _msg(t):
    k = t.get("kind")
    if k in ("citizen", "assistant"):
        label = ("Officer" if False else "Person") if k == "citizen" else "Assistant"
        return "<div class='msg %s'><div class='who'>%s</div>%s</div>" % (k, label, e(t.get("text")))
    if k == "tool":
        args = json.dumps(t.get("args"), ensure_ascii=False)
        return ("<div class='msg tool'>tool call: <b>%s</b>(%s)%s<br>&rarr; %s</div>"
                % (e(t.get("name")), e(args[:300]), " [error]" if t.get("error") else "", e((t.get("result") or "")[:500])))
    return "<div class='msg event'>%s</div>" % e(t.get("text"))


def _persona(r):
    ok = r.get("passed")
    pill = "<span class='pill %s'>%s</span>" % ("ok" if ok else "bad", "PASSED" if ok else "NOT PASSED")
    checks = "".join("<li class='%s'><span class='%s'>%s</span> %s</li>"
                     % ("ok" if c["ok"] else "bad", "ok-t" if c["ok"] else "bad-t",
                        "PASS" if c["ok"] else "FAIL", e(c["label"])) for c in r.get("checks", []))
    expect = "".join("<li>%s</li>" % e(x) for x in r.get("expect", []))
    convo = "".join(_msg(t) for t in r.get("transcript", []))
    tools = [t["name"] for t in r.get("transcript", []) if t.get("kind") == "tool"]
    err = "<p class='bad-t'>Error: %s</p>" % e(r["error"]) if r.get("error") else ""
    return (
        "<div class='card'><div class='head'><div><h3>%s</h3>"
        "<div class='muted'>%s &middot; age %s &middot; %s block &middot; %s</div></div>%s</div>"
        "<p>%s</p><p><b>Wants:</b> %s</p>"
        "<p><b>What must be true in the portal at the end:</b></p><ul>%s</ul>"
        "<p><b>What was checked:</b></p><ul>%s</ul>%s"
        "<details><summary>Read the conversation (%d tool calls: %s)</summary>%s</details></div>"
        % (e(r.get("name")), "Officer" if r.get("role") == "officer" else "Citizen", e(r.get("age")), e(r.get("block")),
           e("%ss" % r.get("seconds", "?")), pill, e(r.get("who")), e(r.get("goal")), expect, checks, err,
           len(tools), e(", ".join(tools) or "none"), convo))


def render_html():
    runs = load().get("runs", [])
    if not runs:
        body = ("<h1>Sewa Setu assistant: evals</h1><p>No eval run has been saved yet. "
                "Run <code>run_evals.py</code> and refresh this page.</p>")
        return PAGE % (CSS, body)
    latest = runs[-1]
    people = latest.get("personas", [])
    passed = sum(1 for r in people if r.get("passed"))
    checks = [c for r in people for c in r.get("checks", [])]
    cpass = sum(1 for c in checks if c["ok"])
    state = "finished " + e(latest.get("finished_at")) if latest.get("finished_at") else "still running"
    top = ("<div class='top'><div class='stat'><b>%d / %d</b>people fully passed</div>"
           "<div class='stat'><b>%d / %d</b>database and behaviour checks passed</div>"
           "<div class='stat'><b>%s</b>model playing the assistant and the people</div></div>"
           % (passed, len(people), cpass, len(checks), e(latest.get("model"))))
    history = "".join("<tr><td>%s</td><td>%s</td><td>%s%%</td><td>%d</td></tr>"
                      % (e(r.get("started_at")), e(r.get("model")), e(r.get("pass_rate", "-")), len(r.get("personas", [])))
                      for r in reversed(runs))
    body = (
        "<h1>Sewa Setu assistant: evals</h1>"
        "<p class='muted'>Invented people talk to an AI assistant that uses the real citizen and officer MCP "
        "endpoints. Afterwards the portal's database is read to see what is really there. Latest run: %s (%s).</p>"
        "%s<h2>The people and what happened</h2>%s"
        "<h2>Earlier runs</h2><table><tr><th>Started</th><th>Model</th><th>Pass rate</th><th>People</th></tr>%s</table>"
        % (e(latest.get("run_id")), state, top, "".join(_persona(r) for r in people), history))
    return PAGE % (CSS, body)


PAGE = ("<!doctype html><html lang='en'><head><meta charset='utf-8'>"
        "<meta name='viewport' content='width=device-width, initial-scale=1'>"
        "<title>Sewa Setu assistant: evals</title><style>%s</style></head><body><div class='wrap'>%s</div></body></html>")
