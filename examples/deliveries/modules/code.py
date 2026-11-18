"""The modules that use no tool: plain code that checks, cleans, counts and reads the values of a message."""

import datetime
import html
import re
from urllib.parse import parse_qsl, urlsplit

import chaperone


WHOLE = ""                                                  # a request for the whole result


@chaperone.module("senders.with_counts", properties={"DESTRUCTIVE": False, "OUTPUT": False})
def senders_with_counts(addresses, counts):
    """Each address with its count, in the form the list of senders has: [{address, messages}]."""
    return [{"address": address, "messages": messages} for address, messages in zip(addresses, counts)]


@chaperone.module("lists.sizes", properties={"DESTRUCTIVE": False, "OUTPUT": False}, requests={"PERSONAL": False})
def lists_sizes(lists):
    """The number of items of each list, and nothing else about them."""
    return {name: len(items) for name, items in lists.items()}, [(WHOLE, "PERSONAL", False)]


@chaperone.module("counter.add_one", properties={"DESTRUCTIVE": False, "OUTPUT": False})
def counter_add_one(count, limit):
    return {"count": count + 1, "too_many": count + 1 >= limit}


MONTHS = ("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec")


ORDERS = {"month_first": "month first", "day_first": "day first"}


def _day(year, month, day):
    try:
        return datetime.date(year, month, day)
    except ValueError:
        return None


def _readings(text, received, day_words):
    """The day `text` states, read month first and read day first: {"month_first": a day or None, "day_first": ...};
    None when it has none of the forms read here: 2026-11-12; 11/12/2026, 11.12.26, 11-12-2026; November 12, 2026;
    12 Nov 2026; a month's name with no year (the year is then the one that puts the day nearest to `received`).
    A day stated by a word is counted from the day the message was received: a word of `day_words`, or the name of
    a day of the week, which is the first such day from the day received on (see _relative)."""
    found = re.search(r"\b(\d{4})-(\d{2})-(\d{2})\b", text)
    if found:
        day = _day(*map(int, found.groups()))
        return {"month_first": day, "day_first": day}
    found = re.search(r"\b(\d{1,2})[/.-](\d{1,2})[/.-](\d{4}|\d{2})\b", text)
    if found:
        first, second, year = map(int, found.groups())
        year += 2000 if year < 100 else 0
        return {"month_first": _day(year, first, second), "day_first": _day(year, second, first)}
    name_first = re.search(r"\b([A-Za-z]{3,9})\.? (\d{1,2})(?:st|nd|rd|th)?\b(?:,? (\d{4}))?", text)
    name_second = re.search(r"\b(\d{1,2})(?:st|nd|rd|th)? ([A-Za-z]{3,9})\b\.?(?:,? (\d{4}))?", text)
    for found, month_at, day_at in ((name_first, 1, 2), (name_second, 2, 1)):
        if found and found.group(month_at)[:3].lower() in MONTHS:
            month, day = MONTHS.index(found.group(month_at)[:3].lower()) + 1, int(found.group(day_at))
            years = [int(found.group(3))] if found.group(3) else [received.year - 1, received.year, received.year + 1]
            days = [d for d in (_day(year, month, day) for year in years) if d is not None]
            nearest = min(days, key=lambda d: abs((d - received).days)) if days else None
            return {"month_first": nearest, "day_first": nearest}
    day = _relative(text, received, day_words)
    return {"month_first": day, "day_first": day} if day else None


def _relative(text, received, day_words):
    """The day a word states, counted from `received`: a word of `day_words`, which gives the number of days to add
    (the workflow writes it: today 0, tomorrow 1), or a day of the week (the first one from `received` on, so
    "Wednesday" in a message received on a Wednesday is that day). None for any other text."""
    words = re.findall(r"[a-z]+", text.lower())
    stated = [days for word, days in day_words.items() if word.lower() in words]
    if len(set(stated)) == 1:
        return received + datetime.timedelta(days=stated[0])
    if stated:
        return None                                          # two words that state different days
    named = [WEEKDAYS.index(word[:3]) for word in words if re.fullmatch(r"(mon|tues?|wed(nes)?|thu(rs)?|fri|sat(ur)?|sun)(day)?", word)]
    if len(named) == 1:
        return received + datetime.timedelta(days=(named[0] - received.weekday()) % 7)
    return None


WEEKDAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")


def _weekday_before(date_text, text):
    """The day of the week the message prints just before the date (0 for Monday), or None when it prints none."""
    before = text[:text.index(date_text)]
    found = re.search(r"\b(mon|tue|wed|thu|fri|sat|sun)[a-z]*\.?,? ?$", before, re.IGNORECASE)
    return WEEKDAYS.index(found.group(1).lower()) if found else None


