"""Small shared primitives for the F# and Elixir AST extractors."""
from __future__ import annotations

import hashlib
from functools import lru_cache

from tree_sitter import Language, Node, Parser

from .db import SymbolRecord


@lru_cache(maxsize=None)
def parser_for(language: str, signature: bool = False) -> tuple[Language, Parser]:
    # Keep the Language alive for as long as the native parser can use it.
    if language == "fsharp":
        import tree_sitter_fsharp as grammar
        capsule = grammar.language_signature() if signature else grammar.language()
    else:
        import tree_sitter_elixir as grammar
        capsule = grammar.language()
    language_object = Language(capsule)
    return language_object, Parser(language_object)


def text(node: Node | None) -> str:
    return node.text.decode("utf-8", errors="replace") if node is not None else ""


def walk(node: Node):
    stack = [node]
    while stack:
        current = stack.pop()
        yield current
        stack.extend(reversed(current.named_children))


def diagnostics(root: Node) -> list[dict]:
    errors = []
    stack = [root]
    while stack:
        node = stack.pop()
        if node.type == "ERROR" or node.is_missing:
            errors.append({"start_line": node.start_point.row + 1,
                           "end_line": node.end_point.row + 1,
                           "type": "missing" if node.is_missing else "ERROR"})
        if node.has_error:
            stack.extend(reversed(node.named_children))
    return errors


def symbol(source: bytes, file_id: int, node: Node, kind: str, name: str,
           qualified: str, *, signature: str | None = None, doc: str | None = None,
           metadata: dict | None = None, end: Node | None = None) -> SymbolRecord:
    last = end if end is not None else node
    body = source[node.start_byte:last.end_byte]
    details = {"start_byte": node.start_byte, "end_byte": last.end_byte,
               "references": [], **(metadata or {})}
    return SymbolRecord(
        file_id=file_id, kind=kind, name=name, qualified_name=qualified,
        signature=signature, content=body.decode("utf-8", errors="replace"),
        start_line=node.start_point.row + 1, end_line=last.end_point.row + 1,
        start_column=node.start_point.column,
        line_count=last.end_point.row - node.start_point.row + 1,
        body_hash=hashlib.sha256(body).hexdigest()[:16], doc_comment=doc,
        visibility=details.get("visibility"), metadata=details,
    )
