"""Arranque: levanta el servidor local y abre el navegador."""

from __future__ import annotations

import argparse
import socket
import threading
import time
import webbrowser

import uvicorn

from . import config
from .server import create_app


def free_port(host: str, preferred: int = 8765) -> int:
    for candidate in (preferred, 0):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                sock.bind((host, candidate))
            except OSError:
                continue
            return sock.getsockname()[1]
    raise RuntimeError("No hay puertos libres")


def open_later(url: str, delay: float = 1.0) -> None:
    def worker() -> None:
        time.sleep(delay)
        webbrowser.open(url)

    threading.Thread(target=worker, daemon=True).start()


def main() -> None:
    parser = argparse.ArgumentParser(prog="chatnodes", description=f"{config.APP_NAME}: estudio con hilos ramificados")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--no-browser", action="store_true", help="no abrir el navegador")
    args = parser.parse_args()

    port = free_port(args.host, args.port)
    url = f"http://{args.host}:{port}/"

    print(f"{config.APP_NAME} en {url}")
    print(f"Datos en {config.data_dir()}")
    if not args.no_browser:
        open_later(url)

    uvicorn.run(create_app(), host=args.host, port=port, log_level="warning")


if __name__ == "__main__":
    main()
