"""Writes evals/cases/*: seeded-bug PRs with expected findings and a recorded model answer.

Line numbers in expected findings and recorded answers are resolved from anchor text, so the
cases stay correct when their code is edited. Run: uv run python evals/build_cases.py
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any

ROOT = Path(__file__).parent / "cases"

AUTH_BASE = '''from app.auth import require_role
from app.db import users


def get_user(request, user_id):
    require_role(request, "admin")
    return users.get(user_id)


def update_user(request, user_id, data):
    require_role(request, "admin")
    users.update(user_id, data)
    return {"ok": True}
'''
AUTH_HEAD = AUTH_BASE + '''

def delete_user(request, user_id):
    users.delete(user_id)
    return {"ok": True}
'''
AUTH_LIB = '''def require_role(request, role):
    if role not in request.user.roles:
        raise PermissionError(role)
'''
DB_LIB = '''class _Users:
    def get(self, user_id):
        return None

    def update(self, user_id, data):
        return None

    def delete(self, user_id):
        return None


users = _Users()
'''

CASES: list[dict[str, Any]] = [
    {
        "name": "01-py-missing-auth-guard",
        "base": {"app/admin.py": AUTH_BASE, "app/auth.py": AUTH_LIB, "app/db.py": DB_LIB},
        "head": {"app/admin.py": AUTH_HEAD, "app/auth.py": AUTH_LIB, "app/db.py": DB_LIB},
        "expected": [{"file": "app/admin.py", "anchor": "users.delete(user_id)",
                      "category": "auth"}],
        "recorded": [
            {"file": "app/admin.py", "anchor": "users.delete(user_id)", "severity": "blocker",
             "category": "auth", "title": "delete_user has no role check",
             "body": "Every other handler calls require_role first; this one deletes any user.",
             "evidence_quote": "users.delete(user_id)",
             "claims": [{"kind": "not_calls", "caller": "delete_user", "callee": "require_role"},
                        {"kind": "calls", "caller": "get_user", "callee": "require_role"}]},
            {"file": "app/admin.py", "anchor": "users.delete(user_id)", "severity": "major",
             "category": "auth", "title": "update_user skips the role check",
             "body": "update_user writes without authorisation.",
             "evidence_quote": "users.update(user_id, data)",
             "claims": [{"kind": "not_calls", "caller": "update_user",
                         "callee": "require_role"}]},
        ],
    },
    {
        "name": "02-py-off-by-one",
        "base": {"stats.py": "def total(xs):\n    return 0\n"},
        "head": {"stats.py": '''def total(xs):
    result = 0
    for i in range(1, len(xs)):
        result += xs[i]
    return result
'''},
        "expected": [{"file": "stats.py", "anchor": "for i in range(1, len(xs)):",
                      "category": "correctness"}],
        "recorded": [
            {"file": "stats.py", "anchor": "for i in range(1, len(xs)):", "severity": "major",
             "category": "correctness", "title": "First element is skipped",
             "body": "range starts at 1, so xs[0] is never added.",
             "evidence_quote": "for i in range(1, len(xs)):",
             "claims": [{"kind": "symbol_exists", "name": "total"}]},
            {"file": "stats.py", "anchor": "result += xs[i]", "severity": "major",
             "category": "correctness", "title": "total() should use safe_sum",
             "body": "Use the safe_sum helper instead of a manual loop.",
             "evidence_quote": "result += xs[i]",
             "claims": [{"kind": "symbol_exists", "name": "safe_sum"}]},
        ],
    },
    {
        "name": "03-py-sql-string-join",
        "base": {"repo/orders.py": '''def find_order(db, order_id):
    return db.execute("SELECT * FROM orders WHERE id = ?", (order_id,)).fetchone()
'''},
        "head": {"repo/orders.py": '''def find_order(db, order_id):
    return db.execute("SELECT * FROM orders WHERE id = ?", (order_id,)).fetchone()


def find_by_customer(db, name):
    return db.execute(f"SELECT * FROM orders WHERE customer = '{name}'").fetchall()
'''},
        "expected": [{"file": "repo/orders.py", "anchor": "WHERE customer =",
                      "category": "sql-injection"}],
        "recorded": [],
    },
    {
        "name": "04-py-none-dereference",
        "base": {"svc/profile.py": '''def find_user(store, user_id):
    return store.get(user_id)
'''},
        "head": {"svc/profile.py": '''def find_user(store, user_id):
    return store.get(user_id)


def display_name(store, user_id):
    user = find_user(store, user_id)
    return user["first"] + " " + user["last"]
'''},
        "expected": [{"file": "svc/profile.py", "anchor": 'return user["first"]',
                      "category": "correctness"}],
        "recorded": [
            {"file": "svc/profile.py", "anchor": 'return user["first"]', "severity": "major",
             "category": "correctness", "title": "Crashes when the user does not exist",
             "body": "find_user returns store.get(...), which is None for a missing id.",
             "evidence_quote": 'return user["first"] + " " + user["last"]',
             "claims": [{"kind": "calls", "caller": "display_name", "callee": "find_user"},
                        {"kind": "quote", "file": "svc/profile.py",
                         "text": "return store.get(user_id)"}]},
            {"file": "svc/profile.py", "anchor": "def find_user", "severity": "minor",
             "category": "correctness", "title": "find_user does not validate user_id",
             "body": "Old function, not changed here.", "evidence_quote": "def find_user(store, user_id):",
             "claims": []},
        ],
    },
    {
        "name": "05-py-committed-secret",
        "base": {"settings.py": 'import os\n\nREGION = "us-east-1"\n'},
        "head": {"settings.py": 'import os\n\nREGION = "us-east-1"\n'
                                 'PAYMENT_API_SECRET = "q8Zr2LmN4vT7wX1yK3pD"\n'},
        "expected": [{"file": "settings.py", "anchor": "PAYMENT_API_SECRET", "category": "secret"}],
        "recorded": [],
    },
    {
        "name": "06-py-personal-data-in-logs",
        "base": {"signup.py": '''import logging

logger = logging.getLogger(__name__)


def signup(form):
    return {"id": 1}
'''},
        "head": {"signup.py": '''import logging

logger = logging.getLogger(__name__)


def signup(form):
    logger.info("new signup %s %s", form.email, form.phone)
    return {"id": 1}
'''},
        "expected": [{"file": "signup.py", "anchor": "logger.info",
                      "category": "personal-data-in-logs"}],
        "recorded": [],
    },
    {
        "name": "07-py-swallowed-payment-error",
        "base": {"billing/charge.py": '''def charge(gateway, amount):
    return gateway.charge(amount)
'''},
        "head": {"billing/charge.py": '''def charge(gateway, amount):
    try:
        gateway.charge(amount)
    except Exception:
        pass
    return {"status": "paid"}
'''},
        "expected": [{"file": "billing/charge.py", "anchor": "except Exception:",
                      "category": "error-handling"}],
        "recorded": [
            {"file": "billing/charge.py", "anchor": "except Exception:", "severity": "blocker",
             "category": "error-handling", "title": "Failed charges are reported as paid",
             "body": "The exception is swallowed and the function still returns paid.",
             "evidence_quote": "except Exception:\n        pass",
             "claims": [{"kind": "quote", "file": "billing/charge.py",
                         "text": 'return {"status": "paid"}'}]},
        ],
    },
    {
        "name": "08-ts-missing-await",
        "base": {"src/orders.ts": '''import { saveOrder } from "./db";

export async function placeOrder(order: { id: string }): Promise<string> {
  return "pending";
}
''', "src/db.ts": '''export async function saveOrder(order: { id: string }): Promise<void> {
  if (!order.id) {
    throw new Error("missing id");
  }
}
'''},
        "head": {"src/orders.ts": '''import { saveOrder } from "./db";

export async function placeOrder(order: { id: string }): Promise<string> {
  try {
    saveOrder(order);
  } catch (err) {
    return "failed";
  }
  return "saved";
}
''', "src/db.ts": '''export async function saveOrder(order: { id: string }): Promise<void> {
  if (!order.id) {
    throw new Error("missing id");
  }
}
'''},
        "expected": [{"file": "src/orders.ts", "anchor": "saveOrder(order);",
                      "category": "async"}],
        "recorded": [
            {"file": "src/orders.ts", "anchor": "saveOrder(order);", "severity": "major",
             "category": "async", "title": "saveOrder is not awaited",
             "body": "The rejection escapes the try/catch, so failures still return saved.",
             "evidence_quote": "saveOrder(order);",
             "claims": [{"kind": "calls", "caller": "placeOrder", "callee": "saveOrder"},
                        {"kind": "symbol_exists", "name": "saveOrder"}]},
            {"file": "src/orders.ts", "anchor": "saveOrder(order);", "severity": "major",
             "category": "async", "title": "placeOrder never validates the order",
             "body": "placeOrder should call validateOrder before saving.",
             "evidence_quote": "saveOrder(order);",
             "claims": [{"kind": "symbol_exists", "name": "validateOrder"}]},
        ],
    },
    {
        "name": "09-ts-assignment-in-condition",
        "base": {"src/invoice.ts": '''export function label(inv: { status: string }): string {
  return "unknown";
}
'''},
        "head": {"src/invoice.ts": '''export function label(inv: { status: string }): string {
  if (inv.status = "paid") {
    return "Paid";
  }
  return "Due";
}
'''},
        "expected": [{"file": "src/invoice.ts", "anchor": 'if (inv.status = "paid")',
                      "category": "correctness"}],
        "recorded": [
            {"file": "src/invoice.ts", "anchor": 'if (inv.status = "paid")', "severity": "major",
             "category": "correctness", "title": "Assignment instead of comparison",
             "body": "This sets status to paid and is always true. Use ===.",
             "evidence_quote": 'if (inv.status === "paid") {', "claims": []},
            {"file": "src/invoice.ts", "anchor": 'if (inv.status = "paid")', "severity": "major",
             "category": "correctness", "title": "Assignment instead of comparison",
             "body": "This sets status to paid and is always true, so every invoice shows Paid.",
             "evidence_quote": 'if (inv.status = "paid") {', "claims": []},
        ],
    },
    {
        "name": "10-ts-loop-bound",
        "base": {"src/sum.ts": '''export function sum(xs: number[]): number {
  return 0;
}
'''},
        "head": {"src/sum.ts": '''export function sum(xs: number[]): number {
  let total = 0;
  for (let i = 0; i <= xs.length; i++) {
    total += xs[i];
  }
  return total;
}
'''},
        "expected": [{"file": "src/sum.ts", "anchor": "i <= xs.length",
                      "category": "correctness"}],
        "recorded": [
            {"file": "src/sum.ts", "anchor": "i <= xs.length", "severity": "major",
             "category": "correctness", "title": "Loop reads past the end of the array",
             "body": "i <= xs.length adds undefined on the last pass, so total becomes NaN.",
             "evidence_quote": "for (let i = 0; i <= xs.length; i++) {",
             "claims": [{"kind": "symbol_exists", "name": "sum"}]},
        ],
    },
    {
        "name": "11-ts-clean-change-hallucinations",
        "base": {"src/format.ts": '''export function money(cents: number): string {
  return (cents / 100).toFixed(2);
}
'''},
        "head": {"src/format.ts": '''export function money(cents: number): string {
  return (cents / 100).toFixed(2);
}

export function percent(ratio: number): string {
  return `${(ratio * 100).toFixed(1)}%`;
}
'''},
        "expected": [],
        "recorded": [
            {"file": "src/format.ts", "anchor": "export function percent", "severity": "major",
             "category": "correctness", "title": "percent should call clampRatio",
             "body": "Other formatters clamp first.", "evidence_quote": "return clampRatio(ratio);",
             "claims": [{"kind": "symbol_exists", "name": "clampRatio"}]},
            {"file": "src/format.ts", "anchor": "return (cents / 100)", "severity": "minor",
             "category": "correctness", "title": "money rounds incorrectly",
             "body": "toFixed rounds half-even.", "evidence_quote": "return (cents / 100).toFixed(2);",
             "claims": []},
            {"file": "src/missing.ts", "line": 3, "severity": "major", "category": "correctness",
             "title": "Bug in a file the PR never touched", "body": "...",
             "evidence_quote": "x", "claims": []},
        ],
    },
    {
        "name": "12-py-clean-refactor",
        "base": {"util/text.py": '''def slug(s):
    return s.lower().replace(" ", "-")
'''},
        "head": {"util/text.py": '''def slug(text):
    return text.strip().lower().replace(" ", "-")
'''},
        "expected": [],
        "recorded": [],
    },
]


def _line_of(src: str, anchor: str) -> int:
    for i, ln in enumerate(src.splitlines(), start=1):
        if anchor in ln:
            return i
    raise ValueError(f"anchor {anchor!r} not found")


def main() -> None:
    if ROOT.exists():
        shutil.rmtree(ROOT)
    for case in CASES:
        d = ROOT / case["name"]
        for side in ("base", "head"):
            for path, text in case[side].items():
                p = d / side / path
                p.parent.mkdir(parents=True, exist_ok=True)
                p.write_text(text)
        head = case["head"]
        expected = [{"file": e["file"], "line": _line_of(head[e["file"]], e["anchor"]),
                     "category": e["category"]} for e in case["expected"]]
        recorded = []
        for r in case["recorded"]:
            r = dict(r)
            anchor = r.pop("anchor", None)
            if anchor is not None:
                r["line"] = _line_of(head[r["file"]], anchor)
            recorded.append(r)
        (d / "case.json").write_text(json.dumps(
            {"title": case["name"], "expected": expected,
             "recorded_answer": {"findings": recorded}}, indent=2) + "\n")
    print(f"wrote {len(CASES)} cases to {ROOT}")


if __name__ == "__main__":
    main()
