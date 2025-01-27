"""Async meter connection module."""
from __future__ import annotations

import datetime
import logging
from abc import ABCMeta, abstractmethod
from asyncio import (
    FIRST_COMPLETED,
    BaseTransport,
    CancelledError,
    Event,
    Future,
    Protocol,
    Queue,
    iscoroutinefunction,
    sleep,
    wait,
)
from typing import Awaitable, Callable, ClassVar, Sequence, Tuple

from han.common import MeterMessageBase, MeterReaderBase

_LOGGER = logging.getLogger(__name__)


class BackOffStrategy(metaclass=ABCMeta):
    """
    Back-off strategy base class.

    Create sub-classes to implement different strategies.
    """

    DEFAULT_MAX_DELAY_SEC: int = 60

    @abstractmethod
    def failure(self) -> None:
        """Call this after a failure."""

    @abstractmethod
    def reset(self) -> None:
        """Call this after success to reset."""

    @property
    @abstractmethod
    def current_delay_sec(self) -> int:
        """Return current back-off delay in seconds."""


class ExponentialBackOff(BackOffStrategy):
    """Exponential back-off strategy."""

    def __init__(self) -> None:
        """Initialize ExponentialBackOff."""
        self._delay: int = 0
        self.max_delay: int = super().DEFAULT_MAX_DELAY_SEC

    def failure(self) -> None:
        """Call this after a failure."""
        self._delay = self._delay * 2
        if self._delay == 0:
            self._delay = 1

    def reset(self) -> None:
        """Call this after success to reset."""
        self._delay = 0

    @property
    def current_delay_sec(self) -> int:
        """Return current back-off delay in seconds."""
        return self._delay if self._delay < self.max_delay else self.max_delay


class SmartMeterBaseProtocol(Protocol, metaclass=ABCMeta):
    """
    Network protocol base class that reads smart meter messages from a stream.

    Sub classes must implement message_received().
    """

    # Total number of this class that has been created.
    # The number is used when initializing trace id for new instances.
    total_instance_counter: ClassVar[int] = 0

    def __init__(
        self,
        reader_candidates: Sequence[MeterReaderBase],
    ) -> None:
        """
        Initialize SmartMeterProtocol.

        :param reader_candidates: message reader candidates.
        """
        super().__init__()
        self._done: Future[None] = Future()
        self.instance_id: int = SmartMeterBaseProtocol.total_instance_counter
        self._reader_candidates = list(reader_candidates)
        self._selected_reader: MeterReaderBase | None = None
        self._transport: BaseTransport | None = None
        self._transport_info: str | None = None
        SmartMeterBaseProtocol.total_instance_counter += 1

    @property
    def done(self) -> Awaitable[None]:
        """Return Awaitable that can be used to wait for connection to be lost or closed."""
        return self._done

    def _set_transport_info(self) -> None:
        if self._transport:
            if hasattr(self._transport, "serial"):
                self._transport_info = str(self._transport.serial)  # type: ignore
            else:
                peer_name = self._transport.get_extra_info("peername")
                if peer_name:
                    host, port, *_ = peer_name
                    self._transport_info = f"host {host} and port {port}"

            if not self._transport_info:
                self._transport_info = str(self._transport)

    def connection_made(self, transport: BaseTransport) -> None:
        """
        Connect the transport with this protocol instance.

        Called when a connection is made.
        The argument is the transport representing the connection.
        To receive data, wait for data_received() calls.
        When the connection is closed, connection_lost() is called.
        """
        self._transport = transport
        self._set_transport_info()
        _LOGGER.info(
            "%s: Smart meter connected to %s", self._instance_id(), self._transport_info
        )

    def connection_lost(self, exc: Exception | None) -> None:
        """
        Signal that the transport connected to this protocol instance has been lost or closed.

        Called when the connection is lost or closed.
        The argument is an exception object or None (the latter
        meaning a regular EOF is received or the connection was
        aborted or closed).

        This method set the Done property.
        """
        if exc:
            _LOGGER.warning(
                "%s: Connection to %s lost: %s",
                self._instance_id(),
                self._transport_info,
                exc,
            )
        else:
            _LOGGER.debug(
                "%s: Connection to %s closed",
                self._instance_id(),
                self._transport_info,
            )

        if self._transport:
            try:
                self._transport.close()
                self._transport = None
            except Exception as ex:  # pylint: disable=broad-except
                _LOGGER.warning(
                    "%s: Error when closing transport %s for %s connection: %s",
                    self._instance_id(),
                    self._transport_info,
                    "lost" if exc else "closed",
                    ex,
                )

        self._done.set_result(None)

    def data_received(self, data: bytes) -> None:
        """Receive data from the transport and put messages(s) on the queue if messages(s) are ready."""
        if self._selected_reader:
            messages = self._selected_reader.read(data)
            for msg in messages:
                self.message_received(msg)
        else:
            for reader in self._reader_candidates:
                messages = reader.read(data)
                for msg in messages:
                    if msg.is_valid:
                        self._selected_reader = reader
                        self._reader_candidates.clear()
                        _LOGGER.info("Reader %s selected.", reader)
                        break
                if self._selected_reader:
                    for msg in messages:
                        self.message_received(msg)
                    break

    @abstractmethod
    def message_received(self, message: MeterMessageBase) -> None:
        """Message is received from the transport."""

    def eof_received(self) -> bool:
        """
        Return False to close the transport.

        Called when the other end signals it won't send any more data.
        """
        _LOGGER.debug(
            "%s: eof_received - the other end (%s) signaled it won't send any more data. Close transport",
            self._instance_id(),
            self._transport_info,
        )

        # return false to close transport.
        return False

    def _instance_id(self) -> str:
        return f"{self.__class__.__name__}[{self.instance_id}]"