def _read_date(text, order, received, days_before, days_after, weekday, day_words):
    """(the day written year-month-day, a note). A reading fits when it is from `days_before` days before to
    `days_after` days after the day the message was received and, when the message prints a day of the week before
    the date, is that day of the week. The day is the reading in the configured `order` when that fits. When only the
    other order fits, that day is taken and the note says so. Otherwise the error `bad_values`."""
    readings = _readings(text, received, day_words)
    if readings is None:
        raise chaperone.ModuleError("bad_values", "the date is not in a form this check reads")
    fits = {name: day is not None and -days_before <= (day - received).days <= days_after
            and (weekday is None or day.weekday() == weekday) for name, day in readings.items()}
    other = next(name for name in ORDERS if name != order)
    if fits[order]:
        counted = _relative(text, received, day_words) is not None and not re.search(r"\d", text)
        return readings[order].isoformat(), (f"The message says “{text}”: the day was counted from {received.isoformat()}, "
                                             f"the day the message was received." if counted else "")
    if fits[other]:
        return readings[other].isoformat(), (f"The date was read {ORDERS[other]}, though date_order is {order}: read {ORDERS[order]}, it is "
                                             f"not within {days_after} days after the message was received, or not the day of the week printed with it.")
    raise chaperone.ModuleError("bad_values", f"the date is not from {days_before} day(s) before to {days_after} days after the day the "
                                          f"message was received, or is not the day of the week printed with it")


def _clock(text):
    """A time of day as the message prints it, as "HH:MM": 1:15 PM, 5pm, 10:25am, 14:00, noon. None for anything else."""
    text = text.strip().lower().replace(".", "")
    if text in ("noon", "midnight"):
        return "12:00" if text == "noon" else "00:00"
    found = re.fullmatch(r"(\d{1,2})(?::(\d{2}))? ?([ap])m", text)
    if found:
        hour, minute = int(found.group(1)), int(found.group(2) or 0)
        if not 1 <= hour <= 12 or minute > 59:
            return None
        return f"{hour % 12 + (12 if found.group(3) == 'p' else 0):02d}:{minute:02d}"
    found = re.fullmatch(r"(\d{1,2}):(\d{2})", text)
    if found and int(found.group(1)) <= 23 and int(found.group(2)) <= 59:
        return f"{int(found.group(1)):02d}:{found.group(2)}"
    return None


def _squeezed(text):
    return " ".join(text.split())


@chaperone.module("check.delivery_values", properties={"DESTRUCTIVE": False, "OUTPUT": False},
              requests={"CHECKED": True, "LLM_WRITTEN": False})
def check_delivery_values(values, required, optional, message, number_form, number_in_link, date_order, day_words, days_before, days_after):
    """The `required` values of the message, checked, `bad_values` otherwise; and each `optional` value the LLM gave
    that passes the same checks. An optional value that fails is left out, and `note` says so.
    Each value is in the subject or the body exactly as the LLM gave it (spaces at its ends and runs of spaces aside):
    the LLM copies, and a value it rewrote in any way is refused.
    tracking_number: the whole of it matches `number_form`, a regular expression. When no link of the message holds
    it: refused with `number_in_link: true`, stated in `note` with false.
    date: read by code (see _read_date); returned written year-month-day. `note` says when the date was read in the
    order that is not `date_order`, and when the day was counted from a word. When the date is optional and absent,
    the result's date is the day the message was received, and `note` says so.
    from_time, to_time, delivered_time: a time of day as printed (1:15 PM, 5pm, 14:00, noon); returned as "HH:MM".
    The result also has `sender`, the domain of the sender's address (ups.com: read by code from the address the
    message is from, never by an LLM), and `source`: the sender's address, the day received and the mail server's id
    of the message, by which the message is found again. No text a sender wrote freely (a subject, a name) is in
    the result: the shipper's name goes through `string.clean` and stays untrusted.
    A check of form, presence and nearness: it does not show a value is the right one. So the result stays UNTRUSTED
    and gains CHECKED: an exception that names CHECKED says where checked values may go."""
    if date_order not in ORDERS:
        raise ValueError(f"date_order is {' or '.join(ORDERS)}")
    text = _squeezed(message.get("subject", "") + "\n" + message.get("body", ""))
    received = datetime.date.fromisoformat(message["received_day"])
    checked, notes = {}, []

    def one(name):
        """The value `name` as it is used, or a Problem saying why it is not."""
        value = values.get(name)
        if not isinstance(value, str) or not value.strip():
            raise chaperone.ModuleError("bad_values", f"the LLM gave no {name}")
        if _squeezed(value) not in text:
            raise chaperone.ModuleError("bad_values", f"the {name} is not in the message exactly as the LLM gave it")
        value = value.strip()
        if name == "tracking_number":
            if not re.fullmatch(number_form, value):
                raise chaperone.ModuleError("bad_values", "the tracking number has not the form number_form states")
            if not any(value in link for link in message.get("links", [])):
                if number_in_link:
                    raise chaperone.ModuleError("bad_values", "no link of the message holds the tracking number")
                notes.append("No link of the message holds the tracking number.")
            return value
        if name == "date":
            printed = _squeezed(value)
            day, about = _read_date(printed, date_order, received, days_before, days_after, _weekday_before(printed, text), day_words)
            notes.extend([about] if about else [])
            return day
        time = _clock(value)
        if time is None:
            raise chaperone.ModuleError("bad_values", f"the {name} is not a time of day this check reads")
        return time

    for name in required:
        try:
            checked[name] = one(name)
        except chaperone.ModuleError as problem:
            raise chaperone.ModuleError(problem.type, problem.message, {"llm_gave": values, "received_day": message["received_day"],
                                                                    "links": len(message.get("links", []))}) from None
    for name in optional:
        if values.get(name) is None:                         # the message states none: nothing to say
            continue
        try:
            checked[name] = one(name)
        except chaperone.ModuleError as problem:
            notes.append(f"What the LLM gave as {name} was not used: {problem.message}.")
    if "date" not in checked:
        checked["date"] = received.isoformat()
        notes.append("The day is the day the message was received.")
    checked["received"] = [message["received_day"], message["received_time"]]     # when the message was received, in the user's zone
    checked["note"] = " ".join(notes)
    sender = message.get("from", "")
    checked["sender"] = sender.rsplit("@", 1)[-1] if "@" in sender else "unknown sender"     # the sender's domain: ups.com
    checked["source"] = f"From {sender or 'an unknown sender'}, received {message['received_day']} {message['received_time']}, message {message['id']}."
    return checked, [(WHOLE, "CHECKED", True), (WHOLE, "LLM_WRITTEN", False)]      # checked, and still UNTRUSTED: the values are a sender's


