"""Consumer-visible F#/Elixir indexing, identities, graph and coverage behavior."""
import json

import pytest

from srclight.db import Database
from srclight.imports import extract_imports
from srclight.indexer import IndexConfig, Indexer


@pytest.fixture
def project(tmp_path):
    root = tmp_path / "source"
    root.mkdir()
    db = Database(tmp_path / "index.db")
    db.open()
    db.initialize()
    indexer = Indexer(db, IndexConfig(root=root))

    def index(files):
        for name, content in files.items():
            path = root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")
        stats = indexer.index()
        assert stats.errors == 0
        return db

    yield root, index, db, indexer
    db.close()


def named(db, qualified):
    found = [s for path in db.all_file_paths() for s in db.symbols_in_file(path)
             if s.qualified_name == qualified]
    assert len(found) == 1, qualified
    return found[0]


def callees(db, qualified):
    return {entry["symbol"].qualified_name
            for entry in db.get_callees(named(db, qualified).id)}


def test_fsharp_modifiers_docs_types_and_parent_scope(project):
    _, index, _, _ = project
    db = index({"Domain.fs": '''namespace Example
module Domain =
    /// Parse a status.
    let private parseStatus (value: string) = value
    let inline twice x = x + x
    let private initial: int = 3
    type Status = Active | Inactive of string
    type Product = { Id: string; Status: Status }
    type Worker() =
        member _.Run(value) = parseStatus value
'''})
    parse = named(db, "Example.Domain.parseStatus")
    assert parse.visibility == "private"
    assert parse.doc_comment == "Parse a status."
    assert parse.signature == "let private parseStatus (value: string)"
    assert db.get_symbol_by_id(parse.parent_symbol_id).qualified_name == "Example.Domain"
    assert named(db, "Example.Domain.Product.Id").kind == "property"
    assert named(db, "Example.Domain.Status.Active").kind == "enum_member"
    assert named(db, "Example.Domain.Worker.Run").kind == "method"
    assert "Example.Domain.parseStatus" in callees(db, "Example.Domain.Worker.Run")
    names = {s.name for s in db.symbols_in_file("Domain.fs")}
    assert not names & {"private", "inline", "int"}


def test_fsharp_recursion_group_preserves_each_body(project):
    _, index, _, _ = project
    db = index({"Parity.fs": '''module Parity
let rec even x = if x = 0 then true else odd (x - 1)
and odd x = if x = 0 then false else even (x - 1)
'''})
    even = named(db, "Parity.even")
    odd = named(db, "Parity.odd")
    assert "false" not in even.content
    assert "true" not in odd.content
    assert callees(db, "Parity.even") == {"Parity.odd"}
    assert callees(db, "Parity.odd") == {"Parity.even"}


def test_fsharp_signatures_use_signature_parser(project):
    _, index, _, _ = project
    db = index({"Api.fsi": '''namespace Example
module Api =
    /// Convert a value.
    val convert: value:int -> string
    type Worker =
        abstract Run: int -> string
'''})
    convert = named(db, "Example.Api.convert")
    assert convert.kind == "function"
    assert convert.signature == "val convert: value:int -> string"
    assert convert.doc_comment == "Convert a value."
    assert named(db, "Example.Api.Worker.Run").signature == "abstract Run: int -> string"
    assert db.get_parse_errors() == {}


def test_fsharp_aliases_pipes_and_lexical_shadowing(project):
    _, index, _, _ = project
    db = index({"Domain.fs": '''namespace Example
module Domain =
    let convert x = x
''', "Use.fs": '''namespace Example
module Use =
    module D = Example.Domain
    let convert x = x + 1
    let run x = x |> D.convert |> convert
    let parameter convert x = convert x
    let lambda x = List.map (fun convert -> convert x) [convert x]
    let prose x =
        (* outer (* convert x *) comment *)
        let message = "convert x"
        x
'''})
    assert callees(db, "Example.Use.run") == {"Example.Domain.convert", "Example.Use.convert"}
    assert callees(db, "Example.Use.parameter") == set()
    assert callees(db, "Example.Use.lambda") == {"Example.Use.convert"}
    assert callees(db, "Example.Use.prose") == set()


