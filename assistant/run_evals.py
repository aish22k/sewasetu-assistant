# -*- coding: utf-8 -*-
"""
run_evals.py - plays invented people against the Sewa Setu assistant and checks the database.

For each person in eval_personas.py:
  1. setup           put any needed records in the portal
  2. sign in         exactly like a real client: OAuth sign-in on the portal (OTP is read from the SMS
                     gateway for citizens, password for officers) -> token
  3. conversation    one AI plays the person, another AI plays the user's assistant. The assistant AI only
                     knows the MCP server's own instructions and tools, and calls the REAL endpoint.
  4. checks          look at the portal's database and at what the assistant said
  5. save            results go to /var/sewasetu/evals/results.json, shown at /eval-dashboard

Run (from the project folder, with the portal already started by docker compose up):
  $env:OPENAI_API_KEY="sk-..."
  docker compose run --rm --no-deps -v "${PWD}\\assistant:/srv/assistant" assistant python /srv/assistant/run_evals.py

Options:  run_evals.py kamala ramesh      only these people      run_evals.py --list     show the people
Environment: EVAL_MODEL (default gpt-4o-mini), EVAL_OFFICER_PASSWORD (default Test12345),
             EVAL_MAX_TURNS (default 14)
"""
import os
import re
import sys
import json
import time
import hashlib
import secrets
import traceback
from datetime import datetime
from urllib.parse import urlencode, urlparse, parse_qs, urljoin

import requests
import openai
from openai import OpenAI

from try_it import PORTAL_INTERNAL, ASSISTANT_INTERNAL, ASSISTANT_PUBLIC, REDIRECT, PATHS, b64, mcp_call
from eval_personas import PERSONAS, PNG, new_mobile, new_bank

RESULTS_PATH = os.environ.get("EVAL_RESULTS_PATH", "/var/sewasetu/evals/results.json")
MODEL = os.environ.get("EVAL_MODEL", "gpt-4o-mini")
OFFICER_PASSWORD = os.environ.get("EVAL_OFFICER_PASSWORD", "Test12345")
MAX_TURNS = int(os.environ.get("EVAL_MAX_TURNS", "22"))
MAX_TOOL_ROUNDS = 8


# ---------------------------------------------------------------------------
# signing in, like a real client (no typing: the OTP is read from the SMS gateway)
# ---------------------------------------------------------------------------

def _otp_ids(mobile):
    r = requests.get(PORTAL_INTERNAL + "/__gateway/api/messages", params={"to": mobile}, timeout=10)
    return r.json().get("messages", [])


def read_new_otp(mobile, after_id):
    for _ in range(25):
        for m in reversed(_otp_ids(mobile)):
            if m["id"] > after_id and m.get("delivered") and m.get("text"):
                found = re.search(r"OTP is (\d{6})", m["text"])
                if found:
                    return found.group(1)
        time.sleep(1)
    raise RuntimeError("the OTP SMS never arrived for " + mobile)


