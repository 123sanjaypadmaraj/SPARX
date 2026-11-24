"""What the approver is shown: a registered preview adds `approving`; the tool and the audit keep the real arguments."""
import json

from sparx import security, tools
from sparx.tools import Context, tool

from conftest import run_tool

GOT = []


@tool("apv_send", "send", {"ref": {"type": "integer"}, "note": {"type": "string"}}, ["ref"], category="testing", risk="send")
def _send(ctx, **kw):
    GOT.append(kw)
    return "sent"


@tool("apv_plain", "plain", {"ref": {"type": "integer"}}, ["ref"], category="testing", risk="send")
def _plain(ctx, ref):
    GOT.append({"ref": ref})
    return "sent"


def setup_function():
    GOT.clear()
    tools.PREVIEWS.pop("apv_send", None)


def recorder(answer=True):
    seen = []

    async def approve(t, args):
        seen.append((t.name, args))
        return answer
    return Context(session="approval", approve=approve), seen


async def test_preview_is_shown_first_and_never_reaches_the_tool_or_the_audit():
    @tools.preview("apv_send")
    def _(args):
        return f"message {args['ref']} to asha@x.com"
    c, seen = recorder()
    assert await run_tool("apv_send", c, ref=7, note="n") == "sent"
    ((name, shown),) = seen
    assert name == "apv_send" and shown == {"approving": "message 7 to asha@x.com", "ref": 7, "note": "n"}
    assert list(shown)[0] == "approving"                     # first, so a display that cuts the arguments keeps it
    assert GOT == [{"ref": 7, "note": "n"}]
    (row,) = security.audit_log(tool="apv_send")
    assert json.loads(row["args"]) == {"ref": 7, "note": "n"} and row["ok"] == 1

    c, seen = recorder(False)                                # declined: the audit row has the real arguments too
    assert "the user declined" in await run_tool("apv_send", c, ref=8)
    assert seen[0][1]["approving"] == "message 8 to asha@x.com" and len(GOT) == 1
    assert json.loads(security.audit_log(tool="apv_send")[0]["args"]) == {"ref": 8}


async def test_preview_is_capped_redacted_and_may_be_structured():
    tools.preview("apv_send")(lambda args: "x" * 5000)
    c, seen = recorder()
    await run_tool("apv_send", c, ref=1)
    assert len(seen[0][1]["approving"]) == 1500

    tools.preview("apv_send")(lambda args: {"to": "asha@x.com", "key": "ghp_abcdefghijklmnopqrstuvwx"})
    c, seen = recorder()
    await run_tool("apv_send", c, ref=1)
    shown = seen[0][1]["approving"]
    assert isinstance(shown, str) and "asha@x.com" in shown and "ghp_abcdefghijklmnopqrstuvwx" not in shown


async def test_a_failing_or_empty_preview_changes_nothing():
    @tools.preview("apv_send")
    def _(args):
        raise RuntimeError("boom")
    c, seen = recorder()
    assert await run_tool("apv_send", c, ref=3) == "sent"
    assert seen == [("apv_send", {"ref": 3})] and GOT == [{"ref": 3}]

    tools.preview("apv_send")(lambda args: None)
    c, seen = recorder()
    assert await run_tool("apv_send", c, ref=4) == "sent" and seen == [("apv_send", {"ref": 4})]


async def test_tool_without_a_preview_is_asked_exactly_as_before():
    assert "apv_plain" not in tools.PREVIEWS
    c, seen = recorder()
    assert await run_tool("apv_plain", c, ref=5) == "sent"
    assert seen == [("apv_plain", {"ref": 5})] and GOT == [{"ref": 5}]


async def test_sending_a_draft_shows_who_it_goes_to_and_what_it_says(ctx):
    from sparx.tools import comms
    d = comms._store_draft(["asha@x.com", "ravi@x.com"], ["boss@x.com"], "Dinner on Friday", "B" * 700,
                           attachments=["C:/docs/menu.pdf"])
    c, seen = recorder(False)                                # declined: nothing is sent, no mail server needed
    out = await run_tool("email_send", c, draft_id=d["draft_id"], confirm=d["confirm"])
    assert "the user declined" in out
    ((name, shown),) = seen
    assert name == "email_send" and list(shown) == ["approving", "draft_id", "confirm"]
    assert shown["draft_id"] == d["draft_id"] and shown["confirm"] == d["confirm"]
    text = shown["approving"]
    assert "asha@x.com, ravi@x.com" in text and "boss@x.com" in text and "Dinner on Friday" in text
    assert "menu.pdf" in text and "C:/docs" not in text
    assert "B" * 500 in text and "B" * 501 not in text
    assert text.index("asha@x.com") < text.index("Dinner on Friday") < 120      # what matters survives a short cut
    assert json.loads(security.audit_log(tool="email_send")[0]["args"]) == {"draft_id": d["draft_id"], "confirm": d["confirm"]}

    c, seen = recorder(False)
    await run_tool("email_send", c, draft_id=99, confirm="x")
    assert "draft not found" in seen[0][1]["approving"]

    c, seen = recorder(False)                                # a direct send already carries everything in its arguments
    await run_tool("email_send", c, to="asha@x.com", subject="Hi", body="x")
    assert seen == [("email_send", {"to": "asha@x.com", "subject": "Hi", "body": "x"})]
