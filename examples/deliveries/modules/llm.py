"""The modules that ask an LLM (tool LOCAL_LLM). Their prompts are in prompts.yaml."""

import chaperone


@chaperone.module("llm.next_step", properties={"DESTRUCTIVE": False})
def llm_next_step(ctx, asked, reply, sizes, options, not_offered):
    """Offers the LLM the options whose value is true or a number above zero, and returns the one it chose with its
    arguments; `unusable` when its answer is none of them. The answer is read generously: spaces, quotes, a full stop
    and capitals around the option's name are taken away, and one search term given as a text is a list of one.
    When the LLM chooses an option that exists and is not offered now (its list is empty), the user is told so: the
    result is `clarify` with the text `not_offered`, in which {option} is the option's name."""
    offered = [name for name, on in options.items() if on is True or (not isinstance(on, bool) and on > 0)]
    answer = ctx.next_step(asked=asked, reply=reply, sizes=sizes, options=offered)
    option = answer.get("option") if isinstance(answer, dict) else None
    option = option.strip(" \t\n\"'.`").lower() if isinstance(option, str) else None
    if option in options and option not in offered:
        return {"option": "clarify", "message": not_offered.replace("{option}", option)}
    if option not in offered:
        return {"option": "unusable"}
    if option == "search":
        terms = answer.get("terms")
        terms = [terms] if isinstance(terms, str) else terms
        if not isinstance(terms, list) or not terms or not all(isinstance(term, str) and term.strip() for term in terms):
            return {"option": "unusable"}
        return {"option": option, "terms": [term.strip() for term in terms]}
    if option == "clarify":
        message = answer.get("message")
        if not isinstance(message, str) or not message.strip():
            return {"option": "unusable"}
        return {"option": option, "message": message.strip()}
    return {"option": option}


VALUES = ("tracking_number", "date", "from_time", "to_time", "delivered_time", "shipper")    # what an LLM is asked to copy


def _shown(message, max_chars):
    """The subject and the body of a message, as an LLM is passed them. A longer body is refused, never cut."""
    subject, body = message.get("subject", ""), message.get("body", "")
    if len(body) > max_chars:
        raise chaperone.ModuleError("too_long", f"the body has more than {max_chars} characters")
    return {"subject": subject, "body": body}


def _word(text):
    """An LLM's one-word answer, read generously: without spaces, quotes, a full stop or capitals around it."""
    return text.strip(" \t\n\"'.`").lower() if isinstance(text, str) else None


@chaperone.module("llm.classify_message", properties={"DESTRUCTIVE": False})
def llm_classify_message(ctx, message, max_chars):
    """The kind of the message, as the LLM answered (prompt `classify`). The workflow's branch says which kinds exist."""
    return {"kind": _word(ctx.classify(**_shown(message, max_chars)))}


@chaperone.module("llm.copy_values", properties={"DESTRUCTIVE": False})
def llm_copy_values(ctx, message, max_chars):
    """What the LLM copied out of the message (prompt `copy_values`): the tracking number, the date, the earliest and
    the latest time of the delivery, the time it was delivered, the shipper's name. Any of them may be absent."""
    answer = ctx.copy_values(**_shown(message, max_chars))
    if not isinstance(answer, dict):
        raise chaperone.ModuleError("bad_answer", "the LLM's answer is not an object with the values")
    return {name: answer.get(name) for name in VALUES}
