# -*- coding: utf-8 -*-
"""
try_officer.py - an end-to-end check of the officer side.

It plays:
  * two citizens: one in Sonari, one in Namti (you type each mobile number and read each OTP)
  * Ritu Hazarika, the Sonari block officer
  * the district officer (all blocks)

and checks, one by one, that officers can do their job and that wrong actions are REFUSED.
Use TWO NEW mobile numbers that have no application yet. The test leaves one APPROVED and
one REJECTED application in the portal.

Run (from the project folder):
  docker compose run --rm --no-deps -v "${PWD}\\assistant:/srv/assistant" assistant python /srv/assistant/try_officer.py
"""
import sys
import json
import time
import base64

import requests

from try_it import get_token, mcp_call, PATHS, ASSISTANT_INTERNAL, ASSISTANT_PUBLIC

PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg==")

results = []


def check(label, ok):
    results.append((label, ok))
    print("   [%s] %s" % ("PASS" if ok else "FAIL", label))


def call(role, token, name, args=None):
    status, data = mcp_call(PATHS[role], token, "tools/call", {"name": name, "arguments": args or {}})
    if status != 200 or not isinstance(data, dict) or "result" not in data:
        return {"_error": "HTTP %s %s" % (status, data)}
    result = data["result"]
    text = result["content"][0]["text"] if result.get("content") else ""
    if result.get("isError"):
        return {"_error": text}
    try:
        return json.loads(text)
    except ValueError:
        return {"_raw": text}


def make_application(token, name, block, account):
    """A citizen applies through the assistant (upload the document, then submit)."""
    me = call("citizen", token, "get_my_application")
    if me.get("has_application") and me.get("status") in ("PENDING", "APPROVED", "DEEMED_APPROVED"):
        sys.exit("\nThis mobile number already has a live application (%s). Nothing was changed.\n"
                 "Please run again with NEW mobile numbers." % me.get("application_no"))
    link = call("citizen", token, "get_document_upload_link").get("upload_link", "")
    up = requests.post(link.replace(ASSISTANT_PUBLIC, ASSISTANT_INTERNAL),
                       files={"document": ("proof.png", PNG, "image/png")})
    if up.status_code != 200:
        sys.exit("The document upload failed: HTTP %s" % up.status_code)
    r = call("citizen", token, "submit_application", {
        "applicant_name": name, "date_of_birth": "15/05/1955", "gender": "Female",
        "marital_status": "Widowed", "village": "Testgaon", "block": block,
        "bank_account": account, "ifsc": "SBIN0003077", "citizen_confirmed": True})
    if not r.get("submitted"):
        sys.exit("The test application was not accepted: %s" % r)
    return r["application_no"]


