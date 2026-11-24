"""Argument coercion and validation (sparx/argcheck.py) and its hook in tools.call."""
import json

import pytest

from conftest import run_tool
from sparx import argcheck, db, security, tools
from sparx.argcheck import coerce
from sparx.tools import A, B, I, N, O, S, Context, tool

tools.load_all()

IDS = {"type": "array", "items": I}
PARAMS = {"n": I, "x": N, "flag": B, "text": S, "tags": A, "ids": IDS, "data": O,
          "rows": {"type": "array", "items": O}, "level": {"type": "string", "enum": ["low", "high"]}}


def ok(args, params=PARAMS, required=()):
    out, err = coerce({"type": "object", "properties": params, "required": list(required)}, args)
    assert err is None, err
    return out


def bad(args, params=PARAMS, required=()):
    out, err = coerce({"type": "object", "properties": params, "required": list(required)}, args)
    assert err and err.startswith("Error:")
    assert out == args                          # a rejected call hands back what it was given
    return err


def same(a, b):
    """Equal including types, so 5 vs 5.0 vs True are told apart."""
    return json.dumps(a, sort_keys=True) == json.dumps(b, sort_keys=True)


@pytest.mark.parametrize("given, want", [
    ({"n": "5"}, {"n": 5}), ({"n": " 5 "}, {"n": 5}), ({"n": "5.0"}, {"n": 5}), ({"n": "-12"}, {"n": -12}),
    ({"n": "1e3"}, {"n": 1000}), ({"n": 5.0}, {"n": 5}), ({"n": 7}, {"n": 7}),
    ({"n": "123456789012345678901"}, {"n": 123456789012345678901}),
    ({"x": "2.5"}, {"x": 2.5}), ({"x": "3"}, {"x": 3}), ({"x": 3}, {"x": 3}), ({"x": 1.5}, {"x": 1.5}),
    ({"flag": "true"}, {"flag": True}), ({"flag": "FALSE"}, {"flag": False}), ({"flag": "Yes"}, {"flag": True}),
    ({"flag": "no"}, {"flag": False}), ({"flag": "1"}, {"flag": True}), ({"flag": "0"}, {"flag": False}),
    ({"flag": 1}, {"flag": True}), ({"flag": 0}, {"flag": False}), ({"flag": True}, {"flag": True}),
    ({"text": 5}, {"text": "5"}), ({"text": 2.5}, {"text": "2.5"}), ({"text": "5"}, {"text": "5"}),
    ({"tags": '["a", "b"]'}, {"tags": ["a", "b"]}), ({"tags": "solo"}, {"tags": ["solo"]}),
    ({"tags": [1, "b"]}, {"tags": ["1", "b"]}), ({"tags": ("a",)}, {"tags": ["a"]}), ({"tags": 7}, {"tags": ["7"]}),
    ({"ids": "[1, 2]"}, {"ids": [1, 2]}), ({"ids": "4"}, {"ids": [4]}), ({"ids": 4}, {"ids": [4]}),
    ({"ids": ["1", 2]}, {"ids": [1, 2]}),
    ({"data": '{"a": 1}'}, {"data": {"a": 1}}), ({"data": {"a": "1"}}, {"data": {"a": "1"}}),
    ({"rows": '[{"a": 1}]'}, {"rows": [{"a": 1}]}), ({"rows": {"a": 1}}, {"rows": [{"a": 1}]}),
    ({"rows": '{"a": 1}'}, {"rows": [{"a": 1}]}),
    ({"level": "HIGH "}, {"level": "high"}), ({"level": "low"}, {"level": "low"}),
])
def test_lossless_coercions(given, want):
    assert same(ok(given), want)


