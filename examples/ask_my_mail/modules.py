"""The modules of "ask my mail". Each talks to its tool and does nothing else."""

import chaperone


@chaperone.module("user.enter", properties={"DESTRUCTIVE": False})
def user_enter(ctx, prompt, fields):
    return ctx.enter(prompt, fields)


@chaperone.module("user.describe", properties={"DESTRUCTIVE": False})
def user_describe(ctx, prompt):
    return ctx.describe(prompt)


@chaperone.module("mail.search", properties={"DESTRUCTIVE": False, "OUTPUT": False})
def mail_search(ctx, keyword):
    return ctx.search(keyword)


@chaperone.module("llm.answer", properties={"DESTRUCTIVE": False})
def llm_answer(ctx, question, messages):
    return ctx.ask({"question": question, "messages": messages})


@chaperone.module("user.show", properties={"DESTRUCTIVE": False})
def user_show(ctx, report, marks, style=None):
    ctx.show(report, marks, style)
