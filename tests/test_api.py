from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from chatnodes.engine import Chunk
from chatnodes.server import create_app
from chatnodes.store import Store


class FakeProcess:
    """Proceso de mentira: solo necesita poder morir."""

    def __init__(self) -> None:
        self.returncode = None
        self.killed = False

    def kill(self) -> None:
        self.killed = True
        self.returncode = -9


class FakeEngine:
    """Motor de mentira: registra cada invocacion y responde un texto fijo."""

    def __init__(self) -> None:
        self.calls: list[dict] = []
        self.reply = "respuesta de prueba"
        self.forked = 0

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
        self.calls.append(
            {
                "prompt": prompt,
                "cwd": str(cwd),
                "session_id": session_id,
                "resume": resume,
                "fork": fork,
                "model": model,
                "system_prompt": system_prompt,
            }
        )
        if on_start is not None:
            on_start(FakeProcess())
        if fork:
            # El CLI devuelve un id nuevo cuando se forkea una sesion.
            self.forked += 1
            sid = f"fork-{self.forked}"
        else:
            sid = resume or session_id or "sesion-falsa"
        yield Chunk(kind="session", session_id=sid)
        yield Chunk(kind="text", text=self.reply)
        yield Chunk(kind="done", session_id=sid, cost_usd=0.005)


@pytest.fixture
def engine():
    return FakeEngine()


@pytest.fixture
def client(tmp_path, engine):
    store = Store(tmp_path / "api.db")
    app = create_app(store=store, engine=engine)
    with TestClient(app) as test_client:
        test_client.store = store
        yield test_client
    store.close()


def events(response) -> list[dict]:
    return [
        json.loads(line[6:])
        for line in response.text.splitlines()
        if line.startswith("data: ")
    ]


def make_project(client, name="Sistemas Operativos") -> dict:
    return client.post("/api/projects", json={"name": name}).json()["project"]


def make_chat(client, project_id) -> dict:
    return client.post(f"/api/projects/{project_id}/chats", json={}).json()["chat"]


def send(client, chat_id, text) -> list[dict]:
    return events(client.post(f"/api/chats/{chat_id}/turn", json={"text": text}))


def last_assistant(client, chat_id):
    tree = client.get(f"/api/chats/{chat_id}").json()
    return [m for m in tree["messages"] if m["role"] == "assistant"][-1]


# --- proyectos y material ---------------------------------------------------


def test_crear_proyecto_crea_su_carpeta(client, tmp_path):
    from pathlib import Path

    project = make_project(client)
    folder = Path(project["folder"])
    assert folder.is_dir()
    assert "sistemas" in folder.name.lower()
    assert client.get("/api/projects").json()["projects"][0]["id"] == project["id"]


def test_subida_acepta_pdf_y_rechaza_lo_demas(client):
    project = make_project(client)
    response = client.post(
        f"/api/projects/{project['id']}/files",
        files=[
            ("files", ("tema1.pdf", b"%PDF-1.4 contenido", "application/pdf")),
            ("files", ("virus.exe", b"MZ", "application/octet-stream")),
        ],
    )
    payload = response.json()
    assert [f["name"] for f in payload["files"]] == ["tema1.pdf"]
    assert payload["rejected"] == ["virus.exe"]


def test_el_material_llega_al_prompt_de_sistema(client, engine):
    project = make_project(client)
    client.post(
        f"/api/projects/{project['id']}/files",
        files=[("files", ("tema1.pdf", b"%PDF", "application/pdf"))],
    )
    chat = make_chat(client, project["id"])
    send(client, chat["id"], "explicame el tema 1")

    system_prompt = engine.calls[0]["system_prompt"]
    assert "- tema1.pdf" in system_prompt
    assert engine.calls[0]["cwd"] == project["folder"]