@pytest.mark.parametrize("given, arg, expected", [
    ({"n": "five"}, "n", "integer"), ({"n": "5.5"}, "n", "integer"), ({"n": True}, "n", "integer"),
    ({"n": [5]}, "n", "integer"), ({"n": {"v": 5}}, "n", "integer"), ({"n": "nan"}, "n", "integer"),
    ({"x": "abc"}, "x", "number"), ({"x": False}, "x", "number"), ({"x": "inf"}, "x", "number"),
    ({"flag": "maybe"}, "flag", "boolean"), ({"flag": 2}, "flag", "boolean"), ({"flag": 1.0}, "flag", "boolean"),
    ({"text": True}, "text", "string"), ({"text": ["a"]}, "text", "string"), ({"text": {"a": 1}}, "text", "string"),
    ({"tags": {"a": 1}}, "tags", "array of string"), ({"ids": "one"}, "ids", "array of integer"),
    ({"ids": True}, "ids", "array of integer"), ({"rows": 3}, "rows", "array of object"),
    ({"rows": "plain words"}, "rows", "array of object"),
    ({"data": "plain words"}, "data", "object"), ({"data": [1]}, "data", "object"), ({"data": 3}, "data", "object"),
    ({"data": "[1, 2]"}, "data", "object"),
])
def test_rejections_name_argument_expected_type_and_value(given, arg, expected):
    err = bad(given)
    value = given[arg]
    assert f"'{arg}' must be {expected}" in err
    assert json.dumps(value)[:20] in err and "takes:" in err


def test_error_says_what_was_received_and_lists_every_problem():
    err = bad({"n": "five", "flag": "maybe"}, required=["text"])
    assert "missing required argument(s): text" in err
    assert "'n' must be integer, received string \"five\"" in err
    assert "'flag' must be boolean, received string \"maybe\"" in err
    assert "n (integer)" in err and "text (string, required)" in err
    assert "received boolean true" in bad({"n": True})
    long = bad({"n": "z" * 500})
    assert "..." in long and len(long) < 600


def test_missing_required():
    assert "missing required argument(s): n, text" in bad({}, required=["n", "text"])
    assert "missing required argument(s): n" in bad({"n": None}, required=["n"])
    assert ok({"n": "0", "flag": "false"}, required=["n", "flag"]) == {"n": 0, "flag": False}   # falsy is not missing
    assert ok({"text": ""}, required=["text"]) == {"text": ""}      # tools.call owns the empty-string rule


def test_none_and_blank_optionals_are_dropped():
    assert ok({"n": None, "text": None, "tags": None, "x": 1}) == {"x": 1}
    assert ok({"n": "", "flag": " ", "tags": "", "data": "", "x": ""}) == {}
    assert ok({"text": "", "level": ""}) == {"text": "", "level": ""}    # "" is a real string
    assert "'n' must be integer" in bad({"n": ""}, required=["n"])


def test_bool_never_becomes_a_number_and_values_that_work_today_are_kept():
    assert "'n' must be integer" in bad({"n": True}) and "'x' must be number" in bad({"x": True})
    assert "'ids' must be array of integer" in bad({"ids": False})
    assert same(ok({"n": 2.5}), {"n": 2.5})                        # a number where an integer was declared
    assert same(ok({"x": 3}), {"x": 3})                            # an int already is a number
    assert ok({"level": "urgent"}) == {"level": "urgent"}          # outside the enum: the tool refuses it itself
    assert ok({"tags": "a, b"}) == {"tags": "a, b"}                # several values in one string: not guessed at
    assert ok({"tags": "[draft] plan"}) == {"tags": ["[draft] plan"]}
    assert ok({"rows": "[{broken"}) == {"rows": "[{broken"}        # broken JSON: the tool explains it
    assert ok({"data": "{broken"}) == {"data": "{broken"}
    assert ok({"extra": "1", "n": "1"}) == {"extra": "1", "n": 1}  # undeclared arguments are not touched


def test_nested_values_are_repaired_but_never_rejected():
    params = {"items": {"type": "array", "items": {"type": "object", "properties": {"qty": I, "done": B}}},
              "opts": {"type": "object", "properties": {"limit": I}}}
    out = ok({"items": [{"qty": "2", "done": "yes"}, {"qty": "many"}, "loose"], "opts": '{"limit": "9", "k": "v"}'}, params)
    assert out == {"items": [{"qty": 2, "done": True}, {"qty": "many"}, "loose"], "opts": {"limit": 9, "k": "v"}}
    assert ok({"ids": ["x", "3"]}) == {"ids": ["x", 3]}


