"""The modules that write on the calendar (tool GOOGLE_CALENDAR, or a calendar file)."""

import datetime

import chaperone


def _plus(time, minutes):
    """`time` ("HH:MM") moved by `minutes`, kept within its day."""
    total = min(max(int(time[:2]) * 60 + int(time[3:]) + minutes, 0), 23 * 60 + 59)
    return f"{total // 60:02d}:{total % 60:02d}"


SHIPPED_FROM = "Shipped from: "                              # the line of an entry's description that names the shipper


NOT_CHECKED = " (as the LLM read it in the sender's message; not checked)"


def _shipper(ctx, entry, shipper):
    """(the shipper, a note). `shipper` is the result of `string.clean` for the name the LLM copied. When it has no
    text: the shipper on the entry this system made earlier for the same parcel, and the note says so."""
    if shipper["text"]:
        return shipper["text"], shipper["note"]
    made = ctx.entries().get(entry["tracking_number"]) or {}
    for line in made.get("description", "").split("\n"):
        if line.startswith(SHIPPED_FROM):
            return line[len(SHIPPED_FROM):].removesuffix(NOT_CHECKED), "This message names no shipper: the shipper is the one an earlier message named."
    return "", shipper["note"]


def _titled(title, entry, shipper):
    """An entry's title: Delivery (ups.com) shipped from Example Outfitters, 1Z999AA10123456784."""
    return f"{title} ({entry['sender']}) " + (f"shipped from {shipper}, " if shipper else "") + entry["tracking_number"]


SUBJECT = "Subject: "                                        # the line of an entry's description that has the message's subject


SUBJECT_NOT_CHECKED = " (as the sender wrote it; cleaned, not checked)"


def _described(entry, about, link, shipper, subject):
    """An entry's description: the shipper, the tracking link, what was noted, and where the message is found: its
    subject (the result of `string.clean`: the sender's text, cleaned and not checked), its sender, when it was
    received and the mail server's id of it."""
    noted = " ".join(part for part in (entry.get("note", ""), about, link.get("note", ""), subject["note"]) if part)
    return "\n".join(line for line in (SHIPPED_FROM + shipper + NOT_CHECKED if shipper else "", f"Track: {link['link']}" if link.get("link") else "",
                                       noted, SUBJECT + subject["text"] + SUBJECT_NOT_CHECKED if subject["text"] else "",
                                       entry.get("source", "")) if line)


@chaperone.module("calendar.write_entry", properties={"DESTRUCTIVE": True})
def calendar_write_entry(ctx, entry, link, shipper, subject, title, no_time, start_if_only_end, end_if_only_start):
    """Creates the calendar entry of a parcel, or updates the one made earlier for the same tracking number. Its
    hours are the earliest and the latest time the message gives. With only a latest time it starts at
    `start_if_only_end`, with only an earliest it ends at `end_if_only_start` (an hour before or after, when that
    setting is on the wrong side of the time given); with no time it is `no_time`. The description says which.
    The title and the description name the shipper, cleaned and not checked (see _shipper)."""
    number, day, first, last = entry["tracking_number"], entry["date"], entry.get("from_time"), entry.get("to_time")
    if first and last and first < last:
        start, end, about = first, last, ""
    elif last and not (first and last and first < last) and (not first or first >= last):
        start = start_if_only_end if start_if_only_end < last else _plus(last, -60)
        end, about = last, f"The message gives only a latest time, {last}; the entry starts at {start}."
    elif first:
        end = end_if_only_start if end_if_only_start > first else _plus(first, 60)
        start, about = first, f"The message gives only an earliest time, {first}; the entry ends at {end}."
    else:
        (start, end), about = no_time, f"The message gives no time: {no_time[0]} to {no_time[1]} is not a delivery time."
    shipper, kept = _shipper(ctx, entry, shipper)
    ctx.upsert(number, title=_titled(title, entry, shipper), start=f"{day}T{start}:00", end=f"{day}T{end}:00",
               description=_described(entry, " ".join(part for part in (about, kept) if part), link, shipper, subject))


@chaperone.module("calendar.mark_delivered", properties={"DESTRUCTIVE": True})
def calendar_mark_delivered(ctx, entry, link, shipper, subject, title, minutes, minutes_before_message, no_time):
    """Marks the parcel delivered: its entry, changed, or a new one when this system made none. With the time the
    message gives, the entry is that time and `minutes` after it, on the day delivered. With no time, the message is
    taken to have been sent shortly after the delivery: the entry ends when the message was received and starts
    `minutes_before_message` before. With no time and a day that is not the day the message was received, the hours
    are `no_time`. The description says which. The title and the description name the shipper, cleaned and not checked (see _shipper)."""
    number, day, at = entry["tracking_number"], entry["date"], entry.get("delivered_time")
    received_day, received_time = entry["received"]
    if at:
        start, end, about = at, _plus(at, minutes), f"Delivered at {at}."
    elif day == received_day:
        start, end = _plus(received_time, -minutes_before_message), received_time
        about = f"The message gives no time of delivery: the entry ends when the message was received, {received_time}."
    else:
        (start, end), about = no_time, f"The message gives no time of delivery: {no_time[0]} to {no_time[1]} is not the time."
    if start == end:                                         # a message received at midnight: one minute, so that the entry has a length
        end = _plus(end, 1)
    shipper, kept = _shipper(ctx, entry, shipper)
    ctx.upsert(number, title=_titled(title, entry, shipper), start=f"{day}T{start}:00", end=f"{day}T{end}:00",
               description=_described(entry, " ".join(part for part in (about, kept) if part), link, shipper, subject))


@chaperone.module("calendar.flag_unconfirmed", properties={"DESTRUCTIVE": True})
def calendar_flag_unconfirmed(ctx, today, scheduled_title, title, days, note):
    """Changes each entry of a delivery to come whose day is more than `days` days before `today`: no notice said the
    parcel was delivered. Its title's first words, `scheduled_title`, become `title`, and `note` becomes the first
    line of its description. It states what was received, not what happened to the parcel. Returns how many."""
    limit = datetime.date.fromisoformat(today) - datetime.timedelta(days=days)
    changed = 0
    for key, made in ctx.entries().items():
        if made["title"].startswith(scheduled_title + " ") and datetime.date.fromisoformat(made["end"][:10]) < limit:
            ctx.upsert(key, title=title + made["title"][len(scheduled_title):], start=made["start"], end=made["end"],
                       description=note + "\n" + made.get("description", ""))
            changed += 1
    return {"changed": changed}


@chaperone.module("calendar.remove_entries", properties={"DESTRUCTIVE": True})
def calendar_remove_entries(ctx):
    """Removes every calendar entry this system made. Returns how many."""
    keys = list(ctx.entries())
    for key in keys:
        ctx.remove(key)
    return {"removed": len(keys)}
