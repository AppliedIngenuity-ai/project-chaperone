"""The modules of the page (tool BROWSER): the four elements of the specification's 2.13."""

from urllib.parse import urlsplit

import chaperone


WHOLE = ""                                                  # a request for the whole result


@chaperone.module("user.show", properties={"DESTRUCTIVE": False})
def user_show(ctx, report, marks, style=None):
    ctx.show(report, marks, style)


@chaperone.module("user.show_update", properties={"DESTRUCTIVE": False})
def user_show_update(ctx, report, marks):
    """Displays news of what a run is doing. Displayed only when ui.yaml says `show_updates: true`."""
    ctx.show(report, marks, None, True)


@chaperone.module("user.describe", properties={"DESTRUCTIVE": False})
def user_describe(ctx, prompt, asked=None):
    """What the user typed, with what the user was asked: `asked`, or the prompt when `asked` is not given."""
    return {"asked": prompt if asked is None else asked, "reply": ctx.describe(prompt)}


@chaperone.module("user.choose", properties={"DESTRUCTIVE": False})
def user_choose(ctx, items, prompt):
    return ctx.choose(items, prompt)


@chaperone.module("user.choose_senders", properties={"DESTRUCTIVE": False},
              requests={"UNTRUSTED": False, "LLM_WRITTEN": False})
def user_choose_senders(ctx, items, prompt, monitored):
    """The addresses of the senders the user ticked. Pressing the button is the approval. A sender whose address is
    in `monitored` is displayed ticked and fixed, and is not returned again."""
    addresses = [sender["address"] for sender in ctx.choose(items, prompt, [s for s in items if s["address"] in monitored])]
    return addresses, [(WHOLE, "UNTRUSTED", False), (WHOLE, "LLM_WRITTEN", False)]


@chaperone.module("user.enter_senders", properties={"DESTRUCTIVE": False})
def user_enter_senders(ctx, prompt):
    """The addresses the user typed, one per line, parsed by the element. Entered, not yet approved: the user ticks
    them in `user.choose_senders`, after seeing how many messages the mailbox has from each."""
    return ctx.enter(prompt, {"addresses": "email_list"})["addresses"]


@chaperone.module("user.choose_links", properties={"DESTRUCTIVE": False}, requests={"UNTRUSTED": False})
def user_choose_links(ctx, items, prompt):
    """The link rules the user ticked. What a tick means is the prompt's and the workflow's: an approval (from then on
    a tracking link that fits the rule may be written on a calendar entry) or a decline. Each rule is displayed as the
    link it fits, on one line, with the words <tracking number> where the number goes:
    https://www.ups.com/track?tracknum=<tracking number>. A rule whose site is not the sender's domain or under it
    is displayed with a warning; it may still be right (a shop's mail with a carrier's link)."""
    shown = []
    for rule in items:
        line = {"link": f"{rule['site']}{rule['path']}?{rule['parameter']}=<tracking number>", "in mail from": rule["sender"]}
        host = (urlsplit(rule["site"]).hostname or "").lower()
        if host != rule["sender"] and not host.endswith("." + rule["sender"]):
            line["warning"] = f"the site of this link is not {rule['sender']} or under it"
        shown.append(line)
    ticked = ctx.choose(shown, prompt)
    return [rule for rule, line in zip(items, shown) if line in ticked], [(WHOLE, "UNTRUSTED", False)]
