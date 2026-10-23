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


def test_conventions_find_used_auth_helper_logging_and_hot_folders() -> None:
    from collections import Counter
    idx = CodeIndex.build({"app/svc.py": PY})
    conv = learn_conventions(idx, Counter({"app/svc.py": 5, "docs/a.md": 1}), ["ensure_staff"])
    assert conv.auth_helpers == ["ensure_staff", "require_admin"]
    assert conv.log_styles == ["logger.info"]
    assert conv.error_types == ["PermissionError"]
    assert conv.hot_folders == ["app", "docs"]
