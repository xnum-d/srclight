"""Elixir AST extraction with module/arity identity and syntax-derived references."""
from __future__ import annotations

from tree_sitter import Node

from .db import SymbolRecord
from .syntax import diagnostics, parser_for, symbol, text, walk

_DEFINITIONS = {"def", "defp", "defmacro", "defmacrop", "defdelegate", "defguard", "defguardp"}
_MODULES = {"defmodule", "defprotocol", "defimpl"}
_SPECIAL = _DEFINITIONS | _MODULES | {
    "alias", "import", "require", "use", "quote", "unquote", "unquote_splicing",
    "case", "cond", "if", "unless", "with", "for", "receive", "try", "fn", "super",
    "defstruct", "defexception", "defoverridable", "raise", "throw", "reraise",
}


def _target(node: Node) -> str:
    return text(node.child_by_field_name("target")) if node.type == "call" else ""


def _arguments(node: Node) -> list[Node]:
    args = next((c for c in node.named_children if c.type == "arguments"), None)
    return list(args.named_children) if args is not None else []


def _pairs(nodes: list[Node]) -> dict[str, Node]:
    return {text(pair.child_by_field_name("key")).rstrip(": "):
            pair.child_by_field_name("value")
            for node in nodes if node.type == "keywords" for pair in node.named_children
            if pair.type == "pair" and pair.child_by_field_name("value") is not None}


def _body(node: Node) -> Node | None:
    block = next((c for c in node.named_children if c.type == "do_block"), None)
    return block if block is not None else _pairs(_arguments(node)).get("do")


def _operator(node: Node) -> str:
    op = node.child_by_field_name("operator")
    return text(op)


def _attribute(node: Node) -> tuple[str, Node | None]:
    if node.type != "unary_operator" or _operator(node) != "@":
        return "", None
    operand = node.child_by_field_name("operand")
    if operand is None:
        return "", None
    args = _arguments(operand)
    return _target(operand) or text(operand), args[0] if args else None


def _doc(value: Node | None) -> str | None:
    if value is None or text(value) in ("false", "nil"):
        return None
    raw = text(value)
    if value.type == "string":
        if raw.startswith('"""'):
            return raw[3:-3].strip()
        if raw.startswith('"'):
            return raw[1:-1]
    if value.type == "sigil":
        content = next((n for n in walk(value) if n.type == "quoted_content"), None)
        return text(content).strip() if content is not None else raw
    return raw


def _expand(name: str, aliases: dict[str, str], module: str = "") -> str:
    if name == "__MODULE__":
        return module
    if name.startswith("__MODULE__."):
        return module + name[len("__MODULE__"):]
    if name.startswith("Elixir."):
        return name[7:]
    first, dot, rest = name.partition(".")
    return aliases.get(first, first) + (dot + rest if dot else "")


def _module_name(name: str, aliases: dict[str, str], module: str) -> str:
    if name.startswith(("Elixir.", "__MODULE__")) or name.split(".")[0] in aliases:
        return _expand(name, aliases, module)
    return module + "." + name if module else name


def _import(node: Node, aliases: dict[str, str], module: str) -> list[dict]:
    keyword = _target(node)
    args = _arguments(node)
    if keyword not in ("alias", "import", "require", "use") or not args:
        return []
    target = args[0]
    options = _pairs(args[1:])
    targets = []
    if target.type == "dot" and target.child_by_field_name("right").type == "tuple":
        prefix = _expand(text(target.child_by_field_name("left")), aliases, module)
        targets = [prefix + "." + text(c)
                   for c in target.child_by_field_name("right").named_children]
    elif target.type in ("alias", "identifier"):
        targets = [_expand(text(target), aliases, module)]
    result = []
    for target_name in targets:
        names = []
        if keyword == "alias":
            short = text(options["as"]) if "as" in options else target_name.rsplit(".", 1)[-1]
            aliases[short] = target_name
            names = [short]
        if keyword == "import" and "only" in options:
            names = [text(n.child_by_field_name("key")).rstrip(": ")
                     for n in walk(options["only"]) if n.type == "pair"]
        result.append({"statement": text(node), "module": target_name,
                       "names": names, "kind": keyword})
    return result


