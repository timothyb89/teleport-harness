"""A plain TCP relay: 127.0.0.1:<port> -> <host>:<port> (`cluster forward`, FORWARD=lan:…).

The forwarder's job is only to make `<id>.lab.<domain>:<INGRESS_PORT>` reach the harness
host's Traefik from this client (the wildcard record resolves to 127.0.0.1). Bytes pass
through untouched, so the test cluster's own TLS, SNI and ALPN still work end to end. This
exists so the LAN mode needs nothing beyond the harness's own Python (no socat).
"""

from __future__ import annotations

import asyncio


async def _pipe(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    try:
        while data := await reader.read(65536):
            writer.write(data)
            await writer.drain()
    except (ConnectionError, OSError):
        pass
    finally:
        try:
            writer.close()
        except Exception:  # noqa: BLE001 — already gone
            pass


async def serve(listen_port: int, host: str, port: int, bind: str = "127.0.0.1") -> None:
    async def handle(cr: asyncio.StreamReader, cw: asyncio.StreamWriter) -> None:
        try:
            ur, uw = await asyncio.open_connection(host, port)
        except OSError:
            cw.close()
            return
        await asyncio.gather(_pipe(cr, uw), _pipe(ur, cw))

    server = await asyncio.start_server(handle, bind, listen_port)
    async with server:
        await server.serve_forever()


def run(listen_port: int, target: str) -> None:
    host, _, port = target.rpartition(":")
    if not host:
        host, port = target, str(listen_port)
    asyncio.run(serve(listen_port, host, int(port)))
