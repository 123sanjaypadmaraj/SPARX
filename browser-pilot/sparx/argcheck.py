"""Coerce and validate tool arguments against a tool's declared parameter schema, before the tool runs.

Models (small local ones especially) send "5" for an integer, "true" for a boolean, a JSON string for a list,
or leave out a required argument. `coerce` repairs what can be repaired without guessing and turns the rest
into one `Error:` line the model can act on. Anything in a schema it does not understand passes through
unchanged, and so do arguments the schema does not mention.
"""
import inspect
import json
import math
import re

_INT = re.compile(r"[+-]?\d+(\.0*)?$")
_TRUE, _FALSE = {"true", "yes", "1"}, {"false", "no", "0"}
_SCALARS = ("string", "integer", "number", "boolean")
_LISTY = re.compile(r"[,;\n]")
_DROP = object()


class _Bad(Exception):
    """The value cannot become what the schema asks for. The message is the expectation."""


def _kind(v) -> str:
    if v is None:
        return "null"
    if isinstance(v, bool):
        return "boolean"
    if isinstance(v, int):
        return "integer"
    if isinstance(v, float):
        return "number"
    if isinstance(v, str):
        return "string"
    if isinstance(v, (list, tuple)):
        return "array"
    return "object" if isinstance(v, dict) else type(v).__name__


def _show(v) -> str:
    try:
        s = json.dumps(v, ensure_ascii=False, default=str)
    except Exception:
        s = repr(v)
    return f"{_kind(v)} {s if len(s) <= 60 else s[:57] + '...'}"


def _expect(schema: dict, enum: bool = True) -> str:
    t = schema.get("type")
    out = " or ".join(map(str, t)) if isinstance(t, list) else str(t or "value")
    items = schema.get("items")
    if t == "array" and isinstance(items, dict) and isinstance(items.get("type"), str):
        out = f"array of {items['type']}"
    values = schema.get("enum") if enum else None
    if isinstance(values, list) and values:
        out += " (one of: " + ", ".join(json.dumps(e, ensure_ascii=False, default=str) for e in values) + ")"
    return out


def _parse(text: str, opener: str, kind: type):
    """A JSON-looking string parsed into a list/dict, else None."""
    if not text.startswith(opener):
        return None
    try:
        v = json.loads(text)
    except ValueError:
        return None
    return v if isinstance(v, kind) else None


def _integer(v):
    if isinstance(v, bool):
        raise _Bad("integer")
    if isinstance(v, int):
        return v
    if isinstance(v, float):
        return int(v) if v.is_integer() else v      # 2.5 was accepted before this check existed; leave it
    if isinstance(v, str):
        s = v.strip()
        if _INT.match(s):
            return int(s.split(".")[0])
        try:
            f = float(s)                            # "1e3"
        except ValueError:
            raise _Bad("integer") from None
        if math.isfinite(f) and f.is_integer():
            return int(f)
    raise _Bad("integer")


def _number(v):
    if isinstance(v, bool):
        raise _Bad("number")
    if isinstance(v, (int, float)):
        return v                                    # an int already is a valid number
    if isinstance(v, str):
        s = v.strip()
        if re.fullmatch(r"[+-]?\d+", s):
            return int(s)
        try:
            f = float(s)
        except ValueError:
            raise _Bad("number") from None
        if math.isfinite(f):
            return f
    raise _Bad("number")


def _boolean(v):
    if isinstance(v, bool):
        return v
    if isinstance(v, int) and v in (0, 1):
        return bool(v)
    if isinstance(v, str):
        s = v.strip().lower()
        if s in _TRUE:
            return True
        if s in _FALSE:
            return False
    raise _Bad("boolean")


def _string(v):
    if isinstance(v, str):
        return v
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        return str(v)
    raise _Bad("string")


def _array(schema: dict, v):
    items = schema.get("items") if isinstance(schema.get("items"), dict) else {}
    itype = items.get("type")
    if isinstance(v, tuple):
        v = list(v)
    if isinstance(v, str):
        s = v.strip()
        parsed = _parse(s, "[", list)
        if parsed is not None:
            v = parsed
        elif itype == "string":
            # One plain value becomes a one-element list. A string that looks like several values is left
            # alone: splitting it would be a guess, and some tools read such a string their own way.
            return v if _LISTY.search(s) else [v]
        elif s[:1] in ("[", "{") and _parse(s, "{", dict) is None:
            return v                                # broken JSON: the tool's own message explains it better
        elif itype in _SCALARS:
            v = [_value(items, s)]
        elif itype == "object" and s.startswith("{"):
            v = [_parse(s, "{", dict)]
        else:
            raise _Bad("array")
    elif isinstance(v, dict):
        if itype != "object":
            raise _Bad("array")
        v = [v]
    elif not isinstance(v, list):
        if itype not in _SCALARS:
            raise _Bad("array")
        v = [_value(items, v)]
    return [_soft(items, x) for x in v] if items else v


def _object(schema: dict, v):
    if isinstance(v, str) and v.strip().startswith("{"):
        parsed = _parse(v.strip(), "{", dict)
        if parsed is None:
            return v                                # broken JSON: the tool's own message explains it better
        v = parsed
    if not isinstance(v, dict):
        raise _Bad("object")
    props = schema.get("properties")
    if isinstance(props, dict):
        v = {k: _soft(props[k], x) if isinstance(props.get(k), dict) else x for k, x in v.items()}
    return v


