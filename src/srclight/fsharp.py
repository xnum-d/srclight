"""F# syntax indexing. References are syntactic, not FSharp.Compiler.Service results."""
from __future__ import annotations

from pathlib import PurePath

from tree_sitter import Node

from .db import SymbolRecord
from .syntax import diagnostics, parser_for, symbol, text, walk

_HEADS = {"function_declaration_left", "value_declaration_left"}
_NAMES = {"identifier", "op_identifier", "active_pattern"}
_SKIP = {"comment", "block_comment", "line_comment", "xml_doc", "string", "const",
         "simple_type", "type_arguments", "type_argument", "attribute", "attributes"}


def _name(node: Node | None) -> str:
    value = text(node)
    if node is not None and node.type == "op_identifier":
        return value.strip("() ")
    return value


def _pattern_names(pattern: Node) -> list[Node]:
    if pattern.type == "identifier_pattern":
        # A curried, return-annotated function is also read as this pattern.
        # Its first path is the binding; subsequent children are parameters/types.
        return [c for c in pattern.named_children
                if c.type in ("long_identifier_or_op", "long_identifier")][:1]
    return [name for child in pattern.named_children if child.type.endswith("pattern")
            for name in _pattern_names(child)]


def _head_names(head: Node) -> list[Node]:
    if head.type == "function_declaration_left":
        return [child for child in head.named_children if child.type in _NAMES][:1]
    pattern = next((c for c in head.named_children if c.type.endswith("pattern")), None)
    return _pattern_names(pattern) if pattern is not None else []


def _function_head(head: Node) -> bool:
    if head.type == "function_declaration_left":
        return True
    pattern = next((c for c in head.named_children if c.type == "identifier_pattern"), None)
    return pattern is not None and any(c.type.endswith("pattern")
                                      for c in pattern.named_children)


def _doc(source: bytes, node: Node) -> str | None:
    lines = source[:node.start_byte].decode("utf-8", errors="replace").splitlines()
    # The prefix of the declaration's own line contains only indentation.
    if lines and not lines[-1].strip():
        lines.pop()
    found = []
    for line in reversed(lines):
        stripped = line.strip()
        if stripped.startswith("///"):
            found.append(stripped[3:].lstrip())
        elif stripped.startswith("[<") and stripped.endswith(">]") and not found:
            continue
        else:
            break
    return "\n".join(reversed(found)) or None


def _bound(head: Node) -> set[str]:
    arguments = [c for c in head.named_children
                 if c.type in ("argument_patterns", "paren_pattern", "typed_pattern")]
    if head.type == "value_declaration_left":
        pattern = next((c for c in head.named_children if c.type == "identifier_pattern"), None)
        if pattern is not None:
            arguments.extend(c for c in pattern.named_children if c.type.endswith("pattern"))
    bound = set()
    stack = list(arguments)
    while stack:
        node = stack.pop()
        if node.type in ("simple_type", "function_type", "tuple_type", "type_arguments"):
            continue
        if node.type == "identifier" and text(node) != "_":
            bound.add(text(node))
        stack.extend(node.named_children)
    return bound


def _pattern_bound(pattern: Node) -> set[str]:
    names = set()
    for node in walk(pattern):
        if node.type == "identifier_pattern":
            for path in node.named_children:
                if path.type not in ("long_identifier", "long_identifier_or_op"):
                    continue
                value = text(path)
                if "." not in value and (value[:1].islower() or value.startswith("``")):
                    names.add(value)
    return names


