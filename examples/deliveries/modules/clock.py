"""Today's date (tool CLOCK)."""

import chaperone


@chaperone.module("clock.today", properties={"DESTRUCTIVE": False})
def clock_today(ctx):
    return {"day": ctx.today()}
