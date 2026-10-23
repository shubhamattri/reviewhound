import pytest

from reviewhound.index.conventions import learn_conventions
from reviewhound.index.treesitter import CodeIndex

PY = '''import logging
logger = logging.getLogger(__name__)


def require_admin(req):
    if not req.admin:
        raise PermissionError("no")


class Store:
    def save(self, item):
        require_admin(item.req)
        self.write(item)


def handler(req):
    require_admin(req)
    logger.info("ok")
    return Store().save(req)
'''
TS = '''import { db } from "./db";

export interface Order { id: string }

export async function placeOrder(o: Order): Promise<void> {
  await db.save(o);
  audit(o.id);
}

export const audit = (id: string) => {
  console.log(id);
};

class Repo {
  find(id: string) {
    return new Order(id);
  }
}
'''


def test_python_definitions_calls_and_callers() -> None:
    idx = CodeIndex.build({"app/svc.py": PY})
    names = {(d.qualified, d.kind) for d in idx.iter_definitions()}
    assert names == {("require_admin", "function"), ("Store", "class"),
                     ("Store.save", "function"), ("handler", "function")}
    assert idx.calls_of("Store.save") == {"require_admin", "write"}
    assert idx.calls_of("handler") == {"require_admin", "info", "Store", "save"}
    assert idx.callers_of("require_admin") == ["app/svc.py::Store.save", "app/svc.py::handler"]
    encl = idx.enclosing_definition("app/svc.py", 13)
    assert encl is not None and encl.qualified == "Store.save"


def test_typescript_definitions_include_arrow_functions_and_types() -> None:
    idx = CodeIndex.build({"src/orders.ts": TS})
    names = {(d.qualified, d.kind) for d in idx.iter_definitions()}
    assert names == {("Order", "type"), ("placeOrder", "function"), ("audit", "function"),
                     ("Repo", "class"), ("Repo.find", "function")}
    assert idx.calls_of("placeOrder") == {"save", "audit"}
    assert idx.calls_of("Repo.find") == {"Order"}
    assert idx.has_symbol("db") is True
    assert idx.has_symbol("validateOrder") is False


def test_ambiguous_function_name_is_unknown_not_guessed() -> None:
    idx = CodeIndex.build({"a.py": "def run():\n    x()\n", "b.py": "def run():\n    y()\n"})
    assert idx.calls_of("run") is None
    assert idx.calls_of("nope") is None


def test_unsupported_files_keep_text_but_no_definitions() -> None:
    idx = CodeIndex.build({"README.md": "# hi\n"})
    assert idx.sources == {"README.md": "# hi\n"}
    assert list(idx.iter_definitions()) == []


JS = '''import { db } from "./db.js";

export async function placeOrder(order) {
  await db.save(order);
  audit(order.id);
}

export const audit = (id) => {
  console.log(id);
};

const legacy = function (x) {
  return helper(x);
};

class Repo {
  find(id) {
    return new Order(id);
  }
}
'''
GO = '''package store

import "fmt"

type Store struct {
\tdb *DB
}

type Saver interface {
\tSave(id string) error
}

func (s *Store) Save(id string) error {
\tif err := s.db.Exec(id); err != nil {
\t\treturn fmt.Errorf("save: %w", err)
\t}
\treturn validate(id)
}

func (s Store) Count() int {
\treturn len(s.db.rows)
}

func validate(id string) error {
\treturn nil
}

func NewStore(db *DB) *Store {
\treturn &Store{db: db}
}
'''


@pytest.mark.parametrize("path", ["src/orders.js", "src/orders.jsx", "src/orders.mjs",
                                  "src/orders.cjs"])
def test_javascript_definitions_calls_and_callers(path: str) -> None:
    idx = CodeIndex.build({path: JS})
    names = {(d.qualified, d.kind) for d in idx.iter_definitions()}
    assert names == {("placeOrder", "function"), ("audit", "function"),
                     ("legacy", "function"), ("Repo", "class"), ("Repo.find", "function")}
    assert idx.calls_of("placeOrder") == {"save", "audit"}
    assert idx.calls_of("legacy") == {"helper"}
    assert idx.calls_of("Repo.find") == {"Order"}
    assert idx.callers_of("audit") == [f"{path}::placeOrder"]
    encl = idx.enclosing_definition(path, 18)
    assert encl is not None and encl.qualified == "Repo.find"
    assert idx.files[path].language == "javascript"
    assert idx.files[path].parse_errors is False


def test_go_methods_are_qualified_by_receiver_and_selector_calls_are_indexed() -> None:
    idx = CodeIndex.build({"store/store.go": GO})
    names = {(d.qualified, d.kind) for d in idx.iter_definitions()}
    assert names == {("Store", "type"), ("Saver", "type"), ("Store.Save", "function"),
                     ("Store.Count", "function"), ("validate", "function"),
                     ("NewStore", "function")}
    assert idx.calls_of("Store.Save") == {"Exec", "Errorf", "validate"}
    assert idx.calls_of("Store.Count") == {"len"}
    assert idx.callers_of("validate") == ["store/store.go::Store.Save"]
    assert idx.is_defined("Store.Save") is True
    encl = idx.enclosing_definition("store/store.go", 15)
    assert encl is not None and encl.qualified == "Store.Save"
    assert idx.has_symbol("Exec") is True
    assert idx.files["store/store.go"].language == "go"
    assert idx.files["store/store.go"].parse_errors is False


def test_conventions_find_used_auth_helper_logging_and_hot_folders() -> None:
    from collections import Counter
    idx = CodeIndex.build({"app/svc.py": PY})
    conv = learn_conventions(idx, Counter({"app/svc.py": 5, "docs/a.md": 1}), ["ensure_staff"])
    assert conv.auth_helpers == ["ensure_staff", "require_admin"]
    assert conv.log_styles == ["logger.info"]
    assert conv.error_types == ["PermissionError"]
    assert conv.hot_folders == ["app", "docs"]
