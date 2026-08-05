#!/usr/bin/env python3
"""Relay MoldUDP64 multicast datagrams from Docker to the Windows host.

The relay joins the multicast group inside the Docker Compose bridge network,
then forwards every UDP datagram unchanged by unicast to host.docker.internal.
"""

from __future__ import annotations

import argparse
import socket
import sys


MAX_UDP_DATAGRAM_BYTES = 65_535


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--multicast-group", default="239.192.0.1")
    parser.add_argument("--multicast-port", type=int, default=18_000)
    parser.add_argument(
        "--multicast-interface",
        default="0.0.0.0",
        help="IPv4 interface used to join the group; 0.0.0.0 selects the default",
    )
    parser.add_argument("--target-host", default="host.docker.internal")
    parser.add_argument("--target-port", type=int, default=18_000)
    return parser.parse_args()


def validate_port(name: str, value: int) -> None:
    if not 1 <= value <= 65_535:
        raise ValueError(f"{name} must be between 1 and 65535")


def main() -> int:
    args = parse_args()

    try:
        validate_port("multicast-port", args.multicast_port)
        validate_port("target-port", args.target_port)
        socket.inet_aton(args.multicast_group)
        socket.inet_aton(args.multicast_interface)
        target_ip = socket.gethostbyname(args.target_host)
    except (OSError, ValueError) as exc:
        print(f"relay configuration error: {exc}", file=sys.stderr, flush=True)
        return 2

    receiver = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
    sender = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)

    try:
        receiver.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        receiver.bind(("", args.multicast_port))

        membership = (
            socket.inet_aton(args.multicast_group)
            + socket.inet_aton(args.multicast_interface)
        )
        receiver.setsockopt(
            socket.IPPROTO_IP,
            socket.IP_ADD_MEMBERSHIP,
            membership,
        )

        target = (target_ip, args.target_port)
        print(
            "MoldUDP64 relay ready: "
            f"{args.multicast_group}:{args.multicast_port} "
            f"-> {args.target_host} ({target_ip}):{args.target_port}",
            flush=True,
        )

        packet_count = 0
        byte_count = 0

        while True:
            datagram, source = receiver.recvfrom(MAX_UDP_DATAGRAM_BYTES)
            sender.sendto(datagram, target)

            packet_count += 1
            byte_count += len(datagram)
            if packet_count == 1 or packet_count % 1_000 == 0:
                print(
                    f"relayed packets={packet_count} bytes={byte_count} "
                    f"last_source={source[0]}:{source[1]} "
                    f"last_size={len(datagram)}",
                    flush=True,
                )
    except KeyboardInterrupt:
        return 0
    except OSError as exc:
        print(f"relay socket error: {exc}", file=sys.stderr, flush=True)
        return 3
    finally:
        receiver.close()
        sender.close()


if __name__ == "__main__":
    raise SystemExit(main())
