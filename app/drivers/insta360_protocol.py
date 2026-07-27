"""Wire protocol for Insta360 cameras that expose the binary API on TCP 6666.

Framing, the sync handshake and the message header follow the publicly
documented format.  Bodies are protobuf, but the camera only needs a handful of
fields from us, so this module carries a minimal wire-format codec instead of a
generated protobuf runtime — that keeps the container image free of extra
dependencies.

Reference: the packet layout is public (length-prefixed frames, ``syNceNdinS``
sync magic, 12-byte message header).  Everything here was verified against a
real device before being relied on.
"""

import socket
import struct
import threading
import time

TYPE_STREAM = 0x01
TYPE_MESSAGE = 0x04
TYPE_KEEPALIVE = 0x05
TYPE_SYNC = 0x06

SYNC_MAGIC = b'syNceNdinS'
DEFAULT_PORT = 6666
KEEPALIVE_SECONDS = 2.0
#: The camera hangs up after roughly ten seconds of silence.
SILENCE_LIMIT_SECONDS = 10.0

RESPONSE_OK = 200

WIRE_VARINT, WIRE_64, WIRE_BYTES, WIRE_32 = 0, 1, 2, 5


class ProtocolError(Exception):
    pass


# --------------------------------------------------------------- protobuf

def encode_varint(value):
    out = bytearray()
    while True:
        byte = value & 0x7F
        value >>= 7
        out.append(byte | (0x80 if value else 0))
        if not value:
            return bytes(out)


def decode_varint(data, index):
    result = shift = 0
    while index < len(data):
        byte = data[index]
        index += 1
        result |= (byte & 0x7F) << shift
        if not byte & 0x80:
            return result, index
        shift += 7
        if shift > 63:
            break
    raise ProtocolError('malformed varint')


def varint_field(number, value):
    return encode_varint(number << 3 | WIRE_VARINT) + encode_varint(value)


def repeated_varint_field(number, values):
    return b''.join(varint_field(number, value) for value in values)


def parse_fields(data):
    """Return ``[(field_number, wire_type, value)]`` for one protobuf message.

    Values are ints for varints and bytes for length-delimited fields; the
    caller decides how to interpret them.
    """
    fields = []
    index = 0
    while index < len(data):
        tag, index = decode_varint(data, index)
        number, wire = tag >> 3, tag & 7
        if number == 0:
            raise ProtocolError('field number 0')
        if wire == WIRE_VARINT:
            value, index = decode_varint(data, index)
        elif wire == WIRE_BYTES:
            length, index = decode_varint(data, index)
            value, index = data[index:index + length], index + length
            if len(value) != length:
                raise ProtocolError('truncated length-delimited field')
        elif wire == WIRE_64:
            value, index = data[index:index + 8], index + 8
        elif wire == WIRE_32:
            value, index = data[index:index + 4], index + 4
        else:
            raise ProtocolError('unsupported wire type %d' % wire)
        fields.append((number, wire, value))
    return fields


def field_values(fields, number, wire=None):
    return [value for num, wire_type, value in fields
            if num == number and (wire is None or wire_type == wire)]


def first_value(fields, number, default=None, wire=None):
    values = field_values(fields, number, wire)
    return values[0] if values else default


def text_of(raw):
    try:
        return raw.decode('utf-8').rstrip('\x00')
    except (UnicodeDecodeError, AttributeError):
        return None


# ----------------------------------------------------------------- framing

def frame(payload):
    """Length-prefixed frame; the length counts the four prefix bytes too."""
    return struct.pack('<I', 4 + len(payload)) + payload


def sync_payload():
    return bytes([TYPE_SYNC, 0, 0]) + SYNC_MAGIC


def keepalive_payload():
    return bytes([TYPE_KEEPALIVE, 0, 0])


def command_payload(code, sequence, body=b''):
    return (bytes([TYPE_MESSAGE, 0, 0])
            + struct.pack('<H', code)
            + bytes([0x02])
            + struct.pack('<I', sequence)[:3]
            + bytes([0x80, 0, 0])
            + body)


