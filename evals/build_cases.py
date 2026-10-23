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


def go(src: str) -> str:
    """Go sources are written with 4-space indents here and stored with gofmt tabs."""
    return src.replace("    ", "\t")


JS_USERS_BASE = '''import { db } from "./db.js";

export class UserRepo {
  async find(id) {
    return db.get("users", id);
  }
}
'''
JS_REPORT_BASE = '''export function header(rows) {
  return rows[0];
}
'''
JS_COMMENTS_BASE = '''export function escapeHtml(s) {
  return s.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
}
'''
JS_ROUTES_BASE = '''import { requireAdmin } from "./auth.js";
import { getUser, updateUser, deleteUser } from "./users.js";

export function register(app) {
  app.get("/users/:id", requireAdmin, getUser);
  app.put("/users/:id", requireAdmin, updateUser);
}
'''
JS_CART_BASE = '''export class Order {
  constructor(qty, price) {
    this.qty = qty;
    this.price = price;
  }
}

export function tax(amount) {
  return amount * 0.2;
}
'''
JS_FORMAT_BASE = '''export class Formatter {
  constructor(width) {
    this.width = width;
  }

  line(text) {
    return text.slice(0, this.width);
  }
}

export function pad(text, n) {
  return text.padEnd(n, " ");
}
'''
GO_CONFIG_BASE = go('''package config

import "encoding/json"

type Config struct {
    Port int
}

func Default() Config {
    return Config{Port: 8080}
}

func Validate(port int) bool {
    return port > 0 && port < 65536
}
''')
GO_COUNTER_BASE = go('''package counter

import "sync"

type Counter struct {
    mu sync.Mutex
    n  map[string]int
}

func (c *Counter) Get(k string) int {
    c.mu.Lock()
    defer c.mu.Unlock()
    return c.n[k]
}
''')
GO_STATS_BASE = go('''package stats

func Count(xs []int) int {
    return len(xs)
}
''')
GO_USERS_BASE = go('''package store

import "database/sql"

func ByID(db *sql.DB, id int) (*sql.Rows, error) {
    return db.Query("SELECT * FROM users WHERE id = $1", id)
}
''')
GO_SERVER_BASE = go('''package api

import "net/http"

type Server struct {
    users UserStore
}

func (s *Server) requireAdmin(r *http.Request) error {
    if r.Header.Get("X-Role") != "admin" {
        return errForbidden
    }
    return nil
}

func (s *Server) GetUser(w http.ResponseWriter, r *http.Request) {
    if err := s.requireAdmin(r); err != nil {
        http.Error(w, "forbidden", http.StatusForbidden)
        return
    }
    writeJSON(w, s.users.Get(r.URL.Query().Get("id")))
}
''')
GO_FETCH_BASE = go('''package fetch

import (
    "io"
    "net/http"
)

func Status(url string) (int, error) {
    resp, err := http.Head(url)
    if err != nil {
        return 0, err
    }
    defer resp.Body.Close()
    return resp.StatusCode, nil
}
''')
GO_TRIM_BASE = go('''package strutil

import "strings"

func Upper(s string) string {
    return strings.ToUpper(s)
}
''')
GO_CACHE_BASE = go('''package cache

import "sync"

type Cache struct {
    mu   sync.Mutex
    data map[string]string
}

func (c *Cache) Set(k, v string) {
    c.mu.Lock()
    defer c.mu.Unlock()
    c.data[k] = v
}
''')
PY_CART_BASE = '''def validate_qty(qty):
    return qty > 0


class Cart:
    def __init__(self):
        self.items = []

    def add(self, sku, qty):
        self.items.append((sku, qty))
'''
TS_INVOICE_BASE = '''export function tax(amount: number): number {
  return amount * 0.2;
}

export class Invoice {
  constructor(private lines: number[]) {}

  subtotal(): number {
    return this.lines.reduce((a, b) => a + b, 0);
  }
}
'''
PY_ORDERS_BASE = '''from app.auth import require_role


def cancel_order(request, order_id):
    require_role(request, "support")
    orders.cancel(order_id)
    return {"ok": True}
'''

