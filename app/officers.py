# -*- coding: utf-8 -*-
"""
Officer accounts for Sewa Setu.

Until now the portal had ONE shared admin login, so a decision could never be recorded
"against her name". Here every officer has their own account:

    username  - the sign-in name; this is what is written in decided_by and the audit log
    full_name - for display
    block     - the block this officer is responsible for.
                NULL means the district level: this officer can see all six blocks.

The table is created (if missing) the first time it is used, so an existing database
needs no manual change. Passwords are stored hashed (salted), never as plain text.
The portal's original `admin` login keeps working and counts as district level.
"""

import os

from werkzeug.security import generate_password_hash, check_password_hash

import app as portal

# Accounts created automatically if they do not exist yet. They all start with the password
# in the OFFICER_PASSWORD setting (or, if that is empty, ADMIN_PASSWORD).
SEED_OFFICERS = [
    ("district.officer", "District Officer (all blocks)", None),
    ("ritu.hazarika", "Ritu Hazarika", "Sonari"),
    ("dhiren.baruah", "Dhiren Baruah", "Namti"),
]

_ready = False


def _ensure():
    """Create the officers table if needed, and add the starting accounts."""
    global _ready
    if _ready:
        return
    for attempt in (1, 2):          # two processes may start at the same moment; retry once
        conn = portal.get_db()
        try:
            cur = conn.cursor()
            cur.execute("""CREATE TABLE IF NOT EXISTS officers (
                               username      VARCHAR(40) PRIMARY KEY,
                               full_name     VARCHAR(100),
                               password_hash TEXT,
                               block         VARCHAR(50),
                               active        BOOLEAN DEFAULT TRUE,
                               created_at    TIMESTAMP DEFAULT now()
                           )""")
            conn.commit()
            password = os.environ.get("OFFICER_PASSWORD") or portal.ADMIN_PASSWORD
            if password:
                cur.execute("SELECT username FROM officers")
                have = {r[0] for r in cur.fetchall()}
                for username, full_name, block in SEED_OFFICERS:
                    if username not in have:
                        cur.execute("INSERT INTO officers (username, full_name, password_hash, block, active) "
                                    "VALUES (%s, %s, %s, %s, TRUE) ON CONFLICT (username) DO NOTHING",
                                    (username, full_name, generate_password_hash(password), block))
                conn.commit()
            cur.close()
            _ready = True
            return
        except Exception:
            conn.rollback()
            if attempt == 2:
                raise
        finally:
            conn.close()


def check_login(username, password):
    """The officer's account if the username and password are right, otherwise None."""
    _ensure()
    conn = portal.get_db()
    try:
        cur = conn.cursor()
        cur.execute("SELECT username, full_name, password_hash, block, active FROM officers "
                    "WHERE username = %s", (username,))
        row = cur.fetchone()
        cur.close()
    finally:
        conn.close()
    if row and row[4] and row[2] and check_password_hash(row[2], password):
        return {"username": row[0], "full_name": row[1], "block": row[3]}
    return None


def find(username):
    """An active officer's account (username, full_name, block), or None.
    block is None for the district level (all blocks)."""
    if username == portal.ADMIN_USERNAME:
        return {"username": username, "full_name": "Administrator", "block": None}
    _ensure()
    conn = portal.get_db()
    try:
        cur = conn.cursor()
        cur.execute("SELECT username, full_name, block, active FROM officers WHERE username = %s",
                    (username,))
        row = cur.fetchone()
        cur.close()
    finally:
        conn.close()
    if row and row[3]:
        return {"username": row[0], "full_name": row[1], "block": row[2]}
    return None