def extract_imports(content: str) -> list[dict]:
    tree = parser_for("elixir")[1].parse(content.encode("utf-8"))
    imports = []

    def visit(node: Node, module: str, aliases: dict[str, str]) -> None:
        if node.type in ("string", "charlist", "sigil", "comment"):
            return
        if _target(node) in _MODULES:
            args = _arguments(node)
            if args:
                name = text(args[0])
                module = _module_name(name, aliases, module)
            aliases = dict(aliases)
        imports.extend(_import(node, aliases, module))
        for child in node.named_children:
            visit(child, module, aliases)

    visit(tree.root_node, "", {})
    return imports


def _head(node: Node) -> tuple[Node | None, Node | None]:
    args = _arguments(node)
    head = args[0] if args else None
    guard = None
    if head is not None and head.type == "binary_operator" and _operator(head) == "when":
        guard = head.child_by_field_name("right")
        head = head.child_by_field_name("left")
    return head, guard


def _parameters(head: Node) -> tuple[int, int, set[str]]:
    args = _arguments(head)
    defaults = sum(a.type == "binary_operator" and _operator(a) == "\\\\" for a in args)
    bound = {text(n) for arg in args for n in walk(arg) if n.type == "identifier"}
    return len(args), defaults, bound


def _references(nodes: list[Node], bound: set[str], module: str,
                aliases: dict[str, str]) -> list[dict]:
    references = {}

    def add(target: Node | None, arity: int, hidden: set[str]) -> None:
        if target is None:
            return
        qualifier = None
        if target.type == "dot":
            left = target.child_by_field_name("left")
            right = target.child_by_field_name("right")
            if left is None or right is None:
                return
            spelling = text(left)
            if left.type != "alias" and not spelling.startswith("__MODULE__"):
                return  # runtime receiver: syntax cannot resolve its module
            qualifier = _expand(spelling, aliases, module)
            name = text(right)
        elif target.type == "identifier":
            name = text(target)
            if name in hidden or name in _SPECIAL or name in ("true", "false", "nil", "__MODULE__"):
                return
        else:
            return
        reference = {"name": name, "arity": arity}
        if qualifier:
            reference["qualified_name"] = f"{qualifier}.{name}/{arity}"
        references[(name, qualifier, arity)] = reference

    stack = [(node, 0, set(bound)) for node in reversed(nodes)]
    while stack:
        node, piped, hidden = stack.pop()
        if node.type in ("string", "charlist", "sigil", "comment", "quoted_content"):
            continue
        keyword = _target(node)
        if keyword in _DEFINITIONS | _MODULES or keyword == "quote":
            continue
        if node.type == "stab_clause":
            left, right = node.child_by_field_name("left"), node.child_by_field_name("right")
            local = hidden | ({text(n) for n in walk(left) if n.type == "identifier"}
                              if left is not None else set())
            if right is not None:
                stack.append((right, 0, local))
            continue
        if node.type == "unary_operator":
            op = _operator(node)
            if op == "@":
                continue
            operand = node.child_by_field_name("operand")
            if (op == "&" and operand is not None and operand.type == "binary_operator"
                    and _operator(operand) == "/"):
                left = operand.child_by_field_name("left")
                right = operand.child_by_field_name("right")
                if left is not None and right is not None and right.type == "integer":
                    target = left.child_by_field_name("target") if left.type == "call" else left
                    add(target, int(text(right)), hidden)
                    continue
        if node.type == "binary_operator":
            op = _operator(node)
            left, right = node.child_by_field_name("left"), node.child_by_field_name("right")
            if op == "|>" and left is not None and right is not None:
                stack.append((right, 1, hidden))
                stack.append((left, 0, hidden))
                continue
            if op == "=" and left is not None and right is not None:
                hidden.update(text(n) for n in walk(left) if n.type == "identifier")
                stack.append((right, 0, hidden))
                continue
        if node.type == "call":
            add(node.child_by_field_name("target"), len(_arguments(node)) + piped, hidden)
            stack.extend((a, 0, hidden) for a in reversed(_arguments(node)))
            block = _body(node)
            if block is not None and block.type == "do_block":
                stack.append((block, 0, set(hidden)))
            continue
        if node.type == "identifier":
            add(node, piped, hidden)  # a bare, unbound identifier is a zero-arity call
            continue
        stack.extend((c, 0, hidden) for c in reversed(node.named_children))
    return list(references.values())


