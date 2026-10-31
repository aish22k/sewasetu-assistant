# -*- coding: utf-8 -*-
"""
try_apply.py - plays one citizen applying for the pension through the assistant, and checks
the answers. It also tries things that must be REFUSED.

Run (from the project folder):
  docker compose run --rm --no-deps -v "${PWD}\\assistant:/srv/assistant" assistant python /srv/assistant/try_apply.py

Use a mobile number that has NO application yet (for example 9000000002).
"""
import sys
import json
import base64

import requests

from try_it import get_token, mcp_call, show, PATHS, ASSISTANT_INTERNAL, ASSISTANT_PUBLIC

# a real 1x1 PNG picture (the smallest valid image)
PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg==")

PATH = PATHS["citizen"]
results = []


def check(label, ok):
    results.append((label, ok))
    print("   [%s] %s" % ("PASS" if ok else "FAIL", label))


def tool(token, name, args=None):
    status, data = mcp_call(PATH, token, "tools/call", {"name": name, "arguments": args or {}})
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


GOOD = {
    "applicant_name": "Test Citizen One", "date_of_birth": "15/05/1955", "gender": "Female",
    "marital_status": "Widowed", "village": "Testgaon", "block": "Sonari",
    "bank_account": "123456789012", "ifsc": "SBIN0003077", "citizen_confirmed": True,
}


def summary(app_no):
    print("\n================ SUMMARY ================")
    failed = [l for l, ok in results if not ok]
    print("%d checks, %d passed, %d failed" % (len(results), len(results) - len(failed), len(failed)))
    for l in failed:
        print("  FAILED: " + l)
    if app_no:
        print("\nApplication number used in this test: %s (%s)"
              % (app_no, "left PENDING" if "--keep" in sys.argv else "now WITHDRAWN"))


def main():
    token = get_token("citizen")

    # safety: this test withdraws the application it creates, so never run it on a number
    # that already has a real, live application
    me0 = tool(token, "get_my_application")
    if me0.get("has_application") and me0.get("status") in ("PENDING", "APPROVED", "DEEMED_APPROVED"):
        sys.exit("\nThis mobile number already has a live application (%s, %s).\n"
                 "Nothing was changed. Please run the test again with a NEW mobile number."
                 % (me0.get("application_no"), me0.get("status")))

    print("\n== 1. Scheme facts (must come from the portal, not from memory) ==")
    info = tool(token, "get_scheme_info")
    check("scheme info says 250 rupees a month", info.get("pension_per_month_rupees") == 250)
    check("scheme info lists the 6 blocks", len(info.get("blocks", [])) == 6)

    print("\n== 2. Submitting with NO document must be refused ==")
    r = tool(token, "submit_application", GOOD)
    show("response", r)
    check("refused (no document yet)", r.get("submitted") is False)

    print("\n== 3. Upload page ==")
    link = tool(token, "get_document_upload_link").get("upload_link", "")
    check("got an upload link", "/upload/" in link)
    url = link.replace(ASSISTANT_PUBLIC, ASSISTANT_INTERNAL)
    bad = requests.post(url, files={"document": ("proof.png", b"this is just text, not a picture", "image/png")})
    check("a text file renamed .png is REFUSED", bad.status_code == 400 and "not a JPG" in bad.text)
    big = requests.post(url, files={"document": ("big.pdf", b"%PDF-" + b"0" * (5 * 1024 * 1024 + 10), "application/pdf")})
    check("a file over 5 MB is REFUSED", big.status_code == 400)
    ok = requests.post(url, files={"document": ("../../evil name.png", PNG, "image/png")})
    check("a real picture is accepted", ok.status_code == 200 and "Uploaded" in ok.text)
    tampered = requests.get(url[:-3] + "xyz")
    check("a tampered link is REFUSED", tampered.status_code == 400)
    r = tool(token, "upload_document", {"content_base64": base64.b64encode(b"just some text").decode()})
    check("direct upload of a non-picture is REFUSED", r.get("uploaded") is False)
    r = tool(token, "upload_document", {"content_base64": base64.b64encode(PNG).decode()})
    check("direct upload (base64) of a real picture is accepted", r.get("uploaded") is True)

    print("\n== 4. Things that must be refused even with a document ==")
    r = tool(token, "submit_application", dict(GOOD, citizen_confirmed=False))
    check("not confirmed by the citizen -> refused", r.get("submitted") is False)
    r = tool(token, "submit_application", dict(GOOD, gender="Boy"))
    check("invalid gender -> refused", r.get("submitted") is False)
    r = tool(token, "submit_application", dict(GOOD, date_of_birth="15/05/1990"))
    check("too young (under 60) -> refused", r.get("submitted") is False)
    r = tool(token, "submit_application", dict(GOOD, date_of_birth="1955"))
    check("date of birth without day and month -> refused", r.get("submitted") is False)
    r = tool(token, "submit_application", dict(GOOD, ifsc="BAD"))
    check("wrong IFSC -> refused", r.get("submitted") is False)
    r = tool(token, "submit_application", dict(GOOD, block="Mars"))
    check("block that does not exist -> refused", r.get("submitted") is False)

    print("\n== 5. A correct application ==")
    r = tool(token, "submit_application", GOOD)
    show("response", r)
    check("submitted, status PENDING", r.get("submitted") is True and r.get("status") == "PENDING")
    app_no = r.get("application_no")

    print("\n== 6. A second application for the same mobile must be refused ==")
    r2 = tool(token, "submit_application", GOOD)
    check("second application refused", r2.get("submitted") is False)
    check("the refusal is kind: says already applied, shows the number, offers choices",
          r2.get("already_applied") is True and r2.get("existing_application", {}).get("application_no") == app_no
          and len(r2.get("options_to_offer", [])) >= 3)
    me = tool(token, "get_my_application")
    check("the first application is untouched (still PENDING)", me.get("status") == "PENDING")

    print("\n== 7. Follow-up ==")
    me = tool(token, "get_my_application")
    check("my application is visible and PENDING", me.get("application_no") == app_no and me.get("status") == "PENDING")

    if "--keep" in sys.argv:
        print("\n== 8. Skipped (--keep): the application stays PENDING in the portal ==")
        summary(app_no)
        return

    print("\n== 8. Withdraw and re-apply path ==")
    r = tool(token, "withdraw_my_application", {"application_no": app_no, "reason": "wrong bank account", "citizen_confirmed": False})
    check("withdraw without confirmation -> refused", r.get("withdrawn") is False)
    r = tool(token, "withdraw_my_application", {"application_no": "SSP00000000", "reason": "wrong bank account", "citizen_confirmed": True})
    check("withdraw with a wrong application number -> refused", r.get("withdrawn") is False)
    me = tool(token, "get_my_application")
    check("still PENDING after the refused attempts", me.get("status") == "PENDING")
    r = tool(token, "withdraw_my_application", {"application_no": app_no, "reason": "", "citizen_confirmed": True})
    check("withdraw WITHOUT a reason -> refused", r.get("withdrawn") is False)
    me = tool(token, "get_my_application")
    check("still PENDING after the no-reason attempt", me.get("status") == "PENDING")
    r = tool(token, "withdraw_my_application", {"application_no": app_no, "reason": "test: wrong bank account", "citizen_confirmed": True})
    check("withdrawn", r.get("withdrawn") is True)
    me = tool(token, "get_my_application")
    check("status is now WITHDRAWN", me.get("status") == "WITHDRAWN")

    summary(app_no)


if __name__ == "__main__":
    main()