CLEANERS = {   # source_type -> what is removed from a text of that type, in this order: (a regular expression, what replaces it)
    "html": [(r"<[^>]*>", " "), (r"[<>]", "")],              # tags, then every < and > left: nothing that remains can begin a tag
}


URLS = r"(?i)\b[a-z][a-z0-9+.-]*://\S+|\bwww\.\S+|\b[\w-]+(\.[\w-]+)+/\S*"    # https://x.example/a, www.x.example, x.example/a; not x.example alone


OPTIONS = {"trim": True, "strip_urls": False}                # the options of string.clean, each with the value it has when not given


@chaperone.module("string.clean", properties={"DESTRUCTIVE": False, "OUTPUT": False}, requests={"SANITIZED": True})
def string_clean(value, source_type, max_chars, options):
    """A text, cleaned as a text of `source_type`: `text`, and `note`, which says what was done. For any text.
    `source_type` says what the text is treated as, whatever it is: `html` reads character references (&lt; is <),
    then removes tags and every < and > left. For every type, characters that are not printed are removed and a run
    of spaces becomes one space. `options` (see OPTIONS for each one's value when it is not given):
      strip_urls  true: web addresses are removed (https://x.example/a, www.x.example, x.example/a). A name with a
                  dot and no slash (x.example) is not taken for one, and stays.
      trim        true: a cleaned text longer than `max_chars` is cut to that length, and "..." is added.
                  false: it is the error `too_long`.
    The text is "" when `value` is no text.
    This is cleaning, not a check: it says nothing about what the text says, and the text keeps every label it had.
    The result carries the label SANITIZED, which an exception may name to let it through where a rule forbids it."""
    if source_type not in CLEANERS:
        raise ValueError(f"source_type is one of: {', '.join(CLEANERS)}")
    if set(options) - set(OPTIONS) or not all(isinstance(chosen, bool) for chosen in options.values()):
        raise ValueError(f"options are true or false, of: {', '.join(OPTIONS)}")
    options = {**OPTIONS, **options}
    if not isinstance(value, str) or not value.strip():
        return {"text": "", "note": ""}, [(WHOLE, "SANITIZED", True)]
    cleaned = html.unescape(value) if source_type == "html" else value
    for pattern, replacement in CLEANERS[source_type] + ([(URLS, " ")] if options["strip_urls"] else []):
        cleaned = re.sub(pattern, replacement, cleaned)
    cleaned = _squeezed("".join(character if character.isprintable() else " " for character in cleaned))
    notes = [] if cleaned == _squeezed(value) else [f"Characters were removed from a text, cleaned as {source_type}."]
    if len(cleaned) > max_chars:
        if not options["trim"]:
            raise chaperone.ModuleError("too_long", f"a cleaned text is longer than {max_chars} characters")
        cleaned = cleaned[:max_chars].rstrip() + "..."
        notes.append(f"A text was cut to {max_chars} characters.")
    return {"text": cleaned, "note": " ".join(notes)}, [(WHOLE, "SANITIZED", True)]