def _references(body: Node | None, bound: set[str], aliases: dict[str, str]) -> list[dict]:
    if body is None:
        return []
    refs: dict[tuple[str, str | None], dict] = {}

    def add(node: Node, hidden: set[str]) -> None:
        if node.type not in ("long_identifier_or_op", "long_identifier", "identifier",
                             "op_identifier", "infix_op"):
            return
        spelling = _name(node)
        if not spelling or spelling.split(".")[0] in hidden:
            return
        parts = spelling.split(".")
        name = parts[-1]
        qualified = None
        if len(parts) > 1:
            parts[0] = aliases.get(parts[0], parts[0])
            qualified = ".".join(parts)
        reference = {"name": name}
        if qualified:
            reference["qualified_name"] = qualified
        refs[(name, qualified)] = reference

    stack = [(body, bound)]
    while stack:
        current, hidden = stack.pop()
        if current.type in _SKIP:
            continue
        if current.type in ("function_or_value_defn", "member_defn", "type_definition"):
            # Local definitions are scanned separately under their lexical name.
            continue
        children = current.named_children
        if current.type == "application_expression" and children:
            add(children[0], hidden)
        elif current.type == "infix_expression":
            operator = next((c for c in children if c.type == "infix_op"), None)
            if operator is not None:
                op = text(operator)
                if op in ("|>", "||>", "|||>") and children:
                    add(children[-1], hidden)
                elif op in ("<|", "<||", "<|||") and children:
                    add(children[0], hidden)
                else:
                    add(operator, hidden)
        elif current.type == "fun_expression":
            params = next((c for c in children if c.type == "argument_patterns"), None)
            if params is not None:
                hidden = hidden | {text(n) for n in walk(params) if n.type == "identifier"}
                children = [c for c in children if c.id != params.id]
        elif current.type == "rule":
            pattern = current.child_by_field_name("pattern")
            if pattern is not None:
                hidden = hidden | _pattern_bound(pattern)
                children = [c for c in children if c.id != pattern.id]
        elif current.type == "for_expression" and children:
            pattern = children[0]
            local = hidden | _pattern_bound(pattern)
            for index, child in reversed(list(enumerate(children[1:]))):
                stack.append((child, local if index == len(children) - 2 else hidden))
            continue
        stack.extend((child, hidden) for child in reversed(children))
    return list(refs.values())


def extract_imports(content: str) -> list[dict]:
    source = content.encode("utf-8")
    tree = parser_for("fsharp")[1].parse(source)
    result = []
    for node in walk(tree.root_node):
        if node.type == "import_decl":
            target = next((c for c in node.named_children if c.type == "long_identifier"), None)
            if target is not None:
                result.append({"statement": text(node), "module": text(target), "names": []})
        elif node.type == "module_abbrev":
            name = next((c for c in node.named_children if c.type == "identifier"), None)
            target = node.child_by_field_name("block")
            if name is not None and target is not None:
                result.append({"statement": text(node), "module": text(target),
                               "names": [text(name)]})
        elif node.type in ("compiler_directive_decl", "compiler_directive"):
            if text(node).lstrip().startswith("#load"):
                for item in walk(node):
                    if item.type == "string":
                        result.append({"statement": text(node), "module": text(item).strip('"'),
                                       "names": []})
    return result


