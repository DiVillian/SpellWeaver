"""Restarting the worldserver, using a command the user supplied.

Abilities are read at startup, so nearly every save ends with "now restart the
worldserver". That is a trip to a terminal for something the tool already has
every other piece of. It does not have one thing: how this particular server is
run. A source build, a systemd unit, a docker container and a Windows service
all restart differently, and guessing would be wrong more often than right.

So the command is the user's, stored on this machine like the client path is.

What is deliberately *not* here: any way for the page to say what to run. The
request carries no command, only the wish to run the configured one. A browser
that is pointed at this server, or a page that is tricked into posting to it,
can therefore do exactly what the person who set it up already chose to allow,
and nothing else.

The command runs through a shell because the realistic answers need one, from
`docker restart ac-worldserver` to `sudo systemctl restart worldserver` to a
Windows batch file. The shell is not a hole here: the string never comes from
outside, and anyone who can edit the settings file can already run anything as
that user.
"""
import subprocess
import time

# A restart that has not finished talking in this long is a restart that has
# detached, or hung. Either way the answer is the same: say what happened and
# stop waiting, rather than holding the request open.
TIMEOUT_SECONDS = 120

# Output is for a person to read in a panel, not a log file to scroll.
MAX_OUTPUT = 4000


class RestartError(Exception):
    """Written for the user to read."""


def configured(settings):
    return bool((settings.restart_command or "").strip())


def run(settings):
    """Run the configured command and report what it did.

    Returns a dict rather than raising for a non-zero exit: a server that
    refused to restart is a result to show, not an error in this tool.
    """
    command = (settings.restart_command or "").strip()
    if not command:
        raise RestartError(
            "No restart command set. Add one on the Settings page, for example "
            "the command you normally type to restart the worldserver.")

    started = time.time()
    try:
        done = subprocess.run(command, shell=True, capture_output=True,
                              text=True, timeout=TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired:
        return {
            "ok": False,
            "command": command,
            "seconds": round(time.time() - started, 1),
            "message": (
                "The command was still running after %d seconds, so it was left "
                "to finish on its own. If it starts the server in the "
                "foreground, use the form that returns straight away."
                % TIMEOUT_SECONDS),
            "output": "",
        }
    except OSError as exc:
        raise RestartError("Could not run the command: %s" % exc)

    output = _tail((done.stdout or "") + (done.stderr or ""))
    ok = done.returncode == 0
    return {
        "ok": ok,
        "command": command,
        "code": done.returncode,
        "seconds": round(time.time() - started, 1),
        "message": ("The restart command finished."
                    if ok else
                    "The command exited with code %d." % done.returncode),
        "output": output,
    }


def _tail(text):
    """The end of the output, which is where the reason usually is."""
    text = (text or "").strip()
    if len(text) <= MAX_OUTPUT:
        return text
    return "..." + text[-MAX_OUTPUT:]