NEW_CASES: list[dict[str, Any]] = [
    {
        "name": "13-js-unawaited-insert",
        "base": {"src/users.js": JS_USERS_BASE},
        "head": {"src/users.js": JS_USERS_BASE + '''
export async function saveUser(user) {
  try {
    db.insert("users", user);
  } catch (err) {
    return false;
  }
  return true;
}
'''},
        "expected": [{"file": "src/users.js", "anchor": 'db.insert("users", user);',
                      "category": "async"}],
        "recorded": [
            {"file": "src/users.js", "anchor": 'db.insert("users", user);', "severity": "major",
             "category": "async", "title": "insert is not awaited",
             "body": "A rejected insert escapes the try/catch, so saveUser still returns true.",
             "evidence_quote": 'db.insert("users", user);',
             "claims": [{"kind": "calls", "caller": "saveUser", "callee": "insert"},
                        {"kind": "symbol_exists", "name": "saveUser"}]},
            {"file": "src/users.js", "anchor": "return false;", "severity": "major",
             "category": "correctness", "title": "Use UserRepo.insert for writes",
             "body": "The repository already has an insert method that retries.",
             "evidence_quote": "return false;",
             "claims": [{"kind": "symbol_exists", "name": "UserRepo.insert"}]},
        ],
    },
    {
        "name": "14-js-loop-skips-first-row",
        "base": {"src/report.js": JS_REPORT_BASE},
        "head": {"src/report.js": JS_REPORT_BASE + '''
export function totalAmount(rows) {
  let total = 0;
  for (let i = 1; i < rows.length; i++) {
    total += rows[i].amount;
  }
  return total;
}
'''},
        "expected": [{"file": "src/report.js", "anchor": "for (let i = 1; i < rows.length",
                      "category": "correctness"}],
        "recorded": [
            {"file": "src/report.js", "anchor": "for (let i = 1; i < rows.length",
             "severity": "major", "category": "correctness", "title": "First row is skipped",
             "body": "The loop starts at 1, so rows[0] is never added to the total.",
             "evidence_quote": "for (let i = 1; i < rows.length; i++) {",
             "claims": [{"kind": "symbol_exists", "name": "totalAmount"}]},
            {"file": "src/report.js", "anchor": "total += rows[i].amount;", "severity": "major",
             "category": "correctness", "title": "Missing amounts become NaN",
             "body": "The fallback to 0 is applied after the addition.",
             "evidence_quote": "total += rows[i].amount || 0;", "claims": []},
        ],
    },
    {
        "name": "15-js-unescaped-html",
        "base": {"src/comments.js": JS_COMMENTS_BASE},
        "head": {"src/comments.js": JS_COMMENTS_BASE + '''
export function renderComment(el, comment) {
  el.innerHTML = "<p>" + comment.text + "</p>";
}
'''},
        "expected": [{"file": "src/comments.js", "anchor": "el.innerHTML =",
                      "category": "security"}],
        "recorded": [
            {"file": "src/comments.js", "anchor": "el.innerHTML =", "severity": "blocker",
             "category": "security", "title": "Comment text is written as raw HTML",
             "body": "A comment containing a script tag runs in every reader's browser. "
                     "escapeHtml exists in this file but is not used here.",
             "evidence_quote": 'el.innerHTML = "<p>" + comment.text + "</p>";',
             "claims": [{"kind": "not_calls", "caller": "renderComment", "callee": "escapeHtml"},
                        {"kind": "symbol_exists", "name": "escapeHtml"}]},
            {"file": "src/comments.js", "anchor": "el.innerHTML =", "severity": "minor",
             "category": "correctness", "title": "Comment text is escaped twice",
             "body": "renderComment escapes text that escapeHtml already escaped.",
             "evidence_quote": 'el.innerHTML = "<p>" + comment.text + "</p>";',
             "claims": [{"kind": "calls", "caller": "renderComment", "callee": "escapeHtml"}]},
            {"file": "src/comments.js", "anchor": "return s.replace", "severity": "major",
             "category": "security", "title": "escapeHtml does not escape quotes",
             "body": "Old helper, not changed by this PR.",
             "evidence_quote": 'return s.replace(/&/g, "&amp;")', "claims": []},
        ],
    },
    {
        "name": "16-js-route-without-admin-check",
        "base": {"src/routes.js": JS_ROUTES_BASE},
        "head": {"src/routes.js": JS_ROUTES_BASE.replace(
            '  app.put("/users/:id", requireAdmin, updateUser);\n',
            '  app.put("/users/:id", requireAdmin, updateUser);\n'
            '  app.delete("/users/:id", deleteUser);\n')},
        "expected": [{"file": "src/routes.js", "anchor": "app.delete(", "category": "auth"}],
        "recorded": [
            {"file": "src/routes.js", "anchor": "app.delete(", "severity": "blocker",
             "category": "auth", "title": "Delete route has no admin check",
             "body": "The other user routes go through requireAdmin; this one lets anyone "
                     "delete a user.",
             "evidence_quote": 'app.delete("/users/:id", deleteUser);',
             "claims": [{"kind": "quote", "file": "src/routes.js",
                         "text": 'app.put("/users/:id", requireAdmin, updateUser);'},
                        {"kind": "calls", "caller": "register", "callee": "delete"}]},
            {"file": "src/routes.js", "anchor": "app.put(", "severity": "major",
             "category": "auth", "title": "Update route has no admin check",
             "body": "updateUser is reachable without requireAdmin.",
             "evidence_quote": 'app.put("/users/:id", updateUser);', "claims": []},
        ],
    },
    {
        "name": "17-js-map-callback-returns-nothing",
        "base": {"src/cart.js": JS_CART_BASE},
        "head": {"src/cart.js": JS_CART_BASE + '''
export function lineTotals(orders) {
  return orders.map((o) => {
    o.qty * o.price;
  });
}
'''},
        "expected": [{"file": "src/cart.js", "anchor": "o.qty * o.price;",
                      "category": "correctness"}],
        "recorded": [
            {"file": "src/cart.js", "anchor": "o.qty * o.price;", "severity": "major",
             "category": "correctness", "title": "map callback returns undefined",
             "body": "The arrow function has a block body and no return, so every total is "
                     "undefined.",
             "evidence_quote": "o.qty * o.price;",
             "claims": [{"kind": "symbol_exists", "name": "lineTotals"}]},
            {"file": "src/cart.js", "anchor": "return orders.map", "severity": "major",
             "category": "correctness", "title": "Totals skip Order.tax",
             "body": "Each order should go through Order.tax before it is summed.",
             "evidence_quote": "return orders.map((o) => {",
             "claims": [{"kind": "symbol_exists", "name": "Order.tax"}]},
        ],
    },
    {
        "name": "18-js-two-secrets-same-shape",
        "base": {"src/config.js": 'export const region = "eu-west-1";\n'},
        "head": {"src/config.js": '''export const region = "eu-west-1";

export const staging = {
  apiToken: "tk_live_Q8n3Lr5Vw2Xy7Za1",
};

export const production = {
  apiToken: "tk_live_M4p9Ks6Tb1Hc3Jd8",
};
'''},
        "expected": [{"file": "src/config.js", "anchor": "tk_live_Q8n3", "category": "secret"},
                     {"file": "src/config.js", "anchor": "tk_live_M4p9", "category": "secret"}],
        "recorded": [
            {"file": "src/config.js", "anchor": "export const region", "severity": "minor",
             "category": "config", "title": "Region is hard-coded",
             "body": "Read the region from the environment.",
             "evidence_quote": 'export const region = "eu-west-1";', "claims": []},
        ],
    },
    {
        "name": "19-go-ignored-unmarshal-error",
        "base": {"config/load.go": GO_CONFIG_BASE},
        "head": {"config/load.go": GO_CONFIG_BASE + go('''
func Load(data []byte) Config {
    var cfg Config
    json.Unmarshal(data, &cfg)
    return cfg
}
''')},
        "expected": [{"file": "config/load.go", "anchor": "json.Unmarshal(data, &cfg)",
                      "category": "error-handling"}],
        "recorded": [
            {"file": "config/load.go", "anchor": "json.Unmarshal(data, &cfg)",
             "severity": "major", "category": "error-handling",
             "title": "Unmarshal error is ignored",
             "body": "Bad config bytes silently give a zero Config with Port 0.",
             "evidence_quote": "json.Unmarshal(data, &cfg)",
             "claims": [{"kind": "calls", "caller": "Load", "callee": "Unmarshal"},
                        {"kind": "symbol_exists", "name": "Config"}]},
            {"file": "config/load.go", "anchor": "return cfg", "severity": "major",
             "category": "correctness", "title": "Load skips Config.Validate",
             "body": "Every loaded config should go through its Validate method.",
             "evidence_quote": "return cfg",
             "claims": [{"kind": "symbol_exists", "name": "Config.Validate"}]},
        ],
    },
    {
        "name": "20-go-lock-held-on-early-return",
        "base": {"counter/counter.go": GO_COUNTER_BASE},
        "head": {"counter/counter.go": GO_COUNTER_BASE + go('''
func (c *Counter) Add(k string, d int) {
    c.mu.Lock()
    if d == 0 {
        return
    }
    c.n[k] += d
    c.mu.Unlock()
}
''')},
        "expected": [{"file": "counter/counter.go", "anchor": "if d == 0 {",
                      "category": "concurrency"}],
        "recorded": [
            {"file": "counter/counter.go", "anchor": "if d == 0 {", "severity": "blocker",
             "category": "concurrency", "title": "Early return keeps the mutex locked",
             "body": "Add returns before Unlock when d is 0, so the next Get or Add blocks "
                     "forever. Get uses defer for this reason.",
             "evidence_quote": "if d == 0 {\n        return\n    }",
             "claims": [{"kind": "calls", "caller": "Counter.Add", "callee": "Lock"},
                        {"kind": "quote", "file": "counter/counter.go",
                         "text": "defer c.mu.Unlock()"}]},
            {"file": "counter/counter.go", "anchor": "return c.n[k]", "severity": "major",
             "category": "concurrency", "title": "Get reads without a read lock",
             "body": "Get should take RLock.", "evidence_quote": "return c.n[k]",
             "claims": [{"kind": "calls", "caller": "Counter.Get", "callee": "RLock"}]},
        ],
    },
    {
        "name": "21-go-index-out-of-range",
        "base": {"stats/sum.go": GO_STATS_BASE},
        "head": {"stats/sum.go": GO_STATS_BASE + go('''
func Sum(xs []int) int {
    total := 0
    for i := 0; i <= len(xs); i++ {
        total += xs[i]
    }
    return total
}
''')},
        "expected": [{"file": "stats/sum.go", "anchor": "i <= len(xs)",
                      "category": "correctness"}],
        "recorded": [
            {"file": "stats/sum.go", "anchor": "i <= len(xs)", "severity": "blocker",
             "category": "correctness", "title": "Loop reads one past the end",
             "body": "i reaches len(xs), so xs[i] panics with index out of range.",
             "evidence_quote": "for i := 0; i <= len(xs); i++ {",
             "claims": [{"kind": "symbol_exists", "name": "Sum"}]},
            {"file": "stats/sum.go", "anchor": "return total", "severity": "minor",
             "category": "style", "title": "Use a range loop",
             "body": "The rest of the package ranges over slices.",
             "evidence_quote": "for _, x := range xs {",
             "claims": [{"kind": "calls", "caller": "Sum", "callee": "Count"}]},
        ],
    },
    {
        "name": "22-go-sql-string-join",
        "base": {"store/users.go": GO_USERS_BASE},
        "head": {"store/users.go": GO_USERS_BASE + go('''
func ByName(db *sql.DB, name string) (*sql.Rows, error) {
    return db.Query("SELECT * FROM users WHERE name = '" + name + "'")
}
''')},
        "expected": [{"file": "store/users.go", "anchor": "WHERE name = '",
                      "category": "sql-injection"}],
        "recorded": [
            {"file": "store/users.go", "anchor": "WHERE id = $1", "severity": "major",
             "category": "correctness", "title": "ByID passes the wrong placeholder",
             "body": "Old code, not changed by this PR.",
             "evidence_quote": 'return db.Query("SELECT * FROM users WHERE id = $1", id)',
             "claims": []},
        ],
    },
    {
        "name": "23-go-handler-without-admin-check",
        "base": {"api/server.go": GO_SERVER_BASE},
        "head": {"api/server.go": GO_SERVER_BASE + go('''
func (s *Server) DeleteUser(w http.ResponseWriter, r *http.Request) {
    s.users.Delete(r.URL.Query().Get("id"))
    w.WriteHeader(http.StatusNoContent)
}
''')},
        "expected": [{"file": "api/server.go", "anchor": "s.users.Delete(", "category": "auth"}],
        "recorded": [
            {"file": "api/server.go", "anchor": "s.users.Delete(", "severity": "blocker",
             "category": "auth", "title": "DeleteUser skips requireAdmin",
             "body": "GetUser checks the role first; DeleteUser lets any caller delete users.",
             "evidence_quote": 's.users.Delete(r.URL.Query().Get("id"))',
             "claims": [{"kind": "not_calls", "caller": "Server.DeleteUser",
                         "callee": "requireAdmin"},
                        {"kind": "calls", "caller": "Server.GetUser", "callee": "requireAdmin"}]},
            {"file": "api/server.go", "anchor": "w.WriteHeader(http.StatusNoContent)",
             "severity": "major", "category": "audit", "title": "Deletes are not audited",
             "body": "Server.RemoveUser writes an audit record; this path does not.",
             "evidence_quote": "w.WriteHeader(http.StatusNoContent)",
             "claims": [{"kind": "calls", "caller": "Server.RemoveUser", "callee": "audit"}]},
        ],
    },
    {
        "name": "24-go-response-body-not-closed",
        "base": {"fetch/fetch.go": GO_FETCH_BASE},
        "head": {"fetch/fetch.go": GO_FETCH_BASE + go('''
func Body(url string) ([]byte, error) {
    resp, err := http.Get(url)
    if err != nil {
        return nil, err
    }
    return io.ReadAll(resp.Body)
}
''')},
        "expected": [{"file": "fetch/fetch.go", "anchor": "resp, err := http.Get(url)",
                      "category": "resource-leak"}],
        "recorded": [
            {"file": "fetch/fetch.go", "anchor": "resp, err := http.Get(url)",
             "severity": "major", "category": "resource-leak",
             "title": "Response body is never closed",
             "body": "Each call leaks a connection; Status closes its body with defer.",
             "evidence_quote": "resp, err := http.Get(url)",
             "claims": [{"kind": "not_calls", "caller": "Body", "callee": "Close"},
                        {"kind": "calls", "caller": "Status", "callee": "Close"}]},
            {"file": "fetch/fetch.go", "anchor": "return io.ReadAll", "severity": "major",
             "category": "correctness", "title": "Body never reads the response",
             "body": "The body is returned without being read.",
             "evidence_quote": "return io.ReadAll(resp.Body)",
             "claims": [{"kind": "not_calls", "caller": "Body", "callee": "ReadAll"}]},
        ],
    },
    {
        "name": "25-js-clean-rename-hallucinations",
        "base": {"src/format.js": JS_FORMAT_BASE},
        "head": {"src/format.js": JS_FORMAT_BASE.replace(
            "  line(text) {\n    return text.slice(0, this.width);",
            "  line(value) {\n    return value.slice(0, this.width);")},
        "expected": [],
        "recorded": [
            {"file": "src/format.js", "anchor": "return value.slice", "severity": "major",
             "category": "correctness", "title": "line should pad through Formatter.pad",
             "body": "Short lines are not padded to the width.",
             "evidence_quote": "return value.slice(0, this.width);",
             "claims": [{"kind": "symbol_exists", "name": "Formatter.pad"}]},
            {"file": "src/format.js", "anchor": "line(value)", "severity": "major",
             "category": "correctness", "title": "substring drops the last character",
             "body": "Use slice.", "evidence_quote": "return value.substring(0, this.width);",
             "claims": []},
        ],
    },
    {
        "name": "26-go-clean-helper-hallucinations",
        "base": {"strutil/trim.go": GO_TRIM_BASE},
        "head": {"strutil/trim.go": GO_TRIM_BASE + go('''
func Trim(s string) string {
    return strings.TrimSpace(s)
}
''')},
        "expected": [],
        "recorded": [
            {"file": "strutil/trim.go", "anchor": "func Trim", "severity": "major",
             "category": "correctness", "title": "Trim should normalise case like Upper",
             "body": "Trim calls Upper, which changes the text.",
             "evidence_quote": "return strings.TrimSpace(s)",
             "claims": [{"kind": "calls", "caller": "Trim", "callee": "Upper"}]},
            {"file": "strutil/split.go", "line": 4, "severity": "major",
             "category": "correctness", "title": "Split ignores empty fields",
             "body": "A file this PR does not touch.", "evidence_quote": "strings.Split(s, sep)",
             "claims": []},
            {"file": "strutil/trim.go", "anchor": "return strings.TrimSpace", "severity": "minor",
             "category": "correctness", "title": "TrimSpace misses non-breaking spaces",
             "body": "strings.TrimSpace only trims ASCII spaces.",
             "evidence_quote": "return strings.TrimSpace(s)",
             "claims": [{"kind": "symbol_exists", "name": "strings.TrimSpace"}]},
        ],
    },
    {
        "name": "27-py-clean-dotted-symbol-trap",
        "base": {"shop/cart.py": PY_CART_BASE},
        "head": {"shop/cart.py": PY_CART_BASE + '''
    def count(self):
        return sum(q for _, q in self.items)
'''},
        "expected": [],
        "recorded": [
            {"file": "shop/cart.py", "anchor": "return sum(q for", "severity": "major",
             "category": "correctness", "title": "count trusts quantities Cart never validated",
             "body": "Cart.validate_qty should run before items are counted.",
             "evidence_quote": "return sum(q for _, q in self.items)",
             "claims": [{"kind": "symbol_exists", "name": "Cart.validate_qty"}]},
            {"file": "shop/cart.py", "anchor": "def count(self):", "severity": "minor",
             "category": "performance", "title": "count loops by hand",
             "body": "Use the built-in sum.", "evidence_quote": "def count(self):",
             "claims": [{"kind": "not_calls", "caller": "Cart.count", "callee": "sum"}]},
        ],
    },
    {
        "name": "28-ts-clean-dotted-member-trap",
        "base": {"src/invoice.ts": TS_INVOICE_BASE},
        "head": {"src/invoice.ts": TS_INVOICE_BASE.replace(
            "    return this.lines.reduce((a, b) => a + b, 0);\n  }\n",
            "    return this.lines.reduce((a, b) => a + b, 0);\n  }\n\n"
            "  count(): number {\n    return this.lines.length;\n  }\n")},
        "expected": [],
        "recorded": [
            {"file": "src/invoice.ts", "anchor": "return this.lines.length;",
             "severity": "major", "category": "correctness",
             "title": "count should go through Invoice.tax",
             "body": "Every Invoice amount is taxed by its tax method.",
             "evidence_quote": "return this.lines.length;",
             "claims": [{"kind": "symbol_exists", "name": "Invoice.tax"}]},
        ],
    },
    {
        "name": "29-py-removed-role-check",
        "base": {"app/orders.py": PY_ORDERS_BASE, "app/auth.py": AUTH_LIB},
        "head": {"app/orders.py": PY_ORDERS_BASE.replace(
            '    require_role(request, "support")\n', ""), "app/auth.py": AUTH_LIB},
        "expected": [{"file": "app/orders.py", "anchor": 'require_role(request, "support")',
                      "category": "auth", "side": "LEFT"}],
        "recorded": [
            {"file": "app/orders.py", "anchor": 'require_role(request, "support")',
             "side": "LEFT", "severity": "blocker", "category": "auth",
             "title": "Role check removed from cancel_order",
             "body": "Any signed-in user can now cancel any order.",
             "evidence_quote": 'require_role(request, "support")',
             "claims": [{"kind": "not_calls", "caller": "cancel_order",
                         "callee": "require_role"},
                        {"kind": "symbol_exists", "name": "require_role"}]},
            {"file": "app/orders.py", "anchor": "orders.cancel(order_id)", "severity": "major",
             "category": "auth", "title": "Admin check removed",
             "body": "The admin role check is gone.",
             "evidence_quote": 'require_role(request, "admin")', "claims": []},
        ],
    },
    {
        "name": "30-py-quote-spans-blank-line",
        "base": {"notify/send.py": "def send(client, msg):\n    return client.post(msg)\n"},
        "head": {"notify/send.py": '''def send(client, msg):
    try:
        return client.post(msg)

    except Exception:
        return None
'''},
        "expected": [{"file": "notify/send.py", "anchor": "except Exception:",
                      "category": "error-handling"}],
        "recorded": [
            {"file": "notify/send.py", "anchor": "except Exception:", "severity": "major",
             "category": "error-handling", "title": "Send failures are swallowed",
             "body": "Callers get None and cannot tell a failed send from an empty reply.",
             "evidence_quote": "return client.post(msg)\n\n    except Exception:\n"
                               "        return None",
             "claims": [{"kind": "symbol_exists", "name": "send"}]},
            {"file": "notify/send.py", "anchor": "return None", "severity": "minor",
             "category": "error-handling", "title": "Exception is not logged",
             "body": "Log the error type.", "evidence_quote": "except Exception as exc:",
             "claims": []},
        ],
    },
    {
        "name": "31-rb-finding-in-unindexed-language",
        "base": {".reviewhound.yml": "policy:\n  drop_unverified: false\n",
                 "lib/billing.rb": "def charge(gateway, amount)\n  gateway.charge(amount)\nend\n"},
        "head": {".reviewhound.yml": "policy:\n  drop_unverified: false\n",
                 "lib/billing.rb": '''def charge(gateway, amount)
  gateway.charge(amount)
rescue StandardError
  nil
end
'''},
        "expected": [{"file": "lib/billing.rb", "anchor": "rescue StandardError",
                      "category": "error-handling"}],
        "recorded": [
            {"file": "lib/billing.rb", "anchor": "rescue StandardError", "severity": "major",
             "category": "error-handling", "title": "Failed charges return nil silently",
             "body": "charge hides gateway errors, so the caller treats a failure as done.",
             "evidence_quote": "rescue StandardError\n  nil",
             "claims": [{"kind": "symbol_exists", "name": "charge"},
                        {"kind": "calls", "caller": "charge", "callee": "charge"}]},
            {"file": "lib/billing.rb", "anchor": "  nil", "severity": "major",
             "category": "error-handling", "title": "Error is bound but unused",
             "body": "Remove the unused variable.", "evidence_quote": "rescue => e",
             "claims": []},
        ],
    },
    {
        "name": "32-go-removed-deferred-unlock",
        "base": {"cache/cache.go": GO_CACHE_BASE},
        "head": {"cache/cache.go": GO_CACHE_BASE.replace("\tdefer c.mu.Unlock()\n", "")},
        "expected": [{"file": "cache/cache.go", "anchor": "defer c.mu.Unlock()",
                      "category": "concurrency", "side": "LEFT"}],
        "recorded": [
            {"file": "cache/cache.go", "anchor": "defer c.mu.Unlock()", "side": "LEFT",
             "severity": "blocker", "category": "concurrency",
             "title": "Set no longer releases the mutex",
             "body": "With the deferred Unlock gone, the second Set deadlocks.",
             "evidence_quote": "defer c.mu.Unlock()",
             "claims": [{"kind": "not_calls", "caller": "Cache.Set", "callee": "Unlock"},
                        {"kind": "calls", "caller": "Cache.Set", "callee": "Lock"}]},
            {"file": "cache/cache.go", "anchor": "c.data[k] = v", "severity": "major",
             "category": "concurrency", "title": "Get reads without the lock",
             "body": "Cache.Get should take the mutex.", "evidence_quote": "c.data[k] = v",
             "claims": [{"kind": "calls", "caller": "Cache.Get", "callee": "Lock"}]},
        ],
    },
]

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
    *NEW_CASES,
]


