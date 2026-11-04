#!/usr/bin/env python3
"""
The tor image's one Tor control-port client.

C Tor in every container of this image listens on 127.0.0.1:9051 with
cookie authentication (entrypoint.sh's torrc). tor-watchdog.py, the
queue manager (onionheaven-queue-manager.py), the takeover worker and the
farm controller's worker verbs (onionheaven-farm.py) all talk to it
through Controller; nothing else in the image opens the port or reads
the cookie.

A reply is read whole, as control-spec.txt section 2.3 has it: "NNN-"
lines continue it, a "NNN+" line starts a data block that ends at a line
holding only ".", and the "NNN " line ends it. A reply cut short by the
timeout is returned as far as it came (the callers look for "250" and
treat anything else as a failure).

As a script, for the farm's worker verbs (output unchanged from the
shell and Python snippets they replace):

  tor_control.py getinfo <key>        the GETINFO reply, as Tor sent it
  tor_control.py onions onions/detached
                                      one <service id>.onion per line
"""

import socket
import sys

CONTROL_HOST = "127.0.0.1"
CONTROL_PORT = 9051
COOKIE_PATH = "/var/lib/tor/control_auth_cookie"
TIMEOUT = 10


class ControlClosed(ConnectionError):
    """Tor closed the control connection."""


def read_cookie(path=None):
    """The control auth cookie's bytes (OSError if it cannot be read)."""
    with open(path or COOKIE_PATH, "rb") as f:
        return f.read()


class Controller:
    """One control-port connection: commands and their replies, or events."""

    def __init__(self, sock):
        self.sock = sock
        self._buf = b""

    @classmethod
    def connect(cls, host=None, port=None, timeout=TIMEOUT, cookie=None,
                cookie_path=None):
        """A connection (to CONTROL_HOST:CONTROL_PORT by default),
        authenticated unless cookie is False. cookie is the cookie's bytes,
        or None to read cookie_path (COOKIE_PATH). Raises OSError (Tor not
        listening, no cookie) or ConnectionError (authentication refused)."""
        sock = socket.create_connection((host or CONTROL_HOST, port or CONTROL_PORT),
                                        timeout=timeout)
        ctl = cls(sock)
        try:
            if cookie is not False:
                if cookie is None:
                    cookie = read_cookie(cookie_path or COOKIE_PATH)
                ctl.authenticate(cookie)
        except BaseException:
            ctl.close()
            raise
        return ctl

    def authenticate(self, cookie):
        reply = self.command("AUTHENTICATE " + cookie.hex())
        if not reply.startswith("250"):
            raise ConnectionError(f"authentication refused: {reply.strip()[:200]}")

    def settimeout(self, timeout):
        self.sock.settimeout(timeout)

    def close(self):
        try:
            self.sock.close()
        except OSError:
            pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    # -- the wire -----------------------------------------------------------

    def send(self, line):
        """Send one command line (no CR or LF of its own: a value that
        carried one would start a second command)."""
        if "\r" in line or "\n" in line:
            raise ValueError("a control command is one line")
        self.sock.sendall(line.encode("utf-8") + b"\r\n")

    def _fill(self):
        chunk = self.sock.recv(4096)
        if not chunk:
            raise ControlClosed("control connection closed")
        self._buf += chunk

    def read_line(self):
        """The next line, without its CRLF. Raises socket.timeout when none
        arrives in time (what was read stays buffered) and ControlClosed at
        the end of the connection."""
        while b"\r\n" not in self._buf:
            self._fill()
        line, self._buf = self._buf.split(b"\r\n", 1)
        return line.decode("utf-8", errors="replace")

    def read_reply(self):
        """One whole reply, its lines joined with CRLF as Tor sent them.
        A timeout or a closed connection ends it early."""
        lines = []
        in_data = False
        try:
            while True:
                line = self.read_line()
                lines.append(line)
                if in_data:
                    in_data = line != "."
                    continue
                if len(line) >= 4 and line[:3].isdigit():
                    if line[3] == "+":
                        in_data = True
                    elif line[3] == " ":
                        break
        except (socket.timeout, ControlClosed):
            pass
        return "".join(line + "\r\n" for line in lines)

    def read_events(self):
        """The complete lines one receive brings (events on a SETEVENTS
        connection): at least one, raising socket.timeout or ControlClosed
        when none comes."""
        if b"\r\n" not in self._buf:
            self._fill()
        lines = []
        while b"\r\n" in self._buf:
            line, self._buf = self._buf.split(b"\r\n", 1)
            lines.append(line.decode("utf-8", errors="replace"))
        return lines

    def command(self, line):
        """Send a command and return its reply."""
        self.send(line)
        return self.read_reply()

    # -- commands ------------------------------------------------------------

    def getinfo(self, key):
        """GETINFO key's value (a data block's lines joined with "\\n"),
        or None when Tor answers anything but 250."""
        reply = self.command("GETINFO " + key)
        lines = reply.split("\r\n")
        if not lines or not lines[0].startswith("250"):
            return None
        first = lines[0][4:]
        if not first.startswith(key + "="):
            return None
        value = first[len(key) + 1:]
        if lines[0][3] != "+":
            return value
        data = []
        for line in lines[1:]:
            if line == ".":
                break
            data.append(line[1:] if line.startswith("..") else line)
        return "\n".join(data)

    def onions(self, kinds=("onions/detached", "onions/current")):
        """The service ids (no .onion) Tor has for the GETINFO keys in kinds."""
        sids = set()
        for kind in kinds:
            value = self.getinfo(kind)
            if value:
                sids.update(v.strip() for v in value.split("\n") if v.strip())
        return sids

    def add_onion(self, key, ports, flags=("Detach",)):
        """ADD_ONION key (e.g. "ED25519-V3:<base64>" or "NEW:BEST") with one
        Port= per entry of ports. Returns (reply, service id or None)."""
        cmd = "ADD_ONION " + key
        if flags:
            cmd += " Flags=" + ",".join(flags)
        for port in ports:
            cmd += " Port=" + port
        reply = self.command(cmd)
        for line in reply.split("\r\n"):
            if line.startswith("250-ServiceID="):
                return reply, line.split("=", 1)[1]
        return reply, None

    def del_onion(self, service_id):
        """DEL_ONION service_id (the address without ".onion").
        (removed?, reply)."""
        reply = self.command("DEL_ONION " + service_id)
        return reply.startswith("250"), reply


def main(argv):
    if len(argv) == 3 and argv[1] == "getinfo":
        with Controller.connect() as ctl:
            sys.stdout.write(ctl.command("GETINFO " + argv[2]))
        return 0
    if len(argv) == 3 and argv[1] == "onions":
        with Controller.connect() as ctl:
            for sid in sorted(ctl.onions((argv[2],))):
                print(sid + ".onion")
        return 0
    sys.stderr.write("usage: tor_control.py getinfo <key> | onions <getinfo key>\n")
    return 2


if __name__ == "__main__":
    try:
        sys.exit(main(sys.argv))
    except (OSError, ValueError) as e:
        sys.stderr.write(f"tor_control: {e}\n")
        sys.exit(1)