def main():
    password = input("Officer password [Test12345]: ").strip() or "Test12345"
    account = "88" + str(int(time.time()))[-10:]       # the SAME bank account in both applications

    print("\n=== Citizen A: applies in the SONARI block ===")
    tok_a = get_token("citizen")
    app_a = make_application(tok_a, "Test Sewali Sonari", "Sonari", account)
    print("   created application", app_a)

    print("\n=== Citizen B: applies in the NAMTI block ===")
    tok_b = get_token("citizen")
    app_b = make_application(tok_b, "Test Mohan Namti", "Namti", account)
    print("   created application", app_b)

    print("\n=== Officer Ritu Hazarika (Sonari) signs in ===")
    ritu = get_token("officer", ("ritu.hazarika", password))

    print("\n== 1. Who am I, and what is waiting ==")
    me = call("officer", ritu, "who_am_i")
    check("Ritu is a Sonari officer", me.get("block") == "Sonari")
    q = call("officer", ritu, "queue_summary")
    check("queue summary is limited to Sonari", q.get("your_scope") == "Sonari"
          and set(q.get("pending_by_block", {}).keys()) <= {"Sonari"})
    lst = call("officer", ritu, "list_pending_applications")
    nos = [c["application_no"] for c in lst.get("oldest_first", [])]
    check("her list contains the Sonari application", app_a in nos)
    check("her list does NOT contain the Namti application", app_b not in nos)
    check("list shows days left before auto-approval",
          all("days_left_before_auto_approval" in c for c in lst.get("oldest_first", [])))
    other = call("officer", ritu, "list_pending_applications", {"block": "Namti"})
    check("asking for another block's list is refused", "problem" in other)

    print("\n== 2. Looking at cases ==")
    case = call("officer", ritu, "get_application", {"application_no": app_a})
    check("she can open the Sonari case", case.get("found") is True and case.get("can_be_decided") is True)
    check("bank account is masked", case.get("bank_account", "").startswith("XXXX")
          and account not in json.dumps(case))
    check("document link is provided", case.get("document", {}).get("available") is True)
    link = case.get("document", {}).get("view_link", "")
    if link:
        doc = requests.get(link.replace(ASSISTANT_PUBLIC, ASSISTANT_INTERNAL))
        check("the document link shows the picture", doc.status_code == 200 and "image/png" in doc.headers.get("content-type", ""))
        bad = requests.get(link.replace(ASSISTANT_PUBLIC, ASSISTANT_INTERNAL)[:-3] + "xyz")
        check("a tampered document link is refused", bad.status_code == 400)
    check("it warns that the same bank account is used elsewhere",
          any("bank account" in f for f in case.get("things_to_check", [])))
    nb = call("officer", ritu, "get_application", {"application_no": app_b})
    check("she CANNOT open the Namti case", nb.get("found") is False)

    print("\n== 3. Deciding: things that must be REFUSED ==")
    r = call("officer", ritu, "approve_application", {"application_no": app_b, "reason": "ok", "officer_confirmed": True})
    check("approving a Namti case is refused", r.get("decided") is False)
    r = call("citizen", tok_b, "get_my_application")
    check("the Namti application is still PENDING", r.get("status") == "PENDING")
    r = call("officer", ritu, "approve_application", {"application_no": app_a, "reason": "documents checked", "officer_confirmed": False})
    check("approving without the officer's clear yes is refused", r.get("decided") is False)
    r = call("officer", ritu, "approve_application", {"application_no": app_a, "reason": "", "officer_confirmed": True})
    check("approving WITHOUT a reason is refused", r.get("decided") is False)
    r = call("officer", ritu, "reject_application", {"application_no": app_a, "reason": "", "officer_confirmed": True})
    check("rejecting WITHOUT a reason is refused", r.get("decided") is False)
    r = call("officer", ritu, "approve_application", {"application_no": "SSP00000000", "reason": "ok", "officer_confirmed": True})
    check("an application number that does not exist is refused", r.get("decided") is False)
    r = call("citizen", tok_a, "get_my_application")
    check("the Sonari application is still PENDING", r.get("status") == "PENDING")

    print("\n== 4. Ritu approves the Sonari case, with a reason ==")
    r = call("officer", ritu, "approve_application", {
        "application_no": app_a, "reason": "Age proof checked and matches the details.", "officer_confirmed": True})
    print("   ", r)
    check("approved", r.get("decided") is True and r.get("status") == "APPROVED")
    check("recorded against Ritu's name", "ritu.hazarika" in r.get("recorded_against", ""))
    r = call("officer", ritu, "approve_application", {"application_no": app_a, "reason": "again", "officer_confirmed": True})
    check("deciding the same case again is refused", r.get("decided") is False)
    r = call("citizen", tok_a, "get_my_application")
    check("the citizen now sees APPROVED", r.get("status") == "APPROVED")

    print("\n=== District officer (all blocks) signs in ===")
    dist = get_token("officer", ("district.officer", password))
    me = call("officer", dist, "who_am_i")
    check("district officer covers all blocks", "all blocks" in str(me.get("block")))
    lst = call("officer", dist, "list_pending_applications")
    check("district list contains the Namti application", app_b in [c["application_no"] for c in lst.get("oldest_first", [])])

    print("\n== 5. The district officer rejects the Namti case ==")
    reason = "The age proof photo is not readable. Please apply again with a clear photo."
    r = call("officer", dist, "reject_application", {"application_no": app_b, "reason": reason, "officer_confirmed": True})
    print("   ", r)
    check("rejected", r.get("decided") is True and r.get("status") == "REJECTED")
    r = call("citizen", tok_b, "get_my_application")
    check("the citizen sees REJECTED and the reason", r.get("status") == "REJECTED" and r.get("reason_given_by_officer") == reason)

    print("\n== 6. The record shows WHO decided, and why ==")
    case = call("officer", dist, "get_application", {"application_no": app_a})
    hist = case.get("history", [])
    check("history shows APPROVED by officer:ritu.hazarika with the reason",
          any(h.get("action") == "APPROVED" and h.get("by") == "officer:ritu.hazarika" and h.get("note") for h in hist))
    check("decided_by on the application is ritu.hazarika", case.get("decided_by") == "ritu.hazarika")

    print("\n================ SUMMARY ================")
    failed = [l for l, ok in results if not ok]
    print("%d checks, %d passed, %d failed" % (len(results), len(results) - len(failed), len(failed)))
    for l in failed:
        print("  FAILED: " + l)
    print("\nApplications used: %s (Sonari, now APPROVED), %s (Namti, now REJECTED)" % (app_a, app_b))


if __name__ == "__main__":
    main()
