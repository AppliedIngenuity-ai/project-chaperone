"""The modules that send mail with the tool MAILBOX go here, each named `mail.send.<name>`.

None is written: this project sends no mail. The setup's line `mail.send.*` states OUTPUT true for every module
written here, so a rule that selects modules whose OUTPUT is true selects it. Until one is written, the report of
the pre-run check warns that no module matches the line. The reading modules are in mail_read.py, under the line `mail.read.*`,
which states OUTPUT false.
"""