class SmartMeterMessageProtocol(SmartMeterBaseProtocol):
    """
    Network protocol that reads smart meter messages from a stream and forwards them to a queue.

    When the user wants to requests a transport to use with this protocol,
    they pass a SmartMeterMessageProtocol factory to a utility function (e.g.,
    EventLoop.create_connection() or serial_asyncio.create_serial_connection).

    Example:
        await serial_asyncio.create_serial_connection(loop, lambda: SmartMeterMessageProtocol(queue), [ModeDReader()], url = "/dev/tty01")
    """

    def __init__(
        self,
        destination_queue: Queue[MeterMessageBase],
        reader_candidates: Sequence[MeterReaderBase],
    ) -> None:
        """
        Initialize SmartMeterMessageProtocol.

        :param destination_queue: destination queue for received messages.
        :param reader_candidates: message reader candidates.
        """
        super().__init__(reader_candidates)
        self.queue: Queue[MeterMessageBase] = destination_queue

    def message_received(self, message: MeterMessageBase) -> None:
        """Received message is passed on to the queue."""
        self.queue.put_nowait(message)


class SmartMeterMessagePayloadProtocol(SmartMeterBaseProtocol):
    """
    Network protocol that reads smart meter messages from a stream and forwards their payload to a queue.

    When the user wants to requests a transport to use with this protocol,
    they pass a SmartMeterMessagePayloadProtocol factory to a utility function (e.g.,
    EventLoop.create_connection() or serial_asyncio.create_serial_connection).

    Example:
        await serial_asyncio.create_serial_connection(loop, lambda: SmartMeterMessagePayloadProtocol(queue), [ModeDReader()] url = "/dev/tty01")
    """

    def __init__(
        self,
        destination_queue: Queue[bytes],
        reader_candidates: Sequence[MeterReaderBase],
    ) -> None:
        """
        Initialize SmartMeterMessagePayloadProtocol.

        :param destination_queue: destination queue for received messages payloads.
        :param reader_candidates: message reader candidates.
        """
        super().__init__(reader_candidates)
        self.queue: Queue[bytes] = destination_queue

    def message_received(self, message: MeterMessageBase) -> None:
        """
        Message is received and its payload is passed on to the queue.

        Only payload from non empty payloads of valid messages (check sum etc.) is passed on
        """
        payload = message.payload
        if message.is_valid:
            if payload is not None and len(payload) > 0:
                self.queue.put_nowait(payload)
            else:
                _LOGGER.debug("Got empty message.")
        else:
            _LOGGER.warning(
                "Got invalid message: %s",
                message.as_bytes.hex() if message.as_bytes else "<empty>",
            )


