# Your private settings: your mail login and, for a hosted LLM, its key. Git never commits the folder private/.
#
# 1. Copy this file once (a private/env.sh you already have is left as it is):
#      mkdir -p private && [ -e private/env.sh ] || cp private.example/env.sh private/env.sh
# 2. In private/env.sh, fill in the lines marked FILL IN, in the sections you use.
# 3. In each new terminal, before running:  source private/env.sh
#
# The program reads the variables this file sets; it never reads the file.
# The Google Calendar files have no template: README.md, "Step 3: Google Calendar".


# ---- Mail: your login ----

export CHAPERONE_MAIL_USER=""          # FILL IN: your address
export CHAPERONE_MAIL_PASSWORD=""      # FILL IN: Gmail: the app password, its 16 letters without spaces.
                                       #          Proton Mail: the password the bridge shows, not your account's.


# ---- Mail: your server. Gmail is set. ----

export CHAPERONE_MAIL_HOST=imap.gmail.com
export CHAPERONE_MAIL_PORT=993
export CHAPERONE_MAIL_SSL=1
export CHAPERONE_MAIL_STARTTLS=0

# Proton Mail Bridge, on this machine: remove the # at the start of the five lines below.
# They come after the Gmail lines, so they replace them.
# export CHAPERONE_MAIL_HOST=127.0.0.1
# export CHAPERONE_MAIL_PORT=1143        # the IMAP port the bridge's window shows
# export CHAPERONE_MAIL_SSL=0
# export CHAPERONE_MAIL_STARTTLS=1
# export CHAPERONE_MAIL_TRUST_LOCAL=1    # the bridge's own certificate is not checked: the bridge is on this machine
#
#   Safer, in place of that last line: export the bridge's certificate to a file, and set
#   export CHAPERONE_MAIL_CERT="$PWD/private/bridge-cert.pem"


# ---- A hosted LLM (examples/safe_rag, setup hosted): remove the # and put in your key ----

# export ANTHROPIC_API_KEY=""          # FILL IN: your key, from console.anthropic.com
