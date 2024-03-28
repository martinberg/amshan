"""Util for reading from HAN port."""
from __future__ import annotations

import argparse
import datetime
import json
import logging
import signal
import sys
from influxdb import InfluxDBClient
from asyncio import Queue, create_task, get_event_loop, run
from typing import Any

from han import autodecoder
from han.meter_connection import (
    AsyncConnectionFactory,
    ConnectionManager,
    MeterTransportProtocol,
)
from han.serial_connection_factory import create_serial_message_payload_connection
from han.tcp_connection_factory import create_tcp_message_payload_connection

logging.basicConfig(
    level=logging.DEBUG,
    format="%(levelname)7s: %(message)s",
    stream=sys.stderr,
)
LOG = logging.getLogger("")


def _get_arg_parser() -> argparse.ArgumentParser:
    def valid_host_port(host_port: str) -> tuple[str, str]:
        host_and_port = host_port.split(":")
        if len(host_and_port) == 2:
            return host_and_port[0], host_and_port[1]
        else:
            msg = f"Not a valid host and port: '{host_port}'."
            raise argparse.ArgumentTypeError(msg)

    parser = argparse.ArgumentParser("read HAN port")

    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument(
        "-host",
        dest="hostandport",
        type=valid_host_port,
        help="input host and port separated by :",
    )
    group.add_argument("-serial", dest="serialdevice", help="input serial port")

    parser.add_argument(
        "-sp",
        dest="ser_parity",
        default="N",
        required=False,
        choices=["N", "O", "E"],
        help="input serial port parity",
    )
    parser.add_argument(
        "-sb",
        dest="ser_baudrate",
        default=2400,
        type=int,
        required=False,
        help="input serial port baud rate",
    )
    parser.add_argument("-mh", dest="mqtthost", default="localhost", help="mqtt host")
    parser.add_argument(
        "-mp", dest="mqttport", type=int, default=1883, help="mqtt port port"
    )
    parser.add_argument(
        "-t", dest="mqtttopic", default="han", help="mqtt publish topic"
    )
    parser.add_argument(
        "-dumpfile", dest="dumpfile", help="dump received bytes to file"
    )
    parser.add_argument(
        "-r",
        dest="reconnect",
        type=bool,
        default=True,
        help="automatic retry/reconnect meter connection",
    )
    parser.add_argument("-v", dest="verbose", default=False)
    return parser


def _json_converter(source: Any) -> str | None:
    if isinstance(source, datetime.datetime):
        return source.isoformat()
    return None


_decoder = autodecoder.AutoDecoder()


def _measure_received(frame: bytes) -> None:
    decoded_frame = _decoder.decode_message_payload(frame)
    if decoded_frame:
        json_frame = json.dumps(decoded_frame, default=_json_converter)
        send_frame_to_inflxudb(decoded_frame)
        LOG.debug("Decoded frame: %s", json_frame)
    else:
        LOG.error("Could not decode frame content: %s", frame.hex())


def send_frame_to_inflxudb(decoded_frame, hostname='localhost', database='elmatare', port='8086', username='username', password='password'):
    measurement_list = []
    ignore_fields = ["meter_datetime", "meter_id", "meter_type", "meter_manufacturer", "list_ver_id"]
    regular_fields = ["active_power_import", "active_power_export", "reactive_power_import", "reactive_power_export",
                      "current_l1", "current_l2", "current_l3", "voltage_l1", "voltage_l2", "voltage_l3"]
    hourly_fields = ["active_power_import_total", "active_power_export_total", "reactive_power_import_total",
                     "reactive_power_export_total"]

    for key in decoded_frame:
        if key in ignore_fields:
            continue
        if key in regular_fields or key in hourly_fields:
            measurement_dick = {
                "measurement": "elmatare",
                "tags": {
                    "meter_id": str(decoded_frame["meter_id"]),
                    "meter_type": str(decoded_frame["meter_type"]),
                    "meter_manufacturer": str(decoded_frame["meter_manufacturer"]),
                    "list_ver_id": str(decoded_frame["list_ver_id"]),
                    "measurement": str(key)
                },
                #"time": decoded_frame["meter_datetime"],
                "time": datetime.datetime.utcnow().isoformat(),
                "fields": {
                    "value": float(decoded_frame[key])
                }
            }

            measurement_list.append(measurement_dick)

    client = InfluxDBClient(host=hostname, port=port, username=username, password=password)
    client.write_points(measurement_list, database=database)

    client.close()


async def _process_frames(queue: "Queue[bytes]") -> None:
    while True:
        frame = await queue.get()
        _measure_received(frame)


async def main() -> None:
    """Start reading."""
    args = _get_arg_parser().parse_args()
    loop = get_event_loop()

    queue: Queue[bytes] = Queue()

    create_task(_process_frames(queue))

    async def tcp_connection_factory() -> MeterTransportProtocol:
        host, port = args.hostandport
        return await create_tcp_message_payload_connection(
            queue, loop, None, host, port
        )

    async def serial_connection_factory() -> MeterTransportProtocol:
        return await create_serial_message_payload_connection(
            queue,
            None,
            None,
            url=args.serialdevice,
            baudrate=args.ser_baudrate,
            parity=args.ser_parity,
        )

    connection_factory: AsyncConnectionFactory = (
        serial_connection_factory if args.serialdevice else tcp_connection_factory
    )

    if args.reconnect:
        # use high-level ConnectionManager
        connection_manager = ConnectionManager(connection_factory)
        loop.add_signal_handler(signal.SIGINT, connection_manager.close)
        await connection_manager.connect_loop()
    else:
        # use low-level transport and protocol
        transport, protocol = await connection_factory()
        loop.add_signal_handler(signal.SIGINT, transport.close)
        await protocol.done

    LOG.info("Done...")


if __name__ == "__main__":
    run(main())
