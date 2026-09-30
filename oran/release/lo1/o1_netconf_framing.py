"""RFC 6242 message framing for the production NETCONF-over-SSH consumer.

``o1-netconf-yang-profile.1.0.0.json#/transport`` declares
``NETCONF_1_1_OVER_SSH`` and requires ``urn:ietf:params:netconf:base:1.1``.
RFC 6242 §4.1 and §4.2 then fix the framing exactly:

* the ``<hello>`` exchange is framed with the end-of-message sentinel
  ``]]>]]>`` -- it has to be, because the framing for everything after it is
  what the hello is *negotiating*;
* the moment **both** peers have advertised ``base:1.1``, every subsequent
  message -- every ``<rpc>`` and every ``<rpc-reply>`` -- is framed with the
  chunked encoding, and the end-of-message sentinel must never appear again.

This module is the client half and **only** the client half.  The self-test
Provider emulator implements the same RFC from scratch, in its own module, with
its own parser structure and its own reason-code namespace
(``oran/release/lo1_selftest/ssh_provider.py``).  That duplication is
deliberate and is the correction for the defect that withdrew 1.0.1: the
emulator had mirrored the client's non-conformant framing, so the two agreed
with each other and the self-test could not see that both were wrong.  A shared
codec would restore exactly that blind spot, so nothing here may be imported by
the self-test package and nothing here may import it.

Everything below is fail-closed.  A stream that does not decode to a
well-formed chunked message raises rather than resynchronising, and each
refusal carries one of the reason codes in :data:`FRAMING_REASON_CODES`:

===================  =========================================================
``LO1-CFRAME-001``   the chunk header is not ``LF '#' chunk-size LF``
``LO1-CFRAME-002``   the chunk size carries a leading zero
``LO1-CFRAME-003``   the peer ended the stream inside a chunk
``LO1-CFRAME-004``   the peer ended the stream without ``LF '#' '#' LF``
``LO1-CFRAME-005``   the peer used ``]]>]]>`` after the hello exchange
``LO1-CFRAME-006``   a chunk or a message exceeded the declared frame limit
``LO1-CFRAME-007``   an empty payload cannot be framed (``chunk-data`` is 1*OCTET)
``LO1-CFRAME-008``   the bound transport backend cannot honour chunked framing
===================  =========================================================
"""

from __future__ import annotations

from collections import deque
from typing import Iterable, Sequence

#: RFC 6241 §8.1 base capabilities.  ``base:1.1`` on both sides is what selects
#: the chunked encoding; the strings are compared, never re-spelled elsewhere.
NETCONF_BASE_1_0_CAPABILITY = "urn:ietf:params:netconf:base:1.0"
NETCONF_BASE_1_1_CAPABILITY = "urn:ietf:params:netconf:base:1.1"

#: The two framings RFC 6242 defines, named rather than spelled as booleans.
FRAMING_END_OF_MESSAGE = "END_OF_MESSAGE"
FRAMING_CHUNKED = "CHUNKED"

#: RFC 6242 §4.3.  Legal for the hello exchange and illegal after it.
END_OF_MESSAGE_SENTINEL = b"]]>]]>"

_LF = 0x0A
_HASH = 0x23
_ZERO = 0x30
_NINE = 0x39

#: RFC 6242 §4.2: ``chunk-size`` counts the octets of one ``chunk-data`` and
#: cannot exceed 4294967295, so a size field is at most ten digits long.
MAX_CHUNK_OCTETS = 4294967295
MAX_CHUNK_SIZE_DIGITS = 10

#: The consumer's own ceiling on one reassembled message.  A Provider that
#: announces more than this is refused rather than allocated for.
DEFAULT_MAX_MESSAGE_OCTETS = 4 * 1024 * 1024

#: Sender-side fragmentation.  A message longer than this is emitted as several
#: chunks, which is legal for any message and is what makes the client's own
#: multi-chunk path a normal path rather than an untested one.
DEFAULT_SEND_CHUNK_OCTETS = 65536

#: How far the decoder reads before it decides that a non-``LF`` first octet is
#: a stray end-of-message sentinel rather than an unclassifiable header.  The
#: refusal happens either way; only the reason code depends on this.
_EOM_CLASSIFICATION_OCTETS = 8192

FRAMING_REASON_CODES: dict[str, str] = {
    "LO1-CFRAME-001": "MALFORMED_CHUNK_SIZE",
    "LO1-CFRAME-002": "LEADING_ZERO_CHUNK_SIZE",
    "LO1-CFRAME-003": "PREMATURE_END_OF_STREAM",
    "LO1-CFRAME-004": "MISSING_END_OF_CHUNKS",
    "LO1-CFRAME-005": "END_OF_MESSAGE_FRAMING_AFTER_HELLO",
    "LO1-CFRAME-006": "FRAME_SIZE_LIMIT_EXCEEDED",
    "LO1-CFRAME-007": "EMPTY_MESSAGE_NOT_FRAMEABLE",
    "LO1-CFRAME-008": "BACKEND_CANNOT_HONOUR_NEGOTIATED_FRAMING",
}