@pytest.mark.parametrize("schema, value", [
    ({}, "5"), ({"description": "anything"}, [1]), ({"type": "tuple"}, "5"), ({"type": None}, "5"),
    ({"anyOf": [I, S]}, "5"), ({"oneOf": [I, B]}, "x"), ({"$ref": "#/defs/x"}, "5"), ({"type": "null"}, "5"),
    ("string", 5), (None, 5), ([I], "5"), ({"type": {"bad": 1}}, "5"), ({"type": ["string", "null"]}, "5"),
    ({"type": ["integer", "string"]}, "5"), ({"type": ["array", "object"]}, 5), ({"type": "array"}, [1, "a", None]),
    ({"type": "array", "items": "string"}, [1]), ({"type": "array", "items": {}}, [[1], "a"]),
    ({"type": "object", "properties": "nope"}, {"a": 1}), ({"type": "string", "enum": "low"}, "LOW"),
    ({"type": "string", "enum": []}, "x"), ({"type": "integer", "enum": [1, 2]}, 9),
])
def test_schemas_not_understood_pass_through_unchanged(schema, value):
    assert same(ok({"v": value}, {"v": schema}), {"v": value})


def test_union_types_take_the_first_lossless_fit():
    assert same(ok({"v": "5"}, {"v": {"type": ["integer", "null"]}}), {"v": 5})
    assert same(ok({"v": 5}, {"v": {"type": ["number", "string"]}}), {"v": 5})
    assert same(ok({"v": "x"}, {"v": {"type": ["integer", "boolean"]}}), {"v": "x"})    # nothing fits: untouched


def test_odd_specs_and_arguments_never_raise():
    for spec in (None, 5, "x", [], {}, {"type": "object"}, {"type": "object", "properties": None},
                 {"type": "object", "properties": {"n": I}, "required": "n"},
                 {"type": "object", "properties": {"n": I}, "required": [None, 3]}):
        assert coerce(spec, {"n": "5", "z": object}) [1] is None
    for args in (None, "x", [1], 5):
        assert coerce({"n": I}, args) == (args, None)
    assert coerce({"n": I}, {"n": "5"}) == ({"n": 5}, None)         # a bare params dict works as a spec
    assert coerce({"n": I}, {1: "a", "n": "2"}) == ({1: "a", "n": 2}, None)


def test_input_is_not_modified_and_tool_objects_are_specs():
    def fn(ctx, n, text="", limit=10, strict=None, *, must):
        return n
    t = tools.Tool("t_spec", "d", {"n": I, "text": S, "limit": I, "must": I, "ghost": I}, ["n"], "testing", "read", False, 5, fn)
    given = {"n": "3", "limit": None, "must": None, "ghost": None, "text": 4}
    out, err = coerce(t, given)
    # `must` has no default in the function, so dropping it would break the call: it stays as sent
    assert err is None and out == {"n": 3, "must": None, "text": "4"}
    assert given == {"n": "3", "limit": None, "must": None, "ghost": None, "text": 4}
    assert "t_spec takes: n (integer, required)" in coerce(t, {})[1]


def test_every_registered_tool_schema_is_accepted():
    assert not tools.LOAD_ERRORS and len(tools.REGISTRY) > 100
    probes = ["5", "true", "", " ", "x, y", "[1]", '{"a": 1}', "[{broken", 5, 2.5, True, None, [1, "a"], {"a": 1}, ("a",)]
    for t in list(tools.REGISTRY.values()):
        assert isinstance(t.params, dict), t.name
        out, err = coerce(t, {})
        assert out == {} and (err is None) == (not t.required), t.name
        if err:
            assert all(r in err for r in t.required), t.name
        for probe in probes:
            out, err = coerce(t, {k: probe for k in t.params})
            assert isinstance(out, dict) and (err is None or err.startswith("Error:")), t.name
        out, err = coerce(t, {"__not_a_param__": 1})
        assert out.get("__not_a_param__") == 1, t.name


# ---------- through the registry ----------

seen = []


@tool("t_args", "Record what arrived.", {"count": I, "on": B, "tags": A, "note": S, "data": O}, ["count"],
      category="testing", risk="read")
async def t_args(ctx, count: int, on: bool = False, tags: list | None = None, note: str = "none", data: dict | None = None):
    seen.append({"count": count, "on": on, "tags": tags, "note": note, "data": data})
    return seen[-1]


@tool("t_args_send", "Pretend to post a number of items.", {"count": I, "to": S}, ["count", "to"],
      category="testing", risk="send")
def t_args_send(ctx, count: int, to: str):
    return f"sent {count + 1} to {to}"


async def test_call_runs_the_tool_with_coerced_arguments(ctx):
    seen.clear()
    out = json.loads(await run_tool("t_args", ctx, count="5", on="yes", tags='["a", "b"]', note=7, data='{"k": 1}'))
    assert same(out, {"count": 5, "on": True, "tags": ["a", "b"], "note": "7", "data": {"k": 1}})
    out = json.loads(await run_tool("t_args", ctx, count=2.0, on=None, tags="solo", note=None, data=""))
    assert same(out, {"count": 2, "on": False, "tags": ["solo"], "note": "none", "data": None})
    assert len(seen) == 2