class Response:
    __slots__ = ('code', 'sequence', 'body')

    def __init__(self, code, sequence, body):
        self.code = code
        self.sequence = sequence
        self.body = body

    @property
    def ok(self):
        return self.code == RESPONSE_OK

    def fields(self):
        return parse_fields(self.body) if self.body else []


def parse_packet(payload):
    kind = payload[0] if payload else None
    if kind == TYPE_MESSAGE and len(payload) >= 12:
        return kind, Response(struct.unpack('<H', payload[3:5])[0],
                              int.from_bytes(payload[6:9], 'little'), payload[12:])
    return kind, None


class Insta360Session:
    """One live connection: sync handshake, keep-alives and request/response."""

    def __init__(self, host, port=DEFAULT_PORT, timeout=8.0):
        self.host = host
        self.port = port
        self.timeout = timeout
        self._sock = None
        self._sequence = 0
        self._send_lock = threading.Lock()
        self._stop = threading.Event()
        self._keeper = None

    # -- lifecycle ---------------------------------------------------------

    def open(self):
        if self._sock is not None:
            return
        sock = socket.create_connection((self.host, self.port), timeout=self.timeout)
        sock.settimeout(self.timeout)
        self._sock = sock
        self._stop.clear()
        try:
            self._send(sync_payload())
            self._await_sync()
        except Exception:
            self.close()
            raise
        self._keeper = threading.Thread(target=self._keepalive_loop, daemon=True)
        self._keeper.start()

    def close(self):
        self._stop.set()
        sock, self._sock = self._sock, None
        if sock is not None:
            try:
                sock.close()
            except OSError:
                pass
        self._keeper = None

    def __enter__(self):
        self.open()
        return self

    def __exit__(self, *_):
        self.close()

    # -- plumbing ----------------------------------------------------------

    def _send(self, payload):
        with self._send_lock:
            if self._sock is None:
                raise ProtocolError('session is closed')
            self._sock.sendall(frame(payload))

    def _recv_exact(self, count):
        chunks = b''
        while len(chunks) < count:
            if self._sock is None:
                raise ProtocolError('session is closed')
            piece = self._sock.recv(count - len(chunks))
            if not piece:
                # A transport failure, not a protocol mismatch: callers should
                # tell the user the camera stopped answering.
                raise ConnectionError('camera closed the connection')
            chunks += piece
        return chunks

    def _read_packet(self):
        total = struct.unpack('<I', self._recv_exact(4))[0]
        if total < 4 or total > 32 * 1024 * 1024:
            raise ProtocolError('implausible frame length %d' % total)
        return self._recv_exact(total - 4)

    def _await_sync(self):
        """Wait for the camera to echo our sync packet.

        A camera that is asleep still completes the TCP handshake but then says
        nothing, so silence is reported as a connection problem; only a wrong
        echo means we are talking to something that is not this protocol.
        """
        deadline = time.monotonic() + self.timeout
        while time.monotonic() < deadline:
            self._sock.settimeout(max(0.2, deadline - time.monotonic()))
            try:
                payload = self._read_packet()
            except socket.timeout:
                break
            if payload and payload[0] == TYPE_SYNC:
                if payload[3:] == SYNC_MAGIC:
                    return True
                raise ProtocolError('camera echoed an unexpected sync packet')
        raise ConnectionError('camera did not answer the sync handshake')

    def _keepalive_loop(self):
        while not self._stop.wait(KEEPALIVE_SECONDS):
            try:
                self._send(keepalive_payload())
            except (OSError, ProtocolError):
                return

    # -- requests ----------------------------------------------------------

    def command(self, code, body=b'', timeout=None):
        """Send one command and return the response with the matching sequence."""
        timeout = timeout or self.timeout
        self._sequence += 1
        sequence = self._sequence
        self._send(command_payload(code, sequence, body))
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self._sock is None:
                raise ProtocolError('session is closed')
            self._sock.settimeout(max(0.2, deadline - time.monotonic()))
            try:
                payload = self._read_packet()
            except socket.timeout:
                break
            kind, response = parse_packet(payload)
            # Keep-alives, stream data and notifications share this connection.
            if kind == TYPE_MESSAGE and response is not None and response.sequence == sequence:
                return response
        raise ConnectionError('camera stopped answering command %d' % code)