def get_token(role, mobile=None, creds=None):
    audience = ASSISTANT_PUBLIC + PATHS[role]
    web = requests.Session()
    r = web.post(PORTAL_INTERNAL + "/oauth/register", json={
        "client_name": "eval runner", "redirect_uris": [REDIRECT], "token_endpoint_auth_method": "none"}, timeout=10)
    r.raise_for_status()
    client_id = r.json()["client_id"]
    verifier = b64(secrets.token_bytes(32))
    challenge = b64(hashlib.sha256(verifier.encode()).digest())
    state = secrets.token_hex(8)
    r = web.get(PORTAL_INTERNAL + "/oauth/authorize?" + urlencode({
        "response_type": "code", "client_id": client_id, "redirect_uri": REDIRECT,
        "code_challenge": challenge, "code_challenge_method": "S256",
        "scope": role, "resource": audience, "state": state}), allow_redirects=False, timeout=10)
    login_url = urljoin(PORTAL_INTERNAL, r.headers["Location"])
    if role == "citizen":
        before = max([m["id"] for m in _otp_ids(mobile)] or [0])
        r = web.post(login_url, data={"mobile": mobile}, allow_redirects=False, timeout=10)
        r.raise_for_status()
        otp = read_new_otp(mobile, before)
        r = web.post(login_url, data={"otp": otp}, allow_redirects=False, timeout=10)
    else:
        r = web.post(login_url, data={"username": creds[0], "password": creds[1]}, allow_redirects=False, timeout=10)
    if r.status_code != 302:
        raise RuntimeError("sign-in failed (HTTP %s)" % r.status_code)
    consent = urljoin(PORTAL_INTERNAL, r.headers["Location"])
    r = web.post(consent, data={"decision": "allow"}, allow_redirects=False, timeout=10)
    q = parse_qs(urlparse(r.headers["Location"]).query)
    if "code" not in q:
        raise RuntimeError("no sign-in code came back")
    r = requests.post(PORTAL_INTERNAL + "/oauth/token", data={
        "grant_type": "authorization_code", "code": q["code"][0], "code_verifier": verifier,
        "client_id": client_id, "redirect_uri": REDIRECT, "resource": audience}, timeout=10)
    r.raise_for_status()
    return r.json()["access_token"]


# ---------------------------------------------------------------------------
# talking to the MCP endpoint
# ---------------------------------------------------------------------------