MeterTransportProtocol = Tuple[BaseTransport, SmartMeterBaseProtocol]

AsyncConnectionFactory = Callable[[], Awaitable[MeterTransportProtocol]]


class ConnectionManager:
    # pylint: disable=too-many-instance-attributes
    """
    Maintain connection and reconnect if connection is lost.

    Reconnecting uses a back-off retry strategy, and has a simple circuit breaker for connection lost.
    """

    DEFAULT_CONNECTION_LOST_BACK_OFF_THRESHOLD: int = 5
    DEFAULT_CONNECTION_LOST_BACK_OFF_SLEEP_SEC: int = 5

    def __init__(
        self,
        connection_factory: AsyncConnectionFactory,
    ) -> None:
        """
        Initialize class.

        :param connection_factory: A factory function that returns a Transport and SmartMeterProtocol tuple.
        """
        if not iscoroutinefunction(connection_factory):
            raise ValueError("Factory must be awaitable.")

        self._connection_factory: AsyncConnectionFactory = connection_factory
        self._connection: MeterTransportProtocol | None = None
        self._is_closing: Event = Event()

        self.back_off_connect_error: BackOffStrategy = ExponentialBackOff()

        self.connection_lost_back_off_threshold: int = (
            ConnectionManager.DEFAULT_CONNECTION_LOST_BACK_OFF_SLEEP_SEC
        )
        self.connection_lost_back_off_sleep_sec: int = (
            ConnectionManager.DEFAULT_CONNECTION_LOST_BACK_OFF_SLEEP_SEC
        )
        self._connection_lost_last_time: datetime.datetime | None = None
        self._connection_lost_sleep_before_reconnect: bool = False

    def close(self) -> None:
        """Close current connection, if any, and stop reconnecting."""
        self._is_closing.set()
        if self._connection:
            _LOGGER.info("Close connection and abort connect loop")
            transport, _ = self._connection
            transport.close()
            self._connection = None

    async def connect_loop(self) -> None:
        """
        Connect to meter using connection factory, and keep reconnecting if connection is lost.

        The connection is not reconnected on connection loss if close() was called on this instance.
        """
        while not self._is_closing.is_set():
            await wait(
                (self._try_connect(), self._is_closing.wait()),
                return_when=FIRST_COMPLETED,
            )

            if self._connection:
                _, protocol = self._connection
                await wait(
                    (protocol.done, self._is_closing.wait()),
                    return_when=FIRST_COMPLETED,
                )

                if not self._is_closing.is_set():
                    _LOGGER.warning("Connection lost")
                    self._update_connection_lost_circuit_breaker()

                self._connection = None

        # done closing if that was the case of connection loss
        self._is_closing.clear()

        _LOGGER.info("Connect loop done")

    def _update_connection_lost_circuit_breaker(self) -> None:
        now = datetime.datetime.utcnow()
        if self._connection_lost_last_time:
            delta = now - self._connection_lost_last_time
            self._connection_lost_sleep_before_reconnect = (
                delta.total_seconds() < self.connection_lost_back_off_threshold
            )
        self._connection_lost_last_time = now

    def _get_back_off_time(self) -> int:
        sleep_time = 0
        current_connect_error_delay = self.back_off_connect_error.current_delay_sec
        if (
            current_connect_error_delay > 0
            or self._connection_lost_sleep_before_reconnect
        ):
            reconnect_sleep = (
                self.connection_lost_back_off_sleep_sec
                if self._connection_lost_sleep_before_reconnect
                else 0
            )
            sleep_time = max(current_connect_error_delay, reconnect_sleep)

            _LOGGER.info(
                "Back-off for %d sec before reconnecting",
                sleep_time,
            )
        return sleep_time

    async def _try_connect(self) -> None:
        sleep_time = self._get_back_off_time()
        if sleep_time > 0:
            await sleep(sleep_time)

        if not self._is_closing.is_set():
            try:
                _LOGGER.debug("Try to connect")
                self._connection = await self._connection_factory()

                self.back_off_connect_error.reset()
            except CancelledError:
                _LOGGER.debug("The operation was cancelled")
                raise
            except Exception as ex:  # pylint: disable=broad-except
                self._connection = None
                self.back_off_connect_error.failure()
                _LOGGER.warning("Error connecting: %s", ex)