def test_fsharp_script_imports_ignore_comment_and_string_decoys():
    imports = extract_imports('''open Example.Domain
module D = Example.Domain
let decoy = "open Wrong.Module"
(* open Also.Wrong *)
''', "fsharp")
    assert [(i["module"], i["names"]) for i in imports] == [
        ("Example.Domain", []), ("Example.Domain", ["D"])]


def test_fsharp_local_function_does_not_collapse_into_global(project):
    _, index, _, _ = project
    db = index({"Local.fs": '''module Local
let helper x = x
let run x =
    let helper y = y + 1
    helper x
'''})
    assert named(db, "Local.run.helper").kind == "function"
    assert callees(db, "Local.run") == {"Local.run.helper"}


def test_fsharp_operator_name_not_parenthesized(project):
    _, index, _, _ = project
    db = index({"Operators.fs": '''module Operators
let (<+>) x y = x + y
let run x = x <+> 2
'''})
    assert named(db, "Operators.<+>").name == "<+>"
    assert callees(db, "Operators.run") == {"Operators.<+>"}


def test_fsharp_nullable_types_preserve_source_and_null_patterns(project):
    _, index, _, _ = project
    source = '''module Nullable
// Nullable reference types are types, not alternative match patterns.
type Result = { Error: string | null }
let identity (value: string | null) : string | null = value
let classify (value: string | null) =
    match value with
    | null -> "empty"
    | text -> text
let literal = "string | null"
let after x = identity x
'''
    db = index({"Nullable.fs": source})
    assert db.get_parse_errors() == {}
    identity = named(db, "Nullable.identity")
    assert identity.signature == "let identity (value: string | null) : string | null"
    assert identity.content == source.splitlines()[3]
    assert identity.start_line == 4
    assert named(db, "Nullable.Result.Error").content == "Error: string | null"
    assert callees(db, "Nullable.after") == {"Nullable.identity"}
    assert '| null -> "empty"' in named(db, "Nullable.classify").content
    assert '"string | null"' in named(db, "Nullable.literal").content


@pytest.mark.parametrize("column", [255, 256, 271, 512])
def test_fsharp_scanner_preserves_long_inline_branch_columns(project, column):
    _, index, _, _ = project
    prefix = "                let fl = if true then 1 else "
    source = "module LongColumn\n" + '''let run x =
    task {
        match x with
        | Some a ->
            match a with
            | Some b ->
''' + prefix.ljust(column) + '''None
                let msrs =
                    match b with
                    | Some m -> []
                    | _ -> []
                return { X = msrs }
            | _ -> return { X = [] }
        | _ -> return { X = [] }
    }
let after x = x
'''
    db = index({"LongColumn.fs": source})
    assert db.get_parse_errors() == {}
    assert named(db, "LongColumn.run.msrs").kind == "variable"
    assert named(db, "LongColumn.after").kind == "function"


def test_fsharp_nullable_signature_types(project):
    _, index, _, _ = project
    db = index({"Nullable.fsi": "module Nullable\n"
                "val identity: value:(string | null) -> (string | null)\n"})
    assert db.get_parse_errors() == {}
    assert named(db, "Nullable.identity").signature == (
        "val identity: value:(string | null) -> (string | null)")


def test_syntax_gaps_survive_incremental_index_and_clear_after_repair(project):
    root, index, db, indexer = project
    index({"Partial.fs": '''module Partial
type Value = { Text: }
let usable x = x
'''})
    assert named(db, "Partial.usable").kind == "function"
    errors = db.get_parse_errors()
    assert errors["Partial.fs"][0]["start_line"] == 2
    indexer.index()
    assert db.get_parse_errors() == errors
    index({"Partial.fs": "module Partial\ntype Value = { Text: string option }\n"
                        "let usable x = x\n"})
    assert db.get_parse_errors() == {}
    (root / "Partial.fs").unlink()
    indexer.index()
    assert db.all_file_paths() == set()


