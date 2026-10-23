"""Tree-sitter code index: definitions, the calls inside each one, and callers."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field

import tree_sitter_go
import tree_sitter_javascript
import tree_sitter_python
import tree_sitter_typescript
from tree_sitter import Language, Node, Parser

from reviewhound.paths import LANGUAGES, language_of

_PY = Language(tree_sitter_python.language())
_TS = Language(tree_sitter_typescript.language_typescript())
_TSX = Language(tree_sitter_typescript.language_tsx())
_JS = Language(tree_sitter_javascript.language())
_GO = Language(tree_sitter_go.language())

_FUNC_TYPES = {
    "function_definition", "function_declaration", "method_definition",
    "generator_function_declaration", "method_declaration",
}
_CLASS_TYPES = {"class_definition", "class_declaration", "abstract_class_declaration"}
_TYPE_TYPES = {"interface_declaration", "type_alias_declaration", "enum_declaration",
               "type_spec", "type_alias"}
_CALL_TYPES = {"call", "call_expression", "new_expression"}


@dataclass(frozen=True)
class Definition:
    name: str
    qualified: str
    kind: str  # function | class | type
    file: str
    start_line: int
    end_line: int


@dataclass
class FileIndex:
    path: str
    language: str
    definitions: list[Definition] = field(default_factory=list)
    calls: dict[str, set[str]] = field(default_factory=dict)  # qualified def -> callee names
    identifiers: set[str] = field(default_factory=set)
    parse_errors: bool = False


def _parser_for(path: str) -> Parser | None:
    lang = language_of(path)
    if lang == "python":
        return Parser(_PY)
    if lang == "typescript":
        return Parser(_TSX if path.endswith(".tsx") else _TS)
    if lang == "javascript":
        return Parser(_JS)
    if lang == "go":
        return Parser(_GO)
    return None


def _text(node: Node | None) -> str:
    if node is None or node.text is None:
        return ""
    return node.text.decode("utf-8", errors="replace")


def _callee_name(call: Node) -> str:
    target = call.child_by_field_name("function") or call.child_by_field_name("constructor")
    if target is None:
        return ""
    if target.type in ("attribute", "member_expression", "selector_expression"):
        prop = (target.child_by_field_name("attribute") or target.child_by_field_name("property")
                or target.child_by_field_name("field"))
        return _text(prop)
    if target.type in ("identifier", "type_identifier"):
        return _text(target)
    return ""


def _go_receiver(method: Node) -> str:
    """`Store` for `func (s *Store)`, `func (s Store)` and `func (s *Store[T])`."""
    params = method.child_by_field_name("receiver")
    decl = next((c for c in params.named_children if c.type == "parameter_declaration"), None) \
        if params is not None else None
    t = decl.child_by_field_name("type") if decl is not None else None
    while t is not None and t.type in ("pointer_type", "generic_type"):
        t = t.named_children[0] if t.type == "pointer_type" else t.child_by_field_name("type")
    return _text(t) if t is not None and t.type == "type_identifier" else ""


def _def_name(node: Node) -> tuple[str, str] | None:
    """Return (name, kind) when `node` defines something worth indexing."""
    if node.type in _FUNC_TYPES:
        return _text(node.child_by_field_name("name")), "function"
    if node.type in _CLASS_TYPES:
        return _text(node.child_by_field_name("name")), "class"
    if node.type in _TYPE_TYPES:
        return _text(node.child_by_field_name("name")), "type"
    if node.type == "variable_declarator":
        value = node.child_by_field_name("value")
        if value is not None and value.type in ("arrow_function", "function_expression",
                                                "function"):
            return _text(node.child_by_field_name("name")), "function"
    return None


def index_file(path: str, source: str) -> FileIndex | None:
    parser = _parser_for(path)
    lang = language_of(path)
    if parser is None or lang is None:
        return None
    tree = parser.parse(source.encode("utf-8"))
    fi = FileIndex(path=path, language=lang, parse_errors=tree.root_node.has_error)

    def walk(node: Node, scope: list[str], owner: str | None) -> None:
        found = _def_name(node)
        child_scope, child_owner = scope, owner
        if found and found[0]:
            name, kind = found
            if node.type == "method_declaration":
                scope = [r for r in (_go_receiver(node),) if r]
            qualified = ".".join([*scope, name])
            fi.definitions.append(Definition(name=name, qualified=qualified, kind=kind, file=path,
                                             start_line=node.start_point[0] + 1,
                                             end_line=node.end_point[0] + 1))
            child_scope = [*scope, name]
            if kind == "function":
                child_owner = qualified
                fi.calls.setdefault(qualified, set())
        if node.type in _CALL_TYPES and owner is not None:
            callee = _callee_name(node)
            if callee:
                fi.calls[owner].add(callee)
        if node.type in ("identifier", "property_identifier", "type_identifier",
                         "field_identifier"):
            fi.identifiers.add(_text(node))
        for child in node.children:
            walk(child, child_scope, child_owner)

    walk(tree.root_node, [], None)
    return fi


class CodeIndex:
    """Index of one repo snapshot (normally the PR head)."""

    def __init__(self, languages: Iterable[str] = LANGUAGES) -> None:
        self.languages = frozenset(languages)
        self.files: dict[str, FileIndex] = {}
        self.sources: dict[str, str] = {}
        self._by_name: dict[str, list[Definition]] = defaultdict(list)
        self._callers: dict[str, set[str]] = defaultdict(set)

    @classmethod
    def build(cls, sources: dict[str, str], languages: Iterable[str] = LANGUAGES) -> CodeIndex:
        idx = cls(languages)
        for path, src in sources.items():
            idx.add(path, src)
        return idx

    def add(self, path: str, source: str) -> None:
        self.sources[path] = source
        if not self.covers(path):
            return
        fi = index_file(path, source)
        if fi is None:
            return
        self.files[path] = fi
        for d in fi.definitions:
            self._by_name[d.name].append(d)
            if d.qualified != d.name:
                self._by_name[d.qualified].append(d)
        for owner, callees in fi.calls.items():
            for callee in callees:
                self._callers[callee].add(f"{path}::{owner}")

    def covers(self, path: str) -> bool:
        """True when facts about `path` can be checked: its language is parsed by this index."""
        return language_of(path) in self.languages

    def qualified_matches(self, dotted: str) -> list[Definition]:
        """Definitions whose qualified name is `dotted` or ends with `.dotted`."""
        suffix = "." + dotted
        return [d for d in self.iter_definitions()
                if d.qualified == dotted or d.qualified.endswith(suffix)]

    def definitions(self, name: str) -> list[Definition]:
        return list(self._by_name.get(name, []))

    def has_symbol(self, name: str) -> bool:
        if name in self._by_name:
            return True
        return any(name in fi.identifiers for fi in self.files.values())

    def is_defined(self, name: str) -> bool:
        return name in self._by_name

    def calls_of(self, qualified_or_name: str) -> set[str] | None:
        """Callees of a function, or None when the function is unknown or ambiguous."""
        name = qualified_or_name
        hits = [fi.calls[q] for fi in self.files.values() for q in fi.calls
                if q == name or q.rsplit(".", 1)[-1] == name or q.endswith("." + name)]
        if len(hits) != 1:
            return None
        return set(hits[0])

    def callers_of(self, name: str) -> list[str]:
        return sorted(self._callers.get(name, set()))

    def enclosing_definition(self, path: str, line: int) -> Definition | None:
        fi = self.files.get(path)
        if fi is None:
            return None
        best: Definition | None = None
        for d in fi.definitions:
            inside = d.start_line <= line <= d.end_line and d.kind == "function"
            if inside and (best is None or d.start_line >= best.start_line):
                best = d
        return best

    def iter_definitions(self) -> Iterator[Definition]:
        for fi in self.files.values():
            yield from fi.definitions