def _line_of(src: str, anchor: str) -> int:
    for i, ln in enumerate(src.splitlines(), start=1):
        if anchor in ln:
            return i
    raise ValueError(f"anchor {anchor!r} not found")


def _tree(case: dict[str, Any], item: dict[str, Any]) -> dict[str, str]:
    """LEFT anchors are lines the PR deleted, so they resolve against the base version."""
    tree: dict[str, str] = case["base"] if item.get("side") == "LEFT" else case["head"]
    return tree


def main(root: Path = ROOT) -> None:
    if root.exists():
        shutil.rmtree(root)
    for case in CASES:
        d = root / case["name"]
        for side in ("base", "head"):
            for path, text in case[side].items():
                p = d / side / path
                p.parent.mkdir(parents=True, exist_ok=True)
                p.write_text(text)
        expected = []
        for e in case["expected"]:
            exp = {"file": e["file"], "line": _line_of(_tree(case, e)[e["file"]], e["anchor"]),
                   "category": e["category"]}
            if e.get("side") == "LEFT":
                exp["side"] = "LEFT"
            expected.append(exp)
        recorded = []
        for r in case["recorded"]:
            r = dict(r)
            anchor = r.pop("anchor", None)
            if anchor is not None:
                r["line"] = _line_of(_tree(case, r)[r["file"]], anchor)
            r.pop("side", None)  # the model does not say which side; the checker decides
            recorded.append(r)
        (d / "case.json").write_text(json.dumps(
            {"title": case["name"], "expected": expected,
             "recorded_answer": {"findings": recorded}}, indent=2) + "\n")
    print(f"wrote {len(CASES)} cases to {root}")


if __name__ == "__main__":
    main()