def test_cada_chat_puede_elegir_su_material(client, engine):
    project = make_project(client)
    client.post(
        f"/api/projects/{project['id']}/files",
        files=[
            ("files", ("tema1.pdf", b"%PDF uno", "application/pdf")),
            ("files", ("tema2.pdf", b"%PDF dos", "application/pdf")),
        ],
    )
    chat = make_chat(client, project["id"])

    response = client.put(f"/api/chats/{chat['id']}/files", json={"names": ["tema2.pdf"]})
    assert response.status_code == 200
    assert response.json()["selected_files"] == ["tema2.pdf"]
    send(client, chat["id"], "explicame el material elegido")

    prompt = engine.calls[-1]["system_prompt"]
    assert "tema2.pdf" in prompt
    assert "tema1.pdf" not in prompt
    assert client.get(f"/api/chats/{chat['id']}").json()["chat"]["selected_files"] == [
        "tema2.pdf"
    ]


def test_no_se_puede_seleccionar_material_ajeno(client):
    project = make_project(client)
    chat = make_chat(client, project["id"])
    response = client.put(f"/api/chats/{chat['id']}/files", json={"names": ["no-existe.pdf"]})
    assert response.status_code == 400


def test_previsualizar_material_es_seguro_y_conserva_el_tipo(client):
    project = make_project(client)
    client.post(
        f"/api/projects/{project['id']}/files",
        files=[("files", ("apuntes.txt", b"hola mundo", "text/plain"))],
    )
    response = client.get(f"/api/projects/{project['id']}/files/apuntes.txt/content")
    assert response.status_code == 200
    assert response.text == "hola mundo"
    assert response.headers["content-type"].startswith("text/plain")
    assert client.get(f"/api/projects/{project['id']}/files/no-existe.txt/content").status_code == 404


# --- hilo principal ---------------------------------------------------------


def test_turno_persiste_pregunta_y_respuesta(client):
    project = make_project(client)
    chat = make_chat(client, project["id"])

    stream = send(client, chat["id"], "explicame el tema 1")

    assert stream[0]["t"] == "accepted"
    assert stream[-1]["t"] == "done"
    tree = client.get(f"/api/chats/{chat['id']}").json()
    assert [(m["role"], m["content"]) for m in tree["messages"]] == [
        ("user", "explicame el tema 1"),
        ("assistant", "respuesta de prueba"),
    ]
    assert tree["messages"][1]["cost_usd"] == 0.005


def test_el_primer_turno_abre_sesion_y_el_segundo_la_reanuda(client, engine):
    project = make_project(client)
    chat = make_chat(client, project["id"])

    send(client, chat["id"], "primera")
    send(client, chat["id"], "segunda")

    assert engine.calls[0]["resume"] is None
    assert engine.calls[0]["session_id"] is not None
    assert engine.calls[1]["resume"] == engine.calls[0]["session_id"]
    assert engine.calls[1]["session_id"] is None


def test_el_chat_se_titula_con_el_primer_mensaje(client):
    project = make_project(client)
    chat = make_chat(client, project["id"])
    send(client, chat["id"], "explicame la jerarquia de memoria")
    titles = [c["title"] for c in client.get(f"/api/projects/{project['id']}/chats").json()["chats"]]
    assert titles == ["explicame la jerarquia de memoria"]


def test_mensaje_vacio_se_rechaza(client):
    project = make_project(client)
    chat = make_chat(client, project["id"])
    assert client.post(f"/api/chats/{chat['id']}/turn", json={"text": "   "}).status_code == 400


# --- nodos ------------------------------------------------------------------


def open_node(client, chat_id, message_id, text, question, parent=None, offset=0):
    return events(
        client.post(
            f"/api/chats/{chat_id}/nodes",
            json={
                "anchor_message_id": message_id,
                "anchor_start": offset,
                "anchor_end": offset + len(text),
                "anchor_text": text,
                "question": question,
                "parent_node_id": parent,
            },
        )
    )


def test_el_nodo_nace_anclado_y_con_su_propia_sesion(client, engine):
    project = make_project(client)
    chat = make_chat(client, project["id"])
    send(client, chat["id"], "explicame el tema 1")
    anchor = last_assistant(client, chat["id"])

    stream = open_node(client, chat["id"], anchor["id"], "respuesta", "que significa esto")

    assert stream[0]["t"] == "node_created"
    tree = client.get(f"/api/chats/{chat['id']}").json()
    node = tree["nodes"][0]
    assert node["anchor_text"] == "respuesta"
    assert node["title"] == "que significa esto"
    assert [(m["role"], m["content"]) for m in node["messages"]] == [
        ("user", "que significa esto"),
        ("assistant", "respuesta de prueba"),
    ]
    # sesion propia, distinta de la del hilo principal
    assert node["session_id"] and node["session_id"] != tree["chat"]["session_id"]


