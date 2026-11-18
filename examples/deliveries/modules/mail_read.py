"""The modules that read the mailbox (tool MAILBOX). The setup's line `mail.read.*` states their facts."""

import chaperone


@chaperone.module("mail.read.search_senders", properties={"DESTRUCTIVE": False})
def mail_read_search_senders(ctx, terms, subjects):
    """The senders of the messages that mention a term: [{address, messages, subjects}], with at most `subjects`
    subjects of each sender's newest messages, for the user to tell the senders apart."""
    return ctx.search_senders(terms, subjects)


@chaperone.module("mail.read.count_messages", properties={"DESTRUCTIVE": False})
def mail_read_count_messages(ctx, addresses):
    """How many messages the mailbox has from each address, in the order given: numbers and nothing else. The
    addresses are the caller's; they are not passed back, so that they are not displayed as something the mailbox said."""
    return [one["messages"] for one in ctx.count_messages(addresses)]


@chaperone.module("mail.read.fetch_next_by_sender", properties={"DESTRUCTIVE": False})
def mail_read_fetch_next_by_sender(ctx, senders, handled):
    """The oldest message from the listed senders whose id is not in `handled`; `found` is false when none is left."""
    message = ctx.fetch_next([address for one_list in senders for address in one_list], handled)
    return {"found": message is not None, "message": message}