def server_instructions(role, token):
    status, data = mcp_call(PATHS[role], token, "initialize", {
        "protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "eval-runner", "version": "1"}})
    if status != 200 or not isinstance(data, dict):
        raise RuntimeError("initialize failed: HTTP %s %s" % (status, data))
    return data["result"].get("instructions", "")


def tool_definitions(role, token):
    status, data = mcp_call(PATHS[role], token, "tools/list")
    if status != 200:
        raise RuntimeError("tools/list failed: HTTP %s %s" % (status, data))
    return [{"type": "function", "function": {"name": t["name"], "description": t.get("description", ""),
                                             "parameters": t["inputSchema"]}}
            for t in data["result"]["tools"]]


def call_tool(role, token, name, args):
    status, data = mcp_call(PATHS[role], token, "tools/call", {"name": name, "arguments": args or {}})
    if status != 200 or not isinstance(data, dict) or "result" not in data:
        return "Error: HTTP %s %s" % (status, str(data)[:300]), True
    result = data["result"]
    text = "".join(c.get("text", "") for c in result.get("content", []) if c.get("type") == "text")
    return text, bool(result.get("isError"))


# ---------------------------------------------------------------------------
# the two AIs
# ---------------------------------------------------------------------------

def llm(client, **kw):
    for attempt in range(4):
        try:
            return client.chat.completions.create(**kw)
        except (openai.RateLimitError, openai.APIConnectionError, openai.InternalServerError):
            time.sleep(3 * (attempt + 1))
    return client.chat.completions.create(**kw)


def sim_prompt(p, ctx):
    return (
        "You are playing a real person in a test conversation with an AI assistant on a phone. Stay fully in "
        "character. Never say you are an AI and never mention a test.\n\n"
        "WHO YOU ARE: %s\n"
        "HOW YOU TALK: %s\n"
        "WHAT YOU WANT: %s\n"
        "FACTS ABOUT YOU (share a fact only when asked, one at a time, in your own words): %s\n"
        "HOW YOU BEHAVE: %s\n\n"
        "Rules: reply with one to three short sentences, the way a real person types. If the assistant gives you a "
        "link to upload a document, you have already opened it and uploaded your photo: just say that you did. "
        "When your goal is finished, or you give up, end your message with [END]."
        % (p["who"], p["style"], p["goal"], p["facts"].format(**ctx), p["behaviour"].format(**ctx)))


def make_judge(client):
    def judge(question, transcript):
        text = "\n".join("%s: %s" % (t["kind"].upper(), t["text"]) for t in transcript
                         if t["kind"] in ("citizen", "assistant"))
        r = llm(client, model=MODEL, max_completion_tokens=10, messages=[{
            "role": "user", "content": "Conversation:\n%s\n\nQuestion: %s\nAnswer with exactly YES or NO." % (text, question)}])
        return (r.choices[0].message.content or "").strip().upper().startswith("YES")
    return judge


def assistant_turn(client, system, tools, a_msgs, role, token, transcript, uploader):
    """The user's assistant answers once, calling tools as needed. Returns what it said plus notes
    about things the person did on the side (like uploading a document)."""
    spoken, notes = [], []
    for _ in range(MAX_TOOL_ROUNDS):
        resp = llm(client, model=MODEL, max_completion_tokens=1200, tools=tools,
                   messages=[{"role": "system", "content": system}] + a_msgs)
        msg = resp.choices[0].message
        if (msg.content or "").strip():
            spoken.append(msg.content.strip())
        calls = msg.tool_calls or []
        entry = {"role": "assistant", "content": msg.content or ""}
        if calls:
            entry["tool_calls"] = [{"id": c.id, "type": "function",
                                    "function": {"name": c.function.name, "arguments": c.function.arguments}}
                                   for c in calls]
        a_msgs.append(entry)
        if not calls:
            break
        for c in calls:
            try:
                args = json.loads(c.function.arguments or "{}")
            except ValueError:
                args = {}
            out, is_err = call_tool(role, token, c.function.name, args)
            transcript.append({"kind": "tool", "name": c.function.name, "args": args, "result": out[:1500],
                               "error": is_err})
            if c.function.name == "get_document_upload_link" and uploader:
                try:
                    link = json.loads(out)["upload_link"]
                    up = requests.post(link.replace(ASSISTANT_PUBLIC, ASSISTANT_INTERNAL),
                                       files={"document": ("proof.png", PNG, "image/png")}, timeout=20)
                    transcript.append({"kind": "event", "text": "The person opened the upload link and uploaded "
                                                                 "her age-proof photo (HTTP %s)." % up.status_code})
                    notes.append("(You opened the link and uploaded your document.)")
                except Exception as e:
                    transcript.append({"kind": "event", "text": "Upload step failed: %s" % e})
            a_msgs.append({"role": "tool", "tool_call_id": c.id, "content": out or "(empty)"})
    return " ".join(spoken) or "(no reply)", notes


def converse(client, p, ctx, role, token):
    transcript = []
    instructions = server_instructions(role, token)
    tools = tool_definitions(role, token)
    who = "a citizen's" if role == "citizen" else "a block officer's"
    system = ("You are an AI assistant on %s phone, connected to the Sewa Setu pension portal through the tools "
              "below. The portal gave you these instructions: %s" % (who, instructions))
    c_system = sim_prompt(p, ctx)
    opening = p["opening"].format(**ctx)
    a_msgs = []
    c_msgs = [{"role": "user", "content": "(You open the chat. Write your first message.)"},
              {"role": "assistant", "content": opening}]
    text = opening
    for _ in range(MAX_TURNS):
        transcript.append({"kind": "citizen", "text": text})
        a_msgs.append({"role": "user", "content": text})
        reply, notes = assistant_turn(client, system, tools, a_msgs, role, token, transcript,
                                      uploader=p.get("has_document"))
        transcript.append({"kind": "assistant", "text": reply})
        c_msgs.append({"role": "user", "content": reply + ("\n" + " ".join(notes) if notes else "")})
        r = llm(client, model=MODEL, max_completion_tokens=300,
                messages=[{"role": "system", "content": c_system}] + c_msgs)
        said = (r.choices[0].message.content or "").strip()
        c_msgs.append({"role": "assistant", "content": said or "..."})
        clean = said.replace("[END]", "").strip()
        if "[END]" in said:
            if clean:
                transcript.append({"kind": "citizen", "text": clean})
            break
        text = clean or "ok"
    return transcript


# ---------------------------------------------------------------------------
# running everyone and saving
# ---------------------------------------------------------------------------

def save_results(run):
    try:
        with open(RESULTS_PATH, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        data = {"runs": []}
    data["runs"] = [r for r in data.get("runs", []) if r["run_id"] != run["run_id"]] + [run]
    data["runs"] = data["runs"][-20:]
    os.makedirs(os.path.dirname(RESULTS_PATH), exist_ok=True)
    tmp = RESULTS_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=1, default=str)
    os.replace(tmp, RESULTS_PATH)


def run_one(client, judge, p):
    ctx = {"mobile": new_mobile(), "bank": new_bank(), "ifsc": "SBIN0003077", "judge": judge}
    ctx["bank_wrong"] = ctx["bank"][:-1] + str((int(ctx["bank"][-1]) + 1) % 10)
    if p.get("setup"):
        p["setup"](ctx)
    ctx["t0"] = datetime.now()
    if p["role"] == "citizen":
        token = get_token("citizen", mobile=ctx["mobile"])
    else:
        token = get_token("officer", creds=(p["officer"], OFFICER_PASSWORD))
    transcript = converse(client, p, ctx, p["role"], token)
    checks = [{"label": l, "ok": bool(ok)} for l, ok in p["checks"](ctx, transcript)]
    return transcript, checks


def main():
    if "--list" in sys.argv:
        for p in PERSONAS:
            print("%-28s %-8s %s" % (p["id"], p["role"], p["name"]))
        return
    key = os.environ.get("OPENAI_API_KEY", "").strip()
    if not key:
        sys.exit("OPENAI_API_KEY is not set.\nIn PowerShell:  $env:OPENAI_API_KEY=\"sk-...\"  and run again.")
    wanted = [a for a in sys.argv[1:] if not a.startswith("--")]
    people = [p for p in PERSONAS if not wanted or p["id"] in wanted]
    if not people:
        sys.exit("No such person. Use --list to see the ids.")

    client = OpenAI(api_key=key)
    judge = make_judge(client)
    run = {"run_id": datetime.now().strftime("%Y%m%d-%H%M%S"), "started_at": datetime.now().isoformat(timespec="seconds"),
           "finished_at": None, "model": MODEL, "personas": []}

    for p in people:
        print("\n=== %s (%s) ===" % (p["name"], p["role"]))
        t = time.time()
        rec = {"id": p["id"], "role": p["role"], "name": p["name"], "age": p["age"], "village": p["village"],
               "block": p["block"], "who": p["who"], "goal": p["goal"], "expect": p["expect"],
               "transcript": [], "checks": [], "error": None}
        try:
            rec["transcript"], rec["checks"] = run_one(client, judge, p)
        except Exception as e:
            rec["error"] = "%s: %s" % (type(e).__name__, e)
            traceback.print_exc()
        rec["seconds"] = int(time.time() - t)
        rec["passed"] = bool(rec["checks"]) and not rec["error"] and all(c["ok"] for c in rec["checks"])
        for c in rec["checks"]:
            print("   [%s] %s" % ("PASS" if c["ok"] else "FAIL", c["label"]))
        if rec["error"]:
            print("   [ERROR] " + rec["error"])
        run["personas"].append(rec)
        run["pass_rate"] = round(100.0 * sum(1 for r in run["personas"] if r["passed"]) / len(run["personas"]))
        save_results(run)

    run["finished_at"] = datetime.now().isoformat(timespec="seconds")
    save_results(run)
    done = sum(1 for r in run["personas"] if r["passed"])
    print("\n================ SUMMARY ================")
    print("%d of %d people fully passed (%s%%). Model: %s" % (done, len(run["personas"]), run["pass_rate"], MODEL))
    for r in run["personas"]:
        if not r["passed"]:
            print("  NOT PASSED: %s" % r["name"])
            for c in r["checks"]:
                if not c["ok"]:
                    print("      FAIL: " + c["label"])
            if r["error"]:
                print("      ERROR: " + r["error"])
    print("\nSee the page at  http://localhost:9000/eval-dashboard")


if __name__ == "__main__":
    main()
