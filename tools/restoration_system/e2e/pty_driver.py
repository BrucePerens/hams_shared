# SPDX-License-Identifier: AGPL-3.0-or-later
"""Runs a command on a pseudo-terminal and types answers when it asks: the production path for passphrases (the terminal), not a test hook."""
import os
import pty
import select
import time


def run_on_tty(argv, answers, timeout=3600, log=None):
    """Run argv with a controlling terminal. `answers` is [(prompt substring, text)]: each text is typed once when its prompt appears.

    Returns (exit status, output text). The typed answers are not echoed by the program (it reads them without echo)."""
    pid, fd = pty.fork()
    if pid == 0:
        os.execvp(argv[0], argv)
    output, pending, deadline = b"", list(answers), time.time() + timeout
    while True:
        if time.time() > deadline:
            os.kill(pid, 15)
            raise TimeoutError("command did not finish")
        ready, _, _ = select.select([fd], [], [], 1.0)
        if ready:
            try:
                chunk = os.read(fd, 65536)
            except OSError:
                chunk = b""
            if not chunk:
                break
            output += chunk
            if log:
                log.write(chunk.decode("utf-8", "replace"))
                log.flush()
            for item in list(pending):
                if item[0].encode() in output:
                    os.write(fd, (item[1] + "\n").encode())
                    pending.remove(item)
                    output = output.replace(item[0].encode(), b"<prompt answered>")
    _, status = os.waitpid(pid, 0)
    return os.waitstatus_to_exitcode(status), output.decode("utf-8", "replace")
