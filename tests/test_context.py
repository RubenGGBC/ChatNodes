from __future__ import annotations

from chatnodes import context


def msg(role, content):
    return {"role": role, "content": content}


def test_transcript_etiqueta_y_salta_los_vacios():
    rendered = context.render_transcript(
        [msg("user", "explicame"), msg("assistant", ""), msg("assistant", "el reloj")]
    )
    assert rendered == "[ESTUDIANTE]\nexplicame\n\n[TU RESPUESTA]\nel reloj"


def test_prompt_de_nodo_incluye_solo_el_contexto_previo():
    prompt = context.build_node_opening_prompt(
        main_messages=[msg("user", "explicame el tema 1"), msg("assistant", "la CPU va a 3 GHz")],
        ancestors=[],
        anchor_text="3 GHz",
        question="que significan los gigahercios",
    )

    assert "explicame el tema 1" in prompt
    assert "la CPU va a 3 GHz" in prompt
    assert "3 GHz" in prompt
    assert "que significan los gigahercios" in prompt
    assert "NO la continues" in prompt


def test_prompt_de_nodo_no_arrastra_mensajes_posteriores():
    # El servidor recorta el hilo; aqui se comprueba que el prompt no anade nada
    # que no se le haya pasado.
    prompt = context.build_node_opening_prompt(
        main_messages=[msg("assistant", "primera diapositiva")],
        ancestors=[],
        anchor_text="primera",
        question="y esto?",
    )
    assert "segunda diapositiva" not in prompt


def test_prompt_de_nodo_anidado_incluye_la_cadena_de_padres():
    padre = {"anchor_text": "3 GHz"}
    prompt = context.build_node_opening_prompt(
        main_messages=[msg("assistant", "la CPU va a 3 GHz")],
        ancestors=[(padre, [msg("user", "que son"), msg("assistant", "ciclos por segundo")])],
        anchor_text="ciclos",
        question="y un ciclo que es",
        anchor_in_node=True,
    )

    assert "ACLARACION PREVIA" in prompt
    assert "ciclos por segundo" in prompt
    assert "de esa aclaracion previa" in prompt


def test_pregunta_vacia_usa_el_texto_por_defecto():
    prompt = context.build_node_opening_prompt(
        main_messages=[], ancestors=[], anchor_text="algo", question="   "
    )
    assert context.FALLBACK_QUESTION in prompt


def test_fragmento_con_tildes_de_cierre_no_rompe_el_delimitador():
    prompt = context.build_node_opening_prompt(
        main_messages=[],
        ancestors=[],
        anchor_text="usa ~~~ como separador",
        question="?",
    )
    # El delimitador crece hasta no aparecer dentro del propio fragmento.
    assert "~~~~\nusa ~~~ como separador\n~~~~" in prompt


def test_system_prompt_lista_el_material_y_las_instrucciones():
    prompt = context.build_system_prompt(
        {"name": "Sistemas Operativos", "instructions": "soy de segundo"},
        ["tema1.pdf", "tema2.pdf"],
    )
    assert "Sistemas Operativos" in prompt
    assert "- tema1.pdf" in prompt
    assert "soy de segundo" in prompt
    assert "hilo principal" in prompt


def test_system_prompt_de_nodo_pide_brevedad():
    prompt = context.build_system_prompt({"name": "SO"}, [], kind="node")
    assert "ACLARACION AL MARGEN" in prompt
    assert "breve" in prompt


def test_system_prompt_sin_material_lo_advierte():
    prompt = context.build_system_prompt({"name": "SO"}, [])
    assert "todavia no tiene material" in prompt


def test_derive_title_recorta_y_cae_al_ancla():
    assert context.derive_title("que es un ciclo") == "que es un ciclo"
    assert context.derive_title("", "3 GHz de reloj") == "3 GHz de reloj"
    largo = context.derive_title("palabra " * 30)
    assert len(largo) <= 70 and largo.endswith("…")