class NetconfTransportError(RuntimeError):
    """The NETCONF transport could not be opened or trusted.

    Defined here rather than in the lifecycle module because the framing layer
    has to raise it and the lifecycle module imports this one; every existing
    importer still reads it from ``o1_netconf``, which re-exports it.
    """


class NetconfFramingError(NetconfTransportError):
    """A NETCONF octet stream violated RFC 6242.  Always fail-closed.

    Carries the reason code as an attribute so a caller records the refusal by
    identity instead of by matching on a message.
    """

    def __init__(self, reason_code: str, detail: str) -> None:
        if reason_code not in FRAMING_REASON_CODES:
            raise KeyError("undeclared framing reason code %s" % reason_code)
        self.reason_code = reason_code
        self.reason = FRAMING_REASON_CODES[reason_code]
        super().__init__("%s (%s): %s" % (reason_code, self.reason, detail))


def negotiate_framing(client_capabilities: Iterable[str],
                      server_capabilities: Iterable[str]) -> str:
    """RFC 6242 §4.1: chunked iff **both** peers advertised ``base:1.1``."""
    client = {str(item).strip() for item in client_capabilities}
    server = {str(item).strip() for item in server_capabilities}
    if (NETCONF_BASE_1_1_CAPABILITY in client
            and NETCONF_BASE_1_1_CAPABILITY in server):
        return FRAMING_CHUNKED
    return FRAMING_END_OF_MESSAGE


# ----------------------------------------------------------------- encoding


def encode_end_of_message(payload: bytes) -> bytes:
    """RFC 6242 §4.3 framing.  Legal for the hello exchange only."""
    return bytes(payload) + END_OF_MESSAGE_SENTINEL


def encode_chunked(payload: bytes, *,
                   chunk_octets: int = DEFAULT_SEND_CHUNK_OCTETS) -> bytes:
    """RFC 6242 §4.2 framing, fragmenting at ``chunk_octets``.

    ``chunk-data`` is ``1*OCTET``, so an empty payload has no legal encoding
    and is refused rather than emitted as a bare end-of-chunks.
    """
    body = bytes(payload)
    if not body:
        raise NetconfFramingError(
            "LO1-CFRAME-007",
            "a zero-length NETCONF message has no RFC 6242 chunked encoding")
    limit = int(chunk_octets)
    if limit < 1 or limit > MAX_CHUNK_OCTETS:
        raise NetconfFramingError(
            "LO1-CFRAME-006",
            "the sender's chunk size %d is outside 1..%d"
            % (limit, MAX_CHUNK_OCTETS))
    out = bytearray()
    for start in range(0, len(body), limit):
        piece = body[start:start + limit]
        out += b"\n#"
        out += str(len(piece)).encode("ascii")
        out += b"\n"
        out += piece
    out += b"\n##\n"
    return bytes(out)


def chunk_count(framed: bytes) -> int:
    """How many chunks one encoded message carries.  Used by evidence, not I/O."""
    return ChunkedMessageDecoder().count_chunks(bytes(framed))


# ----------------------------------------------------------------- decoding


class EndOfMessageDecoder:
    """The hello-phase decoder: split on ``]]>]]>``, keep the remainder.

    The remainder matters.  A Provider is free to put its hello and the first
    chunk-framed octets in one TCP segment, and the bytes past the sentinel
    belong to the chunked decoder that replaces this one.
    """

    def __init__(self, *,
                 max_message_octets: int = DEFAULT_MAX_MESSAGE_OCTETS) -> None:
        self.max_message_octets = int(max_message_octets)
        self._buffer = bytearray()
        self._ready: deque[bytes] = deque()
        #: Aligned with the list :meth:`drain` last returned.  An
        #: end-of-message frame is one frame, so every entry is 1; the
        #: attribute exists so a caller can read chunk counts uniformly across
        #: the framing switch.
        self.drained_frame_counts: list[int] = []

    def feed(self, data: bytes) -> list[bytes]:
        self._buffer += bytes(data)
        while True:
            index = self._buffer.find(END_OF_MESSAGE_SENTINEL)
            if index < 0:
                break
            self._ready.append(bytes(self._buffer[:index]))
            del self._buffer[:index + len(END_OF_MESSAGE_SENTINEL)]
        if len(self._buffer) > self.max_message_octets:
            raise NetconfFramingError(
                "LO1-CFRAME-006",
                "an unterminated end-of-message frame passed the consumer's "
                "%d octet limit" % self.max_message_octets)
        return self.drain()

    def drain(self) -> list[bytes]:
        found = list(self._ready)
        self._ready.clear()
        self.drained_frame_counts = [1] * len(found)
        return found

    @property
    def residue(self) -> bytes:
        """Octets received but not yet part of a complete framed message."""
        return bytes(self._buffer)

    def close(self) -> None:
        if self._buffer:
            raise NetconfFramingError(
                "LO1-CFRAME-003",
                "the peer ended the stream %d octets into an unterminated "
                "end-of-message frame" % len(self._buffer))