async def test_call_rejects_before_running_or_asking(ctx):
    seen.clear()
    err = await run_tool("t_args", ctx, count="lots", on="perhaps")
    assert err.startswith("Error:") and "'count' must be integer, received string \"lots\"" in err
    assert "'on' must be boolean" in err and "t_args takes: count (integer, required)" in err
    err = await run_tool("t_args", ctx, on=True, cuont=3)
    assert err.startswith("Error: missing required argument(s): count") and "takes: count" in err
    assert seen == []
    assert (await run_tool("t_args_send", ctx, count=True, to="a@b.c")).startswith("Error:")
    assert ctx.asked == [] and db.q("SELECT * FROM audit") == []     # nobody was asked, nothing ran
    assert ctx.stats["tool_calls"] == 0


async def test_approval_and_audit_see_the_arguments_that_run():
    asked = []

    async def approve(t, args):
        asked.append(dict(args))
        return True
    c = Context(session="t", approve=approve)
    assert await run_tool("t_args_send", c, count="41", to="a@b.c", cc="dropped") == "sent 42 to a@b.c"
    assert same(asked, [{"count": 41, "to": "a@b.c"}])
    row = db.one("SELECT args, ok FROM audit WHERE tool='t_args_send'")
    assert row["ok"] and same(json.loads(row["args"]), {"count": 41, "to": "a@b.c"})

    async def no(t, args):
        asked.append(dict(args))
        return False
    assert "declined" in await run_tool("t_args_send", Context(session="t", approve=no), count="7", to="x@y.z")
    assert same(asked[-1], {"count": 7, "to": "x@y.z"})
    assert security.permission("send", "t_args_send") == "ask"


async def test_real_tools_accept_stringly_arguments(ctx):
    assert '"result": 14' in await run_tool("calculate", ctx, expression="2 + 3 * 4")
    out = await run_tool("load_tools", ctx, groups='["testing"]')
    assert "testing" in ctx.active and not out.startswith("Error")


# ---- a value outside a declared enum is refused for the tools that would otherwise answer wrongly

def test_strict_enum_tools_exist_and_declare_an_enum():
    for name in argcheck.STRICT_ENUM_TOOLS:
        assert name in tools.REGISTRY, f"{name} is no longer a registered tool"
        assert any(isinstance(s, dict) and s.get("enum") for s in tools.REGISTRY[name].params.values()), name


async def test_a_made_up_filter_value_is_refused_not_answered(ctx, monkeypatch):
    await run_tool("task_add", ctx, title="Buy milk")
    out = await run_tool("task_list", ctx, status="pending")
    assert out.startswith("Error:") and "'status'" in out and '"open"' in out and "one of" in out
    assert "Buy milk" in await run_tool("task_list", ctx, status="Open ")                       # spelling is repaired
    assert "Buy milk" in await run_tool("task_list", ctx, status="open", priority="")               # blank = not given

    def no_network(*a, **k):
        raise AssertionError("a refused call reached the network")
    import httpx
    monkeypatch.setattr(httpx, "AsyncClient", no_network)
    monkeypatch.setattr(httpx, "Client", no_network)
    for name, args in (("research_queue_list", {"status": "bogus"}), ("goal_list", {"status": "finished"}),
                       ("note_search", {"query": "x", "kind": "diary"}),
                       ("web_quote", {"symbol": "AAPL", "kind": "forex"})):
        out = await run_tool(name, ctx, **args)
        assert out.startswith("Error:") and "one of" in out, (name, out)


def test_declared_values_and_tools_outside_the_set_are_untouched():
    fixed, err = argcheck.coerce(tools.REGISTRY["web_search"], {"query": "kites", "kind": "news"})
    assert err is None and fixed["kind"] == "news"
    assert argcheck.coerce(tools.REGISTRY["web_search"], {"query": "kites", "kind": "videos"})[1].startswith("Error:")
    assert "memory_export" not in argcheck.STRICT_ENUM_TOOLS
    fixed, err = argcheck.coerce(tools.REGISTRY["memory_export"], {"format": "md"})
    assert err is None and fixed["format"] == "md"
    assert ok({"level": "urgent"}) == {"level": "urgent"}