def test_el_nodo_no_contamina_el_hilo_principal(client, engine):
    project = make_project(client)
    chat = make_chat(client, project["id"])
    send(client, chat["id"], "explicame el tema 1")
    anchor = last_assistant(client, chat["id"])
    open_node(client, chat["id"], anchor["id"], "respuesta", "que son los gigahercios")

    tree = client.get(f"/api/chats/{chat['id']}").json()
    assert len(tree["messages"]) == 2  # sigue habiendo solo pregunta + respuesta
    assert "gigahercios" not in json.dumps(tree["messages"])

    # y el siguiente turno del hilo principal reanuda la sesion original, no la del nodo
    send(client, chat["id"], "sigue")
    assert engine.calls[-1]["resume"] == tree["chat"]["session_id"]


def test_el_prompt_del_nodo_lleva_el_hilo_hasta_su_ancla(client, engine):
    project = make_project(client)
    chat = make_chat(client, project["id"])
    engine.reply = "la CPU va a 3 GHz"
    send(client, chat["id"], "explicame el reloj")
    anchor = last_assistant(client, chat["id"])
    engine.reply = "segunda diapositiva sobre interrupciones"
    send(client, chat["id"], "sigue")

    open_node(client, chat["id"], anchor["id"], "3 GHz", "que significa eso")

    prompt = engine.calls[-1]["prompt"]
    assert "explicame el reloj" in prompt
    assert "la CPU va a 3 GHz" in prompt
    assert "3 GHz" in prompt
    assert "que significa eso" in prompt
    # lo posterior al ancla se queda fuera
    assert "interrupciones" not in prompt
    assert "sigue" not in prompt


def test_el_nodo_usa_el_prompt_de_sistema_de_aclaracion(client, engine):
    project = make_project(client)
    chat = make_chat(client, project["id"])
    send(client, chat["id"], "hola")
    anchor = last_assistant(client, chat["id"])
    open_node(client, chat["id"], anchor["id"], "respuesta", "aclarame")
    assert "ACLARACION AL MARGEN" in engine.calls[-1]["system_prompt"]


def test_seguir_preguntando_en_el_nodo_reanuda_su_sesion(client, engine):
    project = make_project(client)
    chat = make_chat(client, project["id"])
    send(client, chat["id"], "hola")
    anchor = last_assistant(client, chat["id"])
    node = open_node(client, chat["id"], anchor["id"], "respuesta", "que es")[0]["node"]

    # El evento node_created viaja antes de que el CLI devuelva su id de sesion,
    # asi que la sesion del nodo se consulta ya persistida.
    sesion_del_nodo = client.get(f"/api/chats/{chat['id']}").json()["nodes"][0]["session_id"]
    stream = events(client.post(f"/api/nodes/{node['id']}/turn", json={"text": "y ahora esto"}))

    assert stream[-1]["t"] == "done"
    assert sesion_del_nodo
    assert engine.calls[-1]["resume"] == sesion_del_nodo
    assert engine.calls[-1]["prompt"] == "y ahora esto"
    tree = client.get(f"/api/chats/{chat['id']}").json()
    assert len(tree["nodes"][0]["messages"]) == 4


def test_nodo_anidado_cuelga_del_nodo_padre(client, engine):
    project = make_project(client)
    chat = make_chat(client, project["id"])
    engine.reply = "la CPU va a 3 GHz"
    send(client, chat["id"], "explicame el reloj")
    anchor = last_assistant(client, chat["id"])

    engine.reply = "un ciclo es la unidad minima de trabajo"
    padre = open_node(client, chat["id"], anchor["id"], "3 GHz", "que es eso")[0]["node"]
    hijo_anchor = [
        m for m in client.get(f"/api/chats/{chat['id']}").json()["nodes"][0]["messages"]
        if m["role"] == "assistant"
    ][-1]

    open_node(
        client, chat["id"], hijo_anchor["id"], "unidad minima", "y eso que quiere decir",
        parent=padre["id"],
    )

    prompt = engine.calls[-1]["prompt"]
    assert "unidad minima" in prompt
    tree = client.get(f"/api/chats/{chat['id']}").json()
    hijo = [n for n in tree["nodes"] if n["parent_node_id"]][0]
    assert hijo["parent_node_id"] == padre["id"]