class ChunkedMessageDecoder:
    """Incremental RFC 6242 §4.2 decoder.

    Written as an explicit push state machine: octets are fed in whatever sizes
    the transport produced and the machine advances as far as it can.  That is
    what makes a partial receive (one octet at a time), a coalesced receive
    (several complete messages in one read) and a chunk split across reads all
    the same code path rather than three special cases.
    """

    _EXPECT_HEADER = "HEADER"
    _EXPECT_DATA = "DATA"

    def __init__(self, *,
                 max_message_octets: int = DEFAULT_MAX_MESSAGE_OCTETS) -> None:
        self.max_message_octets = int(max_message_octets)
        self._buffer = bytearray()
        self._message = bytearray()
        self._ready: deque[bytes] = deque()
        self._ready_counts: deque[int] = deque()
        self._state = self._EXPECT_HEADER
        self._remaining = 0
        self._chunks = 0
        self._chunks_in_last_message = 0
        self._eom_suspect = False
        #: Chunk counts aligned with the list :meth:`drain` last returned.
        self.drained_frame_counts: list[int] = []

    # -- observation ------------------------------------------------------
    @property
    def chunks_in_last_message(self) -> int:
        return self._chunks_in_last_message

    @property
    def at_message_boundary(self) -> bool:
        return (self._state == self._EXPECT_HEADER and not self._message
                and not self._buffer)

    # -- the machine ------------------------------------------------------
    def feed(self, data: bytes) -> list[bytes]:
        self._buffer += bytes(data)
        while True:
            if self._eom_suspect:
                self._classify_suspect(at_end=False)
                return self.drain()
            if self._state == self._EXPECT_DATA:
                if not self._consume_data():
                    break
                continue
            if not self._consume_header():
                break
        return self.drain()

    def drain(self) -> list[bytes]:
        found = list(self._ready)
        self._ready.clear()
        self.drained_frame_counts = list(self._ready_counts)
        self._ready_counts.clear()
        return found

    def close(self) -> None:
        """The peer stopped sending.  Say precisely what was incomplete."""
        if self._eom_suspect:
            self._classify_suspect(at_end=True)
        if self._state == self._EXPECT_DATA:
            raise NetconfFramingError(
                "LO1-CFRAME-003",
                "the peer ended the stream with %d octets of chunk %d still "
                "outstanding" % (self._remaining, self._chunks + 1))
        if self._buffer:
            raise NetconfFramingError(
                "LO1-CFRAME-003",
                "the peer ended the stream inside the header of chunk %d: %r"
                % (self._chunks + 1, bytes(self._buffer[:16])))
        if self._message:
            raise NetconfFramingError(
                "LO1-CFRAME-004",
                "the peer ended the stream after %d chunk(s) without sending "
                "the end-of-chunks marker" % self._chunks)

    # -- header -----------------------------------------------------------
    def _consume_header(self) -> bool:
        buffer = self._buffer
        if not buffer:
            return False
        if buffer[0] != _LF:
            self._eom_suspect = True
            return True
        if len(buffer) < 2:
            return False
        if buffer[1] != _HASH:
            raise NetconfFramingError(
                "LO1-CFRAME-001",
                "a chunk header must begin LF '#', not LF %r"
                % bytes(buffer[1:2]))
        if len(buffer) < 3:
            return False
        if buffer[2] == _HASH:
            # end-of-chunks: LF '#' '#' LF
            if len(buffer) < 4:
                return False
            if buffer[3] != _LF:
                raise NetconfFramingError(
                    "LO1-CFRAME-001",
                    "the end-of-chunks marker must be LF '#' '#' LF, not %r"
                    % bytes(buffer[:4]))
            if self._chunks == 0:
                raise NetconfFramingError(
                    "LO1-CFRAME-001",
                    "end-of-chunks arrived before any chunk; a chunked message "
                    "is 1*chunk followed by end-of-chunks")
            del buffer[:4]
            self._ready.append(bytes(self._message))
            self._ready_counts.append(self._chunks)
            self._chunks_in_last_message = self._chunks
            self._message = bytearray()
            self._chunks = 0
            return True
        return self._consume_size()

    def _consume_size(self) -> bool:
        buffer = self._buffer
        digits = bytearray()
        index = 2
        while index < len(buffer):
            octet = buffer[index]
            if octet == _LF:
                break
            if not _ZERO <= octet <= _NINE:
                raise NetconfFramingError(
                    "LO1-CFRAME-001",
                    "the chunk size contains the non-digit octet %r"
                    % bytes(buffer[index:index + 1]))
            digits.append(octet)
            if len(digits) > MAX_CHUNK_SIZE_DIGITS:
                raise NetconfFramingError(
                    "LO1-CFRAME-001",
                    "the chunk size is longer than %d digits"
                    % MAX_CHUNK_SIZE_DIGITS)
            index += 1
        if index >= len(buffer):
            return False  # the size field is still arriving
        if not digits:
            raise NetconfFramingError(
                "LO1-CFRAME-001", "the chunk header carries an empty chunk size")
        if digits[0] == _ZERO:
            raise NetconfFramingError(
                "LO1-CFRAME-002",
                "the chunk size %r has a leading zero; RFC 6242 spells the "
                "size with a non-zero first digit" % bytes(digits))
        size = int(digits.decode("ascii"))
        if size > MAX_CHUNK_OCTETS:
            raise NetconfFramingError(
                "LO1-CFRAME-001",
                "the chunk size %d exceeds the RFC 6242 maximum %d"
                % (size, MAX_CHUNK_OCTETS))
        if size > self.max_message_octets or (
                len(self._message) + size > self.max_message_octets):
            raise NetconfFramingError(
                "LO1-CFRAME-006",
                "a chunked message would reach %d octets, past the consumer's "
                "%d octet frame limit"
                % (len(self._message) + size, self.max_message_octets))
        del buffer[:index + 1]
        self._remaining = size
        self._state = self._EXPECT_DATA
        return True

    def _consume_data(self) -> bool:
        buffer = self._buffer
        if not buffer:
            return False
        take = min(self._remaining, len(buffer))
        self._message += buffer[:take]
        del buffer[:take]
        self._remaining -= take
        if self._remaining:
            return False
        self._chunks += 1
        self._state = self._EXPECT_HEADER
        return True

    # -- classification ---------------------------------------------------
    def _classify_suspect(self, *, at_end: bool) -> None:
        """A header that does not start with LF.  Name it before refusing.

        A peer that kept the 1.0 sentinel after advertising ``base:1.1`` is a
        different defect from a garbled size field, and the two must not share
        a reason code -- the withdrawn release used the sentinel everywhere, so
        that is the one this consumer has to be able to point at.
        """
        if END_OF_MESSAGE_SENTINEL in self._buffer:
            raise NetconfFramingError(
                "LO1-CFRAME-005",
                "the peer sent an end-of-message framed payload after both "
                "sides advertised base:1.1; RFC 6242 permits ]]>]]> only for "
                "the hello exchange")
        if at_end or len(self._buffer) >= _EOM_CLASSIFICATION_OCTETS:
            raise NetconfFramingError(
                "LO1-CFRAME-001",
                "a chunk header must begin with LF; the peer sent %r"
                % bytes(self._buffer[:32]))
        self._eom_suspect = True

    # -- offline helper ---------------------------------------------------
    def count_chunks(self, framed: bytes) -> int:
        messages = self.feed(framed)
        if not messages:
            raise NetconfFramingError(
                "LO1-CFRAME-004",
                "the octets do not contain one complete chunked message")
        return self._chunks_in_last_message