def extract_symbols(source: bytes, file_id: int, rel_path: str
                    ) -> tuple[list[SymbolRecord], list[dict]]:
    signature_file = PurePath(rel_path).suffix.lower() == ".fsi"
    tree = parser_for("fsharp", signature_file)[1].parse(source)
    symbols: list[SymbolRecord] = []

    def emit(node: Node, kind: str, name: str, scope: str, *, body: Node | None = None,
             head: Node | None = None, end: Node | None = None,
             aliases: dict[str, str] | None = None) -> None:
        if not name:
            return
        qualified = f"{scope}.{name}" if scope else name
        if signature_file:
            signature = text(node)
        elif body is not None:
            signature = source[node.start_byte:body.start_byte].decode(
                "utf-8", errors="replace").rstrip().removesuffix("=").rstrip()
        elif head is not None:
            signature = source[node.start_byte:head.end_byte].decode("utf-8", errors="replace")
        else:
            signature = text(node).splitlines()[0]
        metadata = {"language": "fsharp", "module": scope,
                    "references": _references(body, _bound(head) if head else set(), aliases or {})}
        visibility = next((text(n) for n in (head.named_children if head else [])
                           if n.type == "access_modifier"), None)
        if visibility:
            metadata["visibility"] = visibility
        symbols.append(symbol(source, file_id, node, kind, name, qualified,
                              signature=signature, doc=_doc(source, node),
                              metadata=metadata, end=end))

    def visit(node: Node, scope: str, aliases: dict[str, str]) -> None:
        if node.type in _SKIP:
            return
        if node.type in ("namespace", "named_module", "module_defn"):
            name_node = node.child_by_field_name("name")
            if name_node is None:
                name_node = next((c for c in node.named_children if c.type == "identifier"), None)
            name = text(name_node)
            if name:
                emit(node, "namespace" if node.type == "namespace" else "module", name, scope)
                scope = f"{scope}.{name}" if scope else name
            local_aliases = dict(aliases)
            for child in node.named_children:
                if child.id != (name_node.id if name_node is not None else -1):
                    visit(child, scope, local_aliases)
            return
        if node.type == "module_abbrev":
            name_node = next((c for c in node.named_children if c.type == "identifier"), None)
            target = node.child_by_field_name("block")
            if name_node is not None and target is not None:
                aliases[text(name_node)] = text(target)
            return
        if node.type == "type_definition":
            declaration = next((c for c in node.named_children
                                if c.type.endswith("_defn") or c.type == "type_extension"), None)
            if declaration is not None:
                type_name = next((c for c in declaration.named_children
                                  if c.type == "type_name"), None)
                name_node = type_name.child_by_field_name("type_name") if type_name else None
                name = text(name_node)
                kinds = {"record_type_defn": "struct", "union_type_defn": "union",
                         "enum_type_defn": "enum", "interface_type_defn": "interface",
                         "type_abbrev_defn": "type_alias", "delegate_type_defn": "type_alias"}
                kind = kinds.get(declaration.type, "class")
                if kind == "union":
                    cases = [n for n in walk(declaration) if n.type == "union_type_case"]
                    if len(cases) == 1:
                        case_name = next((c for c in cases[0].named_children
                                          if c.type == "identifier"), None)
                        if case_name is not None and text(case_name)[:1].islower():
                            kind = "type_alias"
                emit(node, kind, name, scope)
                if kind == "type_alias":
                    return
                type_scope = f"{scope}.{name}" if scope else name
                for child in declaration.named_children:
                    if child.type != "type_name":
                        visit(child, type_scope, dict(aliases))
                return
        if node.type in ("union_type_case", "enum_type_case"):
            name = next((c for c in node.named_children if c.type == "identifier"), None)
            emit(node, "enum_member", text(name), scope)
            return
        if node.type == "record_field":
            name = next((c for c in node.named_children if c.type == "identifier"), None)
            emit(node, "property", text(name), scope)
            return
        if node.type in ("function_or_value_defn", "value_definition"):
            children = node.named_children
            heads = [(i, c) for i, c in enumerate(children) if c.type in _HEADS]
            for index, (position, head) in enumerate(heads):
                stop = heads[index + 1][0] if index + 1 < len(heads) else len(children)
                body = next((children[i] for i in range(position + 1, stop)
                             if node.field_name_for_named_child(i) == "body"), None)
                names = _head_names(head)
                kind = "function" if (_function_head(head)
                                     or any(c.type == "curried_spec" for c in children)
                                     or body is not None and body.type in (
                                         "fun_expression", "function_expression")) else "variable"
                start = node if len(heads) == 1 else head
                for name_node in names:
                    name = _name(name_node)
                    emit(start, kind, name, scope, body=body, head=head,
                         end=body if len(heads) > 1 else None, aliases=aliases)
                if body is not None:
                    local_scope = (f"{scope}.{_name(names[0])}" if scope else _name(names[0])) \
                        if len(names) == 1 else scope
                    visit(body, local_scope, dict(aliases))
            return
        if node.type == "member_defn":
            head = next((c for c in node.named_children if c.type in (
                "method_or_prop_defn", "member_signature")), None)
            if head is not None:
                name_node = head.child_by_field_name("name")
                if name_node is not None and name_node.type == "property_or_ident":
                    method = name_node.child_by_field_name("method")
                    name_node = method if method is not None else next(
                        (c for c in name_node.named_children if c.type == "identifier"), None)
                if name_node is None:
                    name_node = next((c for c in head.named_children
                                      if c.type == "identifier"), None)
                equals = next((c for c in head.children if c.type == "="), None)
                body = next((c for c in head.named_children
                             if equals is not None and c.start_byte >= equals.end_byte), None)
                if body is None:
                    body = next((c for c in reversed(node.named_children)
                                 if c.id != head.id and c.type not in (
                                     "attributes", "access_modifier")), None)
                emit(node, "method", text(name_node), scope, body=body, head=head, aliases=aliases)
                if body is not None:
                    visit(body, f"{scope}.{text(name_node)}", dict(aliases))
                return
        if node.type == "member_signature":
            name = next((c for c in node.named_children if c.type == "identifier"), None)
            emit(node, "method", text(name), scope)
            return
        for child in node.named_children:
            visit(child, scope, aliases)

    visit(tree.root_node, "", {})
    symbols.sort(key=lambda s: (s.metadata["start_byte"], -s.metadata["end_byte"]))
    return symbols, diagnostics(tree.root_node)