def extract_symbols(source: bytes, file_id: int, rel_path: str
                    ) -> tuple[list[SymbolRecord], list[dict]]:
    tree = parser_for("elixir")[1].parse(source)
    symbols: list[SymbolRecord] = []

    def visit_block(block: Node, module: str, aliases: dict[str, str]) -> None:
        pending_doc = None
        pending_spec = None
        for node in block.named_children:
            attribute, value = _attribute(node)
            if attribute:
                if attribute == "doc":
                    pending_doc = _doc(value)
                elif attribute == "spec":
                    pending_spec = text(node)
                continue
            keyword = _target(node)
            args = _arguments(node)
            if keyword in _MODULES and args:
                raw_name = text(args[0])
                expanded = _expand(raw_name, aliases, module)
                name = _module_name(raw_name, aliases, module)
                details = {"language": "elixir", "module": module, "definition": keyword}
                names = [(name, details)]
                if keyword == "defimpl":
                    target = _pairs(args[1:]).get("for")
                    targets = (list(target.named_children) if target is not None
                               and target.type == "list" else [target])
                    names = []
                    for target in targets:
                        implementor = _expand(text(target), aliases, module) if target else "Any"
                        names.append((expanded + "." + implementor,
                                      {**details, "protocol": expanded, "for_type": implementor}))
                body = _body(node)
                doc = None
                if body is not None:
                    doc = next((_doc(v) for c in body.named_children
                                for k, v in [_attribute(c)] if k == "moduledoc"), None)
                for qualified_module, module_details in names:
                    symbols.append(symbol(
                        source, file_id, node,
                        "interface" if keyword == "defprotocol" else "module",
                        qualified_module.rsplit(".", 1)[-1], qualified_module, doc=doc,
                        signature=source[node.start_byte:body.start_byte].decode().strip()
                        if body is not None else text(node), metadata=module_details))
                    if body is not None:
                        visit_block(body, qualified_module, dict(aliases))
                if keyword != "defimpl" and not raw_name.startswith(("Elixir.", "__MODULE__")):
                    segments = len(raw_name.split("."))
                    aliases[raw_name.split(".")[0]] = (
                        name.rsplit(".", segments - 1)[0] if segments > 1 else name)
                continue
            if keyword in ("alias", "import", "require", "use"):
                _import(node, aliases, module)
                continue
            if keyword in _DEFINITIONS:
                head, guard = _head(node)
                if head is None:
                    continue
                name = _target(head) if head.type == "call" else text(head)
                arity, defaults, bound = _parameters(head)
                body = _body(node)
                references = _references([n for n in (guard, body) if n is not None],
                                         bound, module, aliases)
                if keyword == "defdelegate":
                    options = _pairs(args[1:])
                    if "to" in options:
                        target_name = (text(options["as"]).lstrip(":")
                                       if "as" in options else name)
                        target_module = _expand(text(options["to"]), aliases, module)
                        references.append({
                            "name": target_name, "arity": arity,
                            "qualified_name": f"{target_module}.{target_name}/{arity}",
                        })
                qualified = f"{module}.{name}/{arity}" if module else f"{name}/{arity}"
                signature = source[node.start_byte:head.end_byte].decode("utf-8", errors="replace")
                if guard is not None:
                    signature = source[node.start_byte:guard.end_byte].decode(
                        "utf-8", errors="replace")
                if pending_spec:
                    signature = pending_spec + "\n" + signature
                details = {"language": "elixir", "module": module, "arity": arity,
                           "arities": list(range(arity - defaults, arity + 1)),
                           "definition": keyword, "visibility": "private" if keyword in (
                               "defp", "defmacrop", "defguardp") else "public",
                           "references": references}
                kind = "macro" if keyword in (
                    "defmacro", "defmacrop", "defguard", "defguardp") else "function"
                symbols.append(symbol(source, file_id, node, kind, name, qualified,
                                      signature=signature, doc=pending_doc, metadata=details))
                pending_doc = pending_spec = None
                continue
            if keyword in ("defstruct", "defexception"):
                name = "__struct__" if keyword == "defstruct" else "__exception__"
                symbols.append(symbol(source, file_id, node, "struct", name,
                                      f"{module}.{name}", signature=text(node),
                                      metadata={"language": "elixir", "module": module,
                                                "definition": keyword}))
                continue
            # Definitions under conditionals (e.g. if Code.ensure_loaded?) remain visible.
            if node.type not in ("string", "charlist", "sigil", "comment"):
                for child in node.named_children:
                    if child.type in ("do_block", "else_block", "source"):
                        visit_block(child, module, dict(aliases))

    visit_block(tree.root_node, "", {})
    symbols.sort(key=lambda s: (s.metadata["start_byte"], -s.metadata["end_byte"]))
    return symbols, diagnostics(tree.root_node)