_BY_TYPE = {"integer": lambda s, v: _integer(v), "number": lambda s, v: _number(v),
            "boolean": lambda s, v: _boolean(v), "string": lambda s, v: _string(v),
            "array": _array, "object": _object}


def _enum(schema: dict, v):
    """Spelling repair only ("High " -> "high"). A value outside the enum is left for the tool to refuse:
    tools here validate their own choices and say more about them than a generic message could."""
    enum = schema.get("enum")
    if isinstance(v, str) and isinstance(enum, list) and v not in enum:
        near = [e for e in enum if isinstance(e, str) and e.strip().casefold() == v.strip().casefold()]
        if len(near) == 1:
            return near[0]
    return v


def _value(schema: dict, v):
    """The value as the schema wants it. Raises _Bad when it cannot be that; unknown schemas return v as is."""
    t = schema.get("type")
    if isinstance(t, list):                         # union: keep a value that already fits, else first lossless fit
        names = [x for x in t if isinstance(x, str)]
        if _kind(v) in names or (_kind(v) == "integer" and "number" in names):
            return v
        for name in names:
            if name in _BY_TYPE:
                try:
                    return _BY_TYPE[name](schema, v)
                except _Bad:
                    pass
        return v
    if t in _BY_TYPE:
        v = _BY_TYPE[t](schema, v)
    elif t is not None or any(k in schema for k in ("anyOf", "oneOf", "allOf", "$ref", "not")):
        return v                                    # not a shape this module understands
    return _enum(schema, v)


def _soft(schema: dict, v):
    """Nested values are repaired when possible and never rejected: nested schemas are written loosely."""
    try:
        return _value(schema, v)
    except Exception:
        return v


def _spec(spec):
    """(name, params, required, fn) from a Tool, a {"type": "object", "properties": ...} schema or a bare params dict."""
    if isinstance(spec, dict):
        if spec.get("type") == "object" and isinstance(spec.get("properties"), dict):
            return "", spec["properties"], spec.get("required"), None
        return "", spec, None, None
    return (getattr(spec, "name", ""), getattr(spec, "params", None), getattr(spec, "required", None),
            getattr(spec, "fn", None))


def _droppable(fn, key: str) -> bool:
    """True when leaving `key` out cannot break the call: the function has a default for it or never takes it."""
    if fn is None:
        return True
    try:
        p = inspect.signature(fn).parameters.get(key)
    except (TypeError, ValueError):
        return False
    return p is None or p.default is not p.empty


def _one(schema: dict, v, optional: bool):
    if v is None:
        return _DROP if optional else v
    if optional and isinstance(v, str) and not v.strip():
        t = schema.get("type")
        if t in ("integer", "number", "boolean", "array", "object"):
            return _DROP                            # "" for an optional non-text argument means "not given"
        if t == "string":
            return v                                # "" is a legitimate string; the enum is not applied to it
    return _value(schema, v)


# Tools where a value outside a declared enum is refused here, because the tool itself would answer with a confident
# wrong result (the value goes straight into a query). Add a tool only after checking that it has no refusal message
# of its own for that argument and accepts no undeclared alias.
STRICT_ENUM_TOOLS = frozenset({"task_list", "note_search", "note_summarize", "goal_list", "web_search", "github_search",
                               "hackernews_search", "web_quote", "research_queue_list"})


def coerce(spec, args):
    """Return (args, error). `error` is None, or a line starting with "Error:" naming every bad argument.

    `args` is returned as a new dict with lossless repairs applied; the caller's dict is never modified.
    Never raises: if the schema or the arguments are not in a shape this understands, they come back unchanged.
    """
    try:
        return _coerce(spec, args)
    except Exception:
        return args, None


def _coerce(spec, args):
    name, params, required, fn = _spec(spec)
    if not isinstance(args, dict) or not isinstance(params, dict):
        return args, None
    required = [r for r in required if isinstance(r, str)] if isinstance(required, (list, tuple)) else []
    out, problems = {}, []
    for k, v in args.items():
        schema = params.get(k)
        if not isinstance(schema, dict):
            out[k] = v                              # not declared: tools.call decides what happens to extras
            continue
        optional = k not in required and _droppable(fn, k)
        try:
            new = _one(schema, v, optional)
        except _Bad:
            problems.append(f"'{k}' must be {_expect(schema)}, received {_show(v)}")
            continue
        except Exception:
            new = v
        allowed = schema.get("enum")
        if name in STRICT_ENUM_TOOLS and new is not _DROP and isinstance(allowed, list) and allowed \
                and new not in allowed and not (new == "" and k not in required):
            problems.append(f"'{k}' must be {_expect(schema)}, received {_show(v)}")
            continue
        if new is not _DROP:
            out[k] = new
    missing = [r for r in required if args.get(r) is None]
    if not missing and not problems:
        return out, None
    parts = []
    if missing:
        parts.append(f"missing required argument(s): {', '.join(missing)}")
    if problems:
        parts.append(f"bad argument(s): {'; '.join(problems)}")
    takes = ", ".join(f"{k} ({_expect(s, False) if isinstance(s, dict) else 'value'}{', required' if k in required else ''})"
                      for k, s in params.items())
    return args, f"Error: {'. '.join(parts)}. {name or 'This tool'} takes: {takes or 'no arguments'}. Fix and call again."