def test_elixir_arity_clauses_defaults_docs_and_specs(project):
    _, index, _, _ = project
    db = index({"catalog.ex": r'''defmodule Example.Catalog do
  @moduledoc "Catalog operations."
  @doc "Count products."
  @spec count(map()) :: integer()
  def count(filters \\ %{}), do: map_size(filters)
  def find(:all), do: count()
  def find(id) when is_binary(id), do: id
  def zero, do: count()
end
'''})
    module = named(db, "Example.Catalog")
    assert module.doc_comment == "Catalog operations."
    count = named(db, "Example.Catalog.count/1")
    assert count.metadata["arities"] == [0, 1]
    assert count.doc_comment == "Count products."
    assert count.signature.startswith("@spec count(map()) :: integer()\ndef count(")
    clauses = [s for s in db.symbols_in_file("catalog.ex")
               if s.qualified_name == "Example.Catalog.find/1"]
    assert {s.signature for s in clauses} == {"def find(:all)", "def find(id) when is_binary(id)"}
    assert callees(db, "Example.Catalog.zero/0") == {"Example.Catalog.count/1"}


def test_elixir_overloads_and_punctuation_are_distinct(project):
    _, index, _, _ = project
    db = index({"catalog.ex": '''defmodule Catalog do
  def count, do: 0
  def count(filters), do: filters
  def ready?, do: true
  def reset!, do: count()
  def run(filters), do: {count(filters), ready?(), reset!()}
end
'''})
    assert callees(db, "Catalog.reset!/0") == {"Catalog.count/0"}
    assert callees(db, "Catalog.run/1") == {
        "Catalog.count/1", "Catalog.ready?/0", "Catalog.reset!/0"}


def test_elixir_aliases_grouping_pipelines_and_captures(project):
    _, index, _, _ = project
    db = index({"service.ex": '''defmodule Root.Service do
  def convert(x), do: x
  def convert(x, opts), do: {x, opts}
end
''', "use.ex": '''defmodule Example.Use do
  alias Root.{Service, Other}
  alias Root.Service, as: S
  def convert(x), do: x
  def run(x), do: x |> S.convert() |> Service.convert([])
  def capture, do: &S.convert/1
  def local(x), do: __MODULE__.convert(x)
end
'''})
    assert callees(db, "Example.Use.run/1") == {"Root.Service.convert/1", "Root.Service.convert/2"}
    assert callees(db, "Example.Use.capture/0") == {"Root.Service.convert/1"}
    assert callees(db, "Example.Use.local/1") == {"Example.Use.convert/1"}
    imports = extract_imports((project[0] / "use.ex").read_text(), "elixir")
    assert [(i["module"], i["names"]) for i in imports] == [
        ("Root.Service", ["Service"]), ("Root.Other", ["Other"]), ("Root.Service", ["S"])]


def test_elixir_dynamic_receivers_and_prose_make_no_static_calls(project):
    _, index, _, _ = project
    db = index({"quiet.ex": '''defmodule Quiet do
  def hidden(x), do: x
  @doc "hidden(1)"
  def prose(x), do: "hidden(1)"
  def dynamic(receiver, x), do: receiver.hidden(x)
  def quoted, do: quote(do: hidden(1))
  def param(hidden), do: hidden
  def lambda(xs), do: Enum.map(xs, fn hidden -> hidden end)
end
'''})
    for name in ("prose/1", "dynamic/2", "quoted/0", "param/1", "lambda/1"):
        assert callees(db, "Quiet." + name) == set()


def test_elixir_nested_modules_protocols_and_delegation(project):
    _, index, _, _ = project
    db = index({"nested.ex": '''defmodule Root do
  defmodule Inner do
    def run(x), do: x
  end
  defdelegate run(x), to: Root.Inner
end
defprotocol Convert do
  @doc "Convert a value."
  def convert(value)
end
defimpl Convert, for: Thing do
  def convert(value), do: Root.run(value)
end
'''})
    assert named(db, "Root.Inner.run/1").metadata["module"] == "Root.Inner"
    assert named(db, "Convert").kind == "interface"
    assert named(db, "Convert.Thing.convert/1").metadata["module"] == "Convert.Thing"
    assert callees(db, "Root.run/1") == {"Root.Inner.run/1"}


def test_native_languages_do_not_link_same_named_python_functions(project):
    _, index, _, _ = project
    db = index({"Main.fs": "module Main\nlet convert x = x\nlet run x = convert x\n",
                "other.py": "def convert(x):\n    return x\n"})
    assert callees(db, "Main.run") == {"Main.convert"}