def test_nodo_sin_seleccion_se_rechaza(client):
    project = make_project(client)
    chat = make_chat(client, project["id"])
    response = client.post(
        f"/api/chats/{chat['id']}/nodes",
        json={
            "anchor_message_id": "m",
            "anchor_start": 0,
            "anchor_end": 0,
            "anchor_text": "   ",
            "question": "eh",
        },
    )
    assert response.status_code == 400


def test_estado_visual_del_nodo_se_guarda(client):
    project = make_project(client)
    chat = make_chat(client, project["id"])
    send(client, chat["id"], "hola")
    anchor = last_assistant(client, chat["id"])
    node = open_node(client, chat["id"], anchor["id"], "respuesta", "que es")[0]["node"]

    patched = client.patch(
        f"/api/nodes/{node['id']}", json={"collapsed": True, "width": 430, "height": 260}
    ).json()["node"]

    assert patched["collapsed"] is True
    assert (patched["width"], patched["height"]) == (430, 260)
    tree = client.get(f"/api/chats/{chat['id']}").json()
    assert tree["nodes"][0]["collapsed"] is True


def test_borrar_nodo_arrastra_los_anidados(client):
    project = make_project(client)
    chat = make_chat(client, project["id"])
    send(client, chat["id"], "hola")
    anchor = last_assistant(client, chat["id"])
    padre = open_node(client, chat["id"], anchor["id"], "respuesta", "que es")[0]["node"]
    hijo_anchor = client.get(f"/api/chats/{chat['id']}").json()["nodes"][0]["messages"][1]
    open_node(client, chat["id"], hijo_anchor["id"], "prueba", "y esto", parent=padre["id"])

    borrados = client.delete(f"/api/nodes/{padre['id']}").json()["deleted"]

    assert len(borrados) == 2
    assert client.get(f"/api/chats/{chat['id']}").json()["nodes"] == []


def test_nodos_de_otro_chat_no_se_mezclan(client):
    project = make_project(client)
    uno = make_chat(client, project["id"])
    otro = make_chat(client, project["id"])
    send(client, uno["id"], "hola")
    anchor = last_assistant(client, uno["id"])
    open_node(client, uno["id"], anchor["id"], "respuesta", "que es")

    assert client.get(f"/api/chats/{otro['id']}").json()["nodes"] == []
    assert len(client.get(f"/api/chats/{uno['id']}").json()["nodes"]) == 1


# --- errores ----------------------------------------------------------------


def test_error_del_cli_se_guarda_en_el_mensaje(client, tmp_path):
    class BrokenEngine:
        async def stream(self, **kwargs):
            if kwargs.get("on_start"):
                kwargs["on_start"](FakeProcess())
            yield Chunk(kind="session", session_id="s")
            yield Chunk(kind="error", text="Error: not logged in")

    project = make_project(client)
    chat = make_chat(client, project["id"])
    client.app.state.engine = BrokenEngine()

    stream = send(client, chat["id"], "hola")

    assert stream[-1]["t"] == "error"
    assert "not logged in" in stream[-1]["message"]
    assert "not logged in" in last_assistant(client, chat["id"])["error"]


def test_chat_inexistente_da_404(client):
    assert client.get("/api/chats/nope").status_code == 404
    assert client.post("/api/chats/nope/turn", json={"text": "hola"}).status_code == 404


def test_la_interfaz_se_sirve(client):
    assert "ChatNodes" in client.get("/").text
    assert client.get("/static/app.js").status_code == 200


# --- laboratorio de codigo --------------------------------------------------


def test_laboratorio_detecta_python(client):
    payload = client.get("/api/runner/languages").json()
    assert any(language["id"] == "python" for language in payload["languages"])
    assert payload["limits"]["timeout_seconds"] == 10