def decode_chunked(framed: bytes, *,
                   max_message_octets: int = DEFAULT_MAX_MESSAGE_OCTETS
                   ) -> list[bytes]:
    """Decode a complete octet string.  Used by tests and by evidence tooling."""
    decoder = ChunkedMessageDecoder(max_message_octets=max_message_octets)
    messages = decoder.feed(framed)
    decoder.close()
    return messages


def framing_of(capabilities: Sequence[str]) -> str:
    """Convenience for a single-sided view; both sides still have to agree."""
    return (FRAMING_CHUNKED
            if NETCONF_BASE_1_1_CAPABILITY in set(capabilities)
            else FRAMING_END_OF_MESSAGE)


__all__ = [
    "ChunkedMessageDecoder",
    "DEFAULT_MAX_MESSAGE_OCTETS",
    "DEFAULT_SEND_CHUNK_OCTETS",
    "END_OF_MESSAGE_SENTINEL",
    "EndOfMessageDecoder",
    "FRAMING_CHUNKED",
    "FRAMING_END_OF_MESSAGE",
    "FRAMING_REASON_CODES",
    "MAX_CHUNK_OCTETS",
    "MAX_CHUNK_SIZE_DIGITS",
    "NETCONF_BASE_1_0_CAPABILITY",
    "NETCONF_BASE_1_1_CAPABILITY",
    "NetconfFramingError",
    "NetconfTransportError",
    "chunk_count",
    "decode_chunked",
    "encode_chunked",
    "encode_end_of_message",
    "framing_of",
    "negotiate_framing",
]
