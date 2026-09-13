"""Turnos en vuelo: parar, sobrevivir a una desconexion y reengancharse.

Hace falta concurrencia real (una peticion mientras otra sigue abierta) y
streaming real, y ni TestClient ni el transporte ASGI de httpx lo dan: ambos
ejecutan la aplicacion entera antes de entregar la respuesta. Asi que estos
tests levantan un uvicorn de verdad en un hilo.
"""

from __future__ import annotations

import asyncio
import json
import socket
import threading
import time
from contextlib import contextmanager

import httpx
import pytest
import uvicorn

from chatnodes.engine import Chunk
from chatnodes.server import create_app
from chatnodes.store import Store


class FakeProcess:
    def __init__(self) -> None:
        self.returncode = None
        self.killed = False

    def kill(self) -> None:
        self.killed = True
        self.returncode = -9


class GatedEngine:
    """Escribe un trozo y se queda esperando: o lo matan, o se le libera."""

    def __init__(self) -> None:
        self.release = False
        self.process: FakeProcess | None = None

    async def stream(
        self,
        *,
        prompt,
        cwd,
        session_id=None,
        resume=None,
        fork=False,
        model=None,
        system_prompt=None,
        on_start=None,
    ):
        process = FakeProcess()
        self.process = process
        if on_start is not None:
            on_start(process)
        yield Chunk(kind="session", session_id="s-live")
        yield Chunk(kind="text", text="primera parte. ")
        for _ in range(600):
            if process.killed:
                yield Chunk(kind="error", text="terminado por senal")
                return
            if self.release:
                break
            await asyncio.sleep(0.01)
        yield Chunk(kind="text", text="final.")
        yield Chunk(kind="done", session_id="s-live", cost_usd=0.002)


class QuietServer(uvicorn.Server):
    def install_signal_handlers(self) -> None:  # corre en un hilo secundario
        pass


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@contextmanager
def running(engine, store):
    app = create_app(store=store, engine=engine)
    port = _free_port()
    server = QuietServer(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    limite = time.time() + 15
    while not server.started and time.time() < limite:
        time.sleep(0.02)
    if not server.started:
        raise RuntimeError("el servidor de pruebas no arranco")
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        engine.release = True
        server.should_exit = True
        thread.join(timeout=10)


@pytest.fixture
def app_en_marcha(tmp_path):
    engine = GatedEngine()
    store = Store(tmp_path / "turnos.db")
    with running(engine, store) as base_url:
        with httpx.Client(base_url=base_url, timeout=20) as client:
            project = client.post("/api/projects", json={"name": "P"}).json()["project"]
            chat = client.post(f"/api/projects/{project['id']}/chats", json={}).json()["chat"]
            yield client, chat, engine
    store.close()


def frame(line: str) -> dict | None:
    return json.loads(line[6:]) if line.startswith("data: ") else None


def live_keys(client, chat_id) -> list[str]:
    return [e["key"] for e in client.get(f"/api/chats/{chat_id}/live").json()["live"]]


def test_parar_un_turno_corta_y_conserva_lo_escrito(app_en_marcha):
    client, chat, engine = app_en_marcha
    eventos = []

    with client.stream("POST", f"/api/chats/{chat['id']}/turn", json={"text": "hola"}) as response:
        for line in response.iter_lines():
            event = frame(line)
            if event is None:
                continue
            eventos.append(event)
            if event["t"] == "delta":
                assert live_keys(client, chat["id"]) == [chat["id"]]
                assert client.post(f"/api/turns/{chat['id']}/stop").json()["stopped"] is True

    assert eventos[-1]["t"] == "stopped"
    assert engine.process.killed is True

    respuesta = client.get(f"/api/chats/{chat['id']}").json()["messages"][1]
    assert respuesta["content"] == "primera parte. "
    assert respuesta["error"] is None  # parar no es un fallo
    assert live_keys(client, chat["id"]) == []


def test_el_turno_sobrevive_a_que_se_cierre_el_navegador(app_en_marcha):
    client, chat, engine = app_en_marcha

    with client.stream("POST", f"/api/chats/{chat['id']}/turn", json={"text": "hola"}) as response:
        for line in response.iter_lines():
            event = frame(line)
            if event and event["t"] == "delta":
                break  # se corta la conexion a media respuesta

    assert live_keys(client, chat["id"]) == [chat["id"]]

    recuperados = []
    with client.stream("GET", f"/api/turns/{chat['id']}/stream") as response:
        for line in response.iter_lines():
            event = frame(line)
            if event is None:
                continue
            recuperados.append(event)
            engine.release = True  # que termine mientras seguimos escuchando

    textos = "".join(e.get("text", "") for e in recuperados if e["t"] == "delta")
    assert textos == "primera parte. final."
    assert recuperados[0]["t"] == "accepted"
    assert recuperados[-1]["t"] == "done"

    respuesta = client.get(f"/api/chats/{chat['id']}").json()["messages"][1]
    assert respuesta["content"] == "primera parte. final."
    assert respuesta["cost_usd"] == 0.002


def test_no_se_puede_lanzar_otro_turno_en_el_mismo_hilo(app_en_marcha):
    client, chat, engine = app_en_marcha

    with client.stream("POST", f"/api/chats/{chat['id']}/turn", json={"text": "hola"}) as response:
        for line in response.iter_lines():
            event = frame(line)
            if event and event["t"] == "delta":
                segundo = client.post(
                    f"/api/chats/{chat['id']}/turn", json={"text": "otra cosa"}
                )
                assert segundo.status_code == 409
                client.post(f"/api/turns/{chat['id']}/stop")
                break


def test_turno_inexistente_da_404(app_en_marcha):
    client, chat, engine = app_en_marcha
    assert client.get("/api/turns/no-existe/stream").status_code == 404
    assert client.post("/api/turns/no-existe/stop").status_code == 404
