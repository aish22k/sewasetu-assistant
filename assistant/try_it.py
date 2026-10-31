# -*- coding: utf-8 -*-
"""try_it.py - a helper to test the assistant by hand.

It acts like a real AI app: registers with the portal, signs the person in
(you only type the mobile number and the OTP), gets a token, then calls the MCP tools.
No browser link and no copy-paste of addresses is needed.
"""
import sys
import json
import base64
import hashlib
import secrets
from urllib.parse import urlencode, urlparse, parse_qs, urljoin

import requests

PORTAL_INTERNAL = "http://app:8000"            # how this script reaches the portal (docker network)
ASSISTANT_INTERNAL = "http://assistant:9000"   # how this script reaches the assistant
ASSISTANT_PUBLIC = "http://localhost:9000"     # the address inside the token ("audience")
REDIRECT = "http://localhost:8765/callback"    # nothing listens here; we only read the redirect

PATHS = {"citizen": "/mcp/citizen", "officer": "/mcp/officer"}


def b64(raw):
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def show(title, data):
    print("\n--- %s ---" % title)
    print(data if isinstance(data, str) else json.dumps(data, indent=2, ensure_ascii=False))


def step(r, what, expect=(302,)):
    """Stop with a clear message if the portal did not answer as expected."""
    if r.status_code not in expect:
        text = " ".join(r.text.split())[:300]
        sys.exit("\nSomething went wrong at: %s\nHTTP %s\n%s" % (what, r.status_code, text))


def sign_in(role, audience, creds=None):
    """Walk through the portal's own sign-in pages. Returns the redirect Location holding the code."""
    web = requests.Session()

    # 1. register this helper as an app
    r = web.post(PORTAL_INTERNAL + "/oauth/register", json={
        "client_name": "try_it helper", "redirect_uris": [REDIRECT],
        "token_endpoint_auth_method": "none"}, timeout=10)
    step(r, "registering the app", (201,))
    client_id = r.json()["client_id"]

    # 2. ask to sign in
    verifier = b64(secrets.token_bytes(32))
    challenge = b64(hashlib.sha256(verifier.encode()).digest())
    state = secrets.token_hex(8)
    r = web.get(PORTAL_INTERNAL + "/oauth/authorize?" + urlencode({
        "response_type": "code", "client_id": client_id, "redirect_uri": REDIRECT,
        "code_challenge": challenge, "code_challenge_method": "S256",
        "scope": role, "resource": audience, "state": state}),
        allow_redirects=False, timeout=10)
    step(r, "starting the sign-in")
    login_url = urljoin(PORTAL_INTERNAL, r.headers["Location"])
    if "/oauth/login" not in login_url:
        sys.exit("The portal sent me somewhere unexpected: " + login_url)

    # 3. sign in as the person
    if role == "citizen":
        mobile = input("\nMobile number (10 digits): ").strip()
        r = web.post(login_url, data={"mobile": mobile}, allow_redirects=False, timeout=10)
        step(r, "sending the OTP", (200,))
        print("\nAn OTP was sent. Open  http://localhost:8000/__gateway/  in your browser,")
        print("find the newest message for %s and read the 6-digit OTP." % mobile)
        for attempt in range(3):
            otp = input("Type the OTP here: ").strip()
            r = web.post(login_url, data={"otp": otp}, allow_redirects=False, timeout=10)
            if r.status_code == 302:
                break
            print("That OTP was not accepted. Try again.")
        else:
            sys.exit("Too many wrong OTPs. Please run the script again.")
    else:
        if creds:
            username, password = creds
        else:
            username = input("\nOfficer username [admin]: ").strip() or "admin"
            password = input("Officer password [Test12345]: ").strip() or "Test12345"
        r = web.post(login_url, data={"username": username, "password": password},
                     allow_redirects=False, timeout=10)
        if r.status_code != 302:
            sys.exit("Officer sign-in failed. Check the username and password.")

    # 4. consent ("Allow")
    consent_url = urljoin(PORTAL_INTERNAL, r.headers["Location"])
    r = web.post(consent_url, data={"decision": "allow"}, allow_redirects=False, timeout=10)
    step(r, "giving consent")
    q = parse_qs(urlparse(r.headers["Location"]).query)
    if "code" not in q:
        sys.exit("No code came back: " + r.headers["Location"])
    if q.get("state", [""])[0] != state:
        sys.exit("The 'state' did not match.")
    return client_id, q["code"][0], verifier


def get_token(role, creds=None):
    audience = ASSISTANT_PUBLIC + PATHS[role]
    client_id, code, verifier = sign_in(role, audience, creds)

    r = requests.post(PORTAL_INTERNAL + "/oauth/token", data={
        "grant_type": "authorization_code", "code": code, "code_verifier": verifier,
        "client_id": client_id, "redirect_uri": REDIRECT, "resource": audience}, timeout=10)
    step(r, "getting the token", (200,))
    tok = r.json()
    part = tok["access_token"].split(".")[1]
    claims = json.loads(base64.urlsafe_b64decode(part + "=" * (-len(part) % 4)))
    summary = {k: claims[k] for k in ("sub", "role", "scope", "aud", "iss") if k in claims}
    summary["valid_for_seconds"] = tok["expires_in"]
    show("You are signed in. The token says", summary)
    return tok["access_token"]


def mcp_call(path, token, method, params=None):
    r = requests.post(ASSISTANT_INTERNAL + path, json={
        "jsonrpc": "2.0", "id": 1, "method": method, "params": params or {}},
        headers={"Authorization": "Bearer " + token, "Content-Type": "application/json",
                 "Accept": "application/json, text/event-stream"}, timeout=30)
    body = r.text
    if "text/event-stream" in r.headers.get("content-type", ""):
        for line in body.splitlines():
            if line.startswith("data:"):
                body = line[5:].strip()
                break
    try:
        return r.status_code, json.loads(body)
    except ValueError:
        return r.status_code, body


def call_tool(path, token, name, arguments=None):
    status, data = mcp_call(path, token, "tools/call", {"name": name, "arguments": arguments or {}})
    show("%s (HTTP %s)" % (name, status), data.get("result", data) if isinstance(data, dict) else data)


def main():
    role = sys.argv[1] if len(sys.argv) > 1 else "citizen"
    if role not in PATHS:
        sys.exit("Use: citizen or officer")
    path = PATHS[role]
    token = get_token(role)

    status, data = mcp_call(path, token, "tools/list")
    names = [t["name"] for t in data["result"]["tools"]] if status == 200 else data
    show("Tools this endpoint offers (HTTP %s)" % status, names)

    if role == "citizen":
        call_tool(path, token, "get_my_application")
        dob = input("\nType a date of birth to test check_eligibility (DD/MM/YYYY), e.g. 15/05/1955: ").strip()
        call_tool(path, token, "check_eligibility", {"date_of_birth": dob})
        other = PATHS["officer"]
    else:
        call_tool(path, token, "who_am_i")
        call_tool(path, token, "queue_summary")
        other = PATHS["citizen"]

    # the safety test: this token must NOT work on the other endpoint
    status, data = mcp_call(other, token, "tools/list")
    show("SAFETY TEST: same token on the other endpoint (should be refused, 401 or 403)",
         "HTTP %s  %s" % (status, data))


if __name__ == "__main__":
    main()