def test_laboratorio_ejecuta_codigo_con_entrada(client):
    response = client.post(
        "/api/runner/run",
        json={
            "language": "python",
            "code": "a = int(input())\nprint(a * 2)",
            "stdin": "21\n",
        },
    )
    payload = response.json()
    assert response.status_code == 200
    assert payload["phase"] == "run"
    assert payload["exit_code"] == 0
    assert payload["stdout"].strip() == "42"


def test_laboratorio_devuelve_errores_de_codigo(client):
    response = client.post(
        "/api/runner/run",
        json={"language": "python", "code": "raise ValueError('fallo controlado')"},
    )
    payload = response.json()
    assert response.status_code == 200
    assert payload["exit_code"] != 0
    assert "fallo controlado" in payload["stderr"]


def test_laboratorio_rechaza_codigo_vacio_y_lenguaje_ajeno(client):
    assert client.post(
        "/api/runner/run", json={"language": "python", "code": "  "}
    ).status_code == 400
    assert client.post(
        "/api/runner/run", json={"language": "cobol-inexistente", "code": "DISPLAY 1"}
    ).status_code == 400


def test_laboratorio_ejecuta_clips_si_esta_disponible(client):
    languages = {item["id"] for item in client.get("/api/runner/languages").json()["languages"]}
    if "clips" not in languages:
        pytest.skip("CLIPS no esta instalado")
    code = """(deffacts inicio (numero 7))
(defrule doble (numero ?n) => (printout t (* ?n 2) crlf))"""
    payload = client.post(
        "/api/runner/run", json={"language": "clips", "code": code}
    ).json()
    assert payload["exit_code"] == 0
    assert payload["stdout"].strip() == "14"


def test_laboratorio_ejecuta_prolog_si_esta_disponible(client):
    languages = {item["id"] for item in client.get("/api/runner/languages").json()["languages"]}
    if "prolog" not in languages:
        pytest.skip("SWI-Prolog no esta instalado")
    code = "main :- X is 6 * 7, format('~w~n', [X])."
    payload = client.post(
        "/api/runner/run", json={"language": "prolog", "code": code}
    ).json()
    assert payload["exit_code"] == 0
    assert payload["stdout"].strip() == "42"


# --- fork de sesiones -------------------------------------------------------


def test_nodo_sobre_la_ultima_respuesta_forkea_la_sesion(client, engine):
    """Caso habitual: el contexto ya esta en la sesion, no se reenvia."""
    project = make_project(client)
    chat = make_chat(client, project["id"])
    send(client, chat["id"], "explicame el reloj")
    anchor = last_assistant(client, chat["id"])
    sesion_chat = client.get(f"/api/chats/{chat['id']}").json()["chat"]["session_id"]

    open_node(client, chat["id"], anchor["id"], "respuesta", "que significa")

    call = engine.calls[-1]
    assert call["fork"] is True
    assert call["resume"] == sesion_chat
    assert "CONVERSACION PRINCIPAL" not in call["prompt"]
    assert "que significa" in call["prompt"]
    # y el id del nodo es el nuevo que devuelve el fork, no el del hilo
    nodo = client.get(f"/api/chats/{chat['id']}").json()["nodes"][0]
    assert nodo["session_id"] != sesion_chat
    assert client.get(f"/api/chats/{chat['id']}").json()["chat"]["session_id"] == sesion_chat


def test_nodo_sobre_un_mensaje_antiguo_redacta_el_hilo(client, engine):
    """Si el ancla no es la punta del hilo, forkear traeria contexto de mas."""
    project = make_project(client)
    chat = make_chat(client, project["id"])
    engine.reply = "la CPU va a 3 GHz"
    send(client, chat["id"], "explicame el reloj")
    anchor = last_assistant(client, chat["id"])
    engine.reply = "ahora las interrupciones"
    send(client, chat["id"], "sigue")

    open_node(client, chat["id"], anchor["id"], "3 GHz", "que significa")

    call = engine.calls[-1]
    assert call["fork"] is False
    assert call["resume"] is None
    assert "CONVERSACION PRINCIPAL" in call["prompt"]
    assert "interrupciones" not in call["prompt"]