def test_fsharp_return_annotated_curried_binding_has_one_lexical_scope(project):
    _, index, _, _ = project
    db = index({"Render.fs": '''module Render
type ProductId = string
let render (onExit: unit -> unit) : int * int =
    let local x = x + 1
    (local 1, local 2)
'''})
    assert named(db, "Render.ProductId").kind == "type_alias"
    assert named(db, "Render.render").kind == "function"
    assert named(db, "Render.render.local").kind == "function"
    assert {s.qualified_name for s in db.symbols_in_file("Render.fs")} == {
        "Render", "Render.ProductId", "Render.render", "Render.render.local"}
    assert callees(db, "Render.render") == {"Render.render.local"}


def test_elixir_same_line_arities_and_clauses_are_not_replaced(project):
    _, index, _, _ = project
    db = index({"same.ex": "defmodule Same do; def read(), do: 0; "
                "def read(:a), do: :a; def read(x), do: x; end\n"})
    assert named(db, "Same.read/0").signature == "def read()"
    clauses = [s for s in db.symbols_in_file("same.ex") if s.qualified_name == "Same.read/1"]
    assert {s.signature for s in clauses} == {"def read(:a)", "def read(x)"}


def test_elixir_nested_dotted_modules_and_protocol_implementation_lists(project):
    _, index, _, _ = project
    db = index({"nested.ex": '''defmodule Outer do
  defmodule Other.Deep do
    def run(x), do: x
  end
  def call(x), do: Other.Deep.run(x)
end
defprotocol Convert do
  def convert(value)
end
defimpl Convert, for: [One, Two] do
  def convert(value), do: value
end
'''})
    assert callees(db, "Outer.call/1") == {"Outer.Other.Deep.run/1"}
    for implementor in ("One", "Two"):
        scope = "Convert." + implementor
        function = named(db, scope + ".convert/1")
        assert db.get_symbol_by_id(function.parent_symbol_id).qualified_name == scope


@pytest.mark.parametrize("files,path,expected_module,target", [
    ({"Domain.fs": "namespace Example\nmodule Domain =\n    let value = 1\n",
      "Use.fs": "module Use\nmodule D = Example.Domain\nlet run () = D.value\n"},
     "Use.fs", "Example.Domain", "Domain.fs"),
    ({"service.ex": "defmodule Root.Service do; def run(x), do: x; end\n",
      "use.ex": "defmodule Use do; alias Root.Service, as: S; def run(x), do: S.run(x); end\n"},
     "use.ex", "Root.Service", "service.ex"),
])
def test_mcp_imports_resolve_native_module_aliases(project, monkeypatch, files, path,
                                                 expected_module, target):
    from srclight import server
    root, index, _, _ = project
    db = index(files)
    monkeypatch.setattr(server, "_db", db)
    monkeypatch.setattr(server, "_repo_root", root)
    monkeypatch.setattr(server, "_workspace_name", None)
    result = json.loads(server.find_imports(path))
    assert result["imports"][0]["module"] == expected_module
    assert result["imports"][0]["resolved_to"]["file"] == target
    assert result["resolved_count"] == 1


def test_mcp_pattern_search_does_not_claim_complete_syntax_coverage(project, monkeypatch):
    from srclight import server
    root, index, _, _ = project
    db = index({"Partial.fs": "module Partial\ntype R = { Value: }\n"
                             "let usable x = x\n"})
    monkeypatch.setattr(server, "_db", db)
    monkeypatch.setattr(server, "_repo_root", root)
    monkeypatch.setattr(server, "_workspace_name", None)
    status = json.loads(server.index_status())
    result = json.loads(server.find_pattern("usable", language="fsharp"))
    assert "Partial.fs" in status["parse_errors"]
    assert result["parse_errors"] == status["parse_errors"]
    assert "partial" in result["unindexed_note"]


def test_fsharp_match_and_loop_patterns_shadow_global_function_names(project):
    _, index, _, _ = project
    db = index({"Patterns.fs": '''module Patterns
let helper x = x + 1
let other x = x + 2
let choose value =
    match value with
    | Some helper -> helper 1
    | None -> other 2
let each values =
    for helper in values do
        helper 1
'''})
    assert callees(db, "Patterns.choose") == {"Patterns.other"}
    assert callees(db, "Patterns.each") == set()