URL_CHARS = r"[A-Za-z0-9._~:/?#\[\]@!$&'()*+,;=%-]+"        # the characters a web address may hold (RFC 3986), and no other


AWAITS = "A tracking link for this sender awaits approval."


NOT_WRITTEN = "The message's tracking link was not written: "


def _domain(message):
    """The domain of the address the message is from (ups.com), or "" when it names none."""
    sender = message.get("from", "")
    return sender.rsplit("@", 1)[-1] if "@" in sender else ""


def _split(link, max_chars):
    """A link in the three parts a rule states, or why it is not read. The length is tested before anything is split.
        https://www.ups.com/stuff/tracking?a=1&trk=12345678#top
        site        https://www.ups.com      the kind of link, the host, and a login or a port when the link has one
        path        /stuff/tracking          from the first single slash to the ?
        parameters  [(a, 1), (trk, 12345678)]
    Returns (site, path, parameters, "") or (None, None, None, why)."""
    if len(link) > max_chars:
        return None, None, None, f"it is longer than {max_chars} characters."
    if not re.fullmatch(URL_CHARS, link):
        return None, None, None, "it holds a character a web address may not hold."
    try:
        parts = urlsplit(link)
    except ValueError:
        return None, None, None, "it is not a web address."
    return f"{parts.scheme}://{parts.netloc.lower()}", parts.path, parse_qsl(parts.query, keep_blank_values=True), ""


@chaperone.module("links.derive", properties={"DESTRUCTIVE": False, "OUTPUT": False})
def links_derive(message, number, max_chars):
    """The link rules of the message: one for each link that is read (see _split) and has a parameter whose value is
    the tracking number. A rule is four texts: {sender, site, path, parameter}. `sender` is the domain of the address
    the message is from; the other three are the link's (see _split), `parameter` the name of the one that holds the
    number. The link's other parameters are not in the rule, so links that differ only in those fit one rule."""
    rules = []
    for link in message.get("links", []):
        site, path, parameters, why = _split(link, max_chars)
        name = next((name for name, value in parameters or [] if value == number), None)
        rule = {"sender": _domain(message), "site": site, "path": path, "parameter": name}
        if name and rule["sender"] and rule not in rules:
            rules.append(rule)
    return {"rules": rules}


@chaperone.module("links.not_approved", properties={"DESTRUCTIVE": False, "OUTPUT": False})
def links_not_approved(rules, approved, declined):
    """The rules of `rules` that are in neither `approved` nor `declined`: those the user is still to be asked about."""
    return [rule for rule in rules if rule not in approved and rule not in declined]


@chaperone.module("links.fill", properties={"DESTRUCTIVE": False, "OUTPUT": False}, requests={"UNTRUSTED": False})
def links_fill(message, approved, number, max_chars):
    """The tracking link of a message: the first of its links that holds the checked tracking number and passes every
    step below, written whole; no link otherwise, and `note` says at which step the link that got furthest stopped
    (for a site in no approved rule, when a rule read from the link awaits the user: only that one awaits approval).
    The steps, cheapest first, against the rules the user approved for the message's sender:
      1. the link is read (see _split): not too long, only the characters of a web address;
      2. its site is a rule's site;
      3. its path is the path of a rule with that site;
      4. the parameter such a rule names is in the link, and its value is the tracking number.
    No step tests for https, a login or a port: each is in the site, which must be one the user approved.
    A rule is a filter: the link's other parameters are text the sender chose. They are in the link written, and
    nowhere else."""
    rules = [rule for rule in approved if rule.get("sender") == _domain(message)]
    furthest = (0, "")
    for link in message.get("links", []):
        if number not in link:
            continue
        site, path, parameters, why = _split(link, max_chars)
        if why:
            stopped = (1, why)
        elif not any(rule.get("site") == site for rule in rules):
            stopped = (2, "its site is in no rule approved for this sender.")
        elif not any(rule.get("site") == site and rule.get("path") == path for rule in rules):
            stopped = (3, "its site is approved, its path is not.")
        elif not any(rule.get("site") == site and rule.get("path") == path and (rule.get("parameter"), number) in parameters for rule in rules):
            stopped = (4, "its site and its path are approved, but no approved parameter holds the tracking number.")
        else:
            return {"link": link, "note": ""}, [(WHOLE, "UNTRUSTED", False)]
        stopped = (stopped[0], NOT_WRITTEN + stopped[1])
        if stopped[0] > 1 and any(value == number for _name, value in parameters):    # links.derive read a rule from this link: the user is asked
            stopped = (stopped[0], AWAITS if stopped[0] == 2 else stopped[1] + " " + AWAITS)    # a site not yet approved: the usual case, said shortly
        furthest = max(furthest, stopped)
    return {"link": "", "note": furthest[1]}, [(WHOLE, "UNTRUSTED", False)]
