"""The modules of "share notes". Each talks to its tool and does nothing else."""

import chaperone


@chaperone.module("notes.search", properties={"DESTRUCTIVE": False})
def notes_search(ctx, keyword):
    return ctx.search(keyword)


@chaperone.module("llm.summarize", properties={"DESTRUCTIVE": False})
def llm_summarize(ctx, notes):
    return ctx.summarize(notes)


@chaperone.module("user.show", properties={"DESTRUCTIVE": False})
def user_show(ctx, report, marks, style=None):
    ctx.show(report, marks, style)


@chaperone.module("mail.send.message", properties={"DESTRUCTIVE": False})
def mail_send_message(ctx, to, body):
    return ctx.send(to, body)