def test_nodo_anidado_no_punta_arrastra_la_cadena_de_padres(client, engine):
    project = make_project(client)
    chat = make_chat(client, project["id"])
    engine.reply = "la CPU va a 3 GHz"
    send(client, chat["id"], "explicame el reloj")
    anchor = last_assistant(client, chat["id"])

    engine.reply = "un ciclo es la unidad minima"
    padre = open_node(client, chat["id"], anchor["id"], "3 GHz", "que es eso")[0]["node"]
    dentro = [
        m for m in client.get(f"/api/chats/{chat['id']}").json()["nodes"][0]["messages"]
        if m["role"] == "assistant"
    ][-1]
    # otro turno en el nodo: el ancla deja de ser la punta de esa rama
    engine.reply = "mas detalle"
    client.post(f"/api/nodes/{padre['id']}/turn", json={"text": "y ademas?"})

    open_node(client, chat["id"], dentro["id"], "unidad minima", "explicame eso", parent=padre["id"])

    prompt = engine.calls[-1]["prompt"]
    assert engine.calls[-1]["fork"] is False
    assert "ACLARACION PREVIA" in prompt
    assert "un ciclo es la unidad minima" in prompt
    assert "la CPU va a 3 GHz" in prompt  # el hilo principal sigue presente


# --- apuntes y repaso -------------------------------------------------------


def test_exportar_devuelve_markdown_descargable(client):
    project = make_project(client)
    chat = make_chat(client, project["id"])
    send(client, chat["id"], "explicame el reloj")
    anchor = last_assistant(client, chat["id"])
    open_node(client, chat["id"], anchor["id"], "respuesta", "que significa")

    response = client.get(f"/api/chats/{chat['id']}/export")

    assert response.status_code == 200
    assert "text/markdown" in response.headers["content-type"]
    assert ".md" in response.headers["content-disposition"]
    assert "# explicame el reloj" in response.text
    assert "Aclaración 1" in response.text
    assert "que significa" in response.text


def test_repaso_crea_un_chat_nuevo_con_las_dudas(client):
    project = make_project(client)
    chat = make_chat(client, project["id"])
    send(client, chat["id"], "explicame el reloj")
    anchor = last_assistant(client, chat["id"])
    open_node(client, chat["id"], anchor["id"], "respuesta", "no entiendo los ciclos")

    payload = client.post(f"/api/chats/{chat['id']}/review").json()

    assert payload["chat"]["title"].startswith("Repaso:")
    assert payload["chat"]["id"] != chat["id"]
    assert "no entiendo los ciclos" in payload["prompt"]
    assert "«respuesta»" in payload["prompt"]
    titulos = [c["title"] for c in client.get(f"/api/projects/{project['id']}/chats").json()["chats"]]
    assert any(t.startswith("Repaso:") for t in titulos)


def test_repaso_sin_aclaraciones_se_rechaza(client):
    project = make_project(client)
    chat = make_chat(client, project["id"])
    send(client, chat["id"], "explicame el reloj")
    assert client.post(f"/api/chats/{chat['id']}/review").status_code == 400


def test_modo_estudio_crea_un_chat_independiente(client):
    project = make_project(client)
    chat = make_chat(client, project["id"])
    send(client, chat["id"], "explicame la memoria virtual")

    response = client.post(
        f"/api/chats/{chat['id']}/study", json={"mode": "quiz", "amount": 5}
    )
    payload = response.json()

    assert response.status_code == 200
    assert payload["chat"]["id"] != chat["id"]
    assert payload["chat"]["title"].startswith("Quiz:")
    assert "5 preguntas" in payload["prompt"]
    assert "memoria virtual" in payload["prompt"]


def test_modo_estudio_valida_tipo_y_cantidad(client):
    project = make_project(client)
    chat = make_chat(client, project["id"])
    assert client.post(
        f"/api/chats/{chat['id']}/study", json={"mode": "trampa", "amount": 10}
    ).status_code == 400
    assert client.post(
        f"/api/chats/{chat['id']}/study", json={"mode": "exam", "amount": 99}
    ).status_code == 400
