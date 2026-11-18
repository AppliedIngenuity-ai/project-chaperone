"""The senders listed in a file (tool SENDERS_FILE)."""

import chaperone


@chaperone.module("file.monitored_senders", properties={"DESTRUCTIVE": False})
def file_monitored_senders(ctx):
    return ctx.read_lines()
