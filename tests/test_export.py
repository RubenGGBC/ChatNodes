from __future__ import annotations

from chatnodes import export

PROJECT = {"name": "Sistemas Operativos"}
CHAT = {"title": "Tema 1"}


def message(ident, role, content):
    return {"id": ident, "role": role, "content": content}


def node(ident, anchor_message_id, anchor_text, parent=None, messages=()):
    return {
        "id": ident,
        "anchor_message_id": anchor_message_id,
        "anchor_text": anchor_text,
        "parent_node_id": parent,
        "messages": list(messages),
    }


def test_las_aclaraciones_salen_tras_el_mensaje_que_anclan():
    markdown = export.chat_to_markdown(
        project=PROJECT,
        chat=CHAT,
        messages=[
            message("m1", "user", "explicame el reloj"),
            message("m2", "assistant", "la CPU va a 3 GHz"),
            message("m3", "assistant", "y ahora las interrupciones"),
        ],
        nodes=[
            node(
                "n1", "m2", "3 GHz",
                messages=[
                    message("x1", "user", "que significa eso"),
                    message("x2", "assistant", "ciclos por segundo"),
                ],
            )
        ],
    )

    posicion_reloj = markdown.index("la CPU va a 3 GHz")
    posicion_nodo = markdown.index("ciclos por segundo")
    posicion_siguiente = markdown.index("las interrupciones")
    assert posicion_reloj < posicion_nodo < posicion_siguiente
    assert "Aclaración 1" in markdown
    assert "«3 GHz»" in markdown
    assert "*Duda:* que significa eso" in markdown


def test_los_nodos_anidados_se_indentan_y_se_numeran():
    markdown = export.chat_to_markdown(
        project=PROJECT,
        chat=CHAT,
        messages=[message("m1", "assistant", "explicacion")],
        nodes=[
            node("n1", "m1", "algo", messages=[message("a", "assistant", "nivel uno")]),
            node("n2", "x1", "otra cosa", parent="n1", messages=[message("b", "assistant", "nivel dos")]),
        ],
    )

    assert "> **Aclaración 1**" in markdown
    assert "> > **Aclaración 1.1**" in markdown
    assert "> nivel uno" in markdown
    assert "> > nivel dos" in markdown


def test_los_nodos_sin_ancla_no_se_pierden():
    markdown = export.chat_to_markdown(
        project=PROJECT,
        chat=CHAT,
        messages=[message("m1", "assistant", "explicacion")],
        nodes=[node("n1", "borrado", "texto que ya no existe",
                    messages=[message("a", "assistant", "sigo aqui")])],
    )

    assert "Aclaraciones sin ancla" in markdown
    assert "sigo aqui" in markdown


def test_la_cabecera_nombra_proyecto_y_chat():
    markdown = export.chat_to_markdown(
        project=PROJECT, chat=CHAT, messages=[], nodes=[]
    )
    assert markdown.startswith("# Tema 1")
    assert "Sistemas Operativos" in markdown


def test_el_repaso_recoge_fragmentos_y_dudas():
    prompt = export.review_prompt(
        CHAT,
        [
            node("n1", "m1", "3 GHz", messages=[message("a", "user", "que significa")]),
            node("n2", "m1", "CPI", messages=[message("b", "user", "y esto")]),
        ],
    )

    assert "Tema 1" in prompt
    assert "«3 GHz»" in prompt
    assert "que significa" in prompt
    assert "«CPI»" in prompt
    assert "espera mi respuesta" in prompt


def test_los_fragmentos_largos_se_recortan():
    largo = "palabra " * 40
    prompt = export.review_prompt(CHAT, [node("n1", "m1", largo, messages=[])])
    linea = [l for l in prompt.splitlines() if "Fragmento" in l][0]
    assert len(linea) < 200
    assert "…" in linea
