from __future__ import annotations

import pytest

from chatnodes.store import NotFound


def test_mensajes_del_hilo_principal_y_de_nodo_no_se_mezclan(store, chat):
    store.add_message(chat["id"], "user", "explicame el tema 1")
    store.add_message(chat["id"], "assistant", "El reloj de la CPU...")
    node = store.create_node(chat["id"], "m1", 0, 5, "reloj")
    store.add_message(chat["id"], "user", "que es un ciclo", node_id=node["id"])

    principal = store.thread_messages(chat["id"], None)
    rama = store.thread_messages(chat["id"], node["id"])

    assert [m["content"] for m in principal] == ["explicame el tema 1", "El reloj de la CPU..."]
    assert [m["content"] for m in rama] == ["que es un ciclo"]


def test_seq_es_independiente_por_hilo(store, chat):
    node = store.create_node(chat["id"], "m1", 0, 1, "x")
    a = store.add_message(chat["id"], "user", "uno")
    b = store.add_message(chat["id"], "user", "dos", node_id=node["id"])
    assert a["seq"] == 1
    assert b["seq"] == 1


def test_main_messages_upto_recorta_en_el_mensaje_anclado(store, chat):
    primero = store.add_message(chat["id"], "user", "hola")
    segundo = store.add_message(chat["id"], "assistant", "primera diapositiva")
    store.add_message(chat["id"], "user", "sigue")
    store.add_message(chat["id"], "assistant", "segunda diapositiva")

    recorte = store.main_messages_upto(chat["id"], segundo["id"])

    assert [m["id"] for m in recorte] == [primero["id"], segundo["id"]]


def test_main_messages_upto_con_ancla_de_rama_devuelve_todo(store, chat):
    store.add_message(chat["id"], "user", "hola")
    store.add_message(chat["id"], "assistant", "respuesta")
    node = store.create_node(chat["id"], "m1", 0, 1, "x")
    dentro = store.add_message(chat["id"], "assistant", "aclaracion", node_id=node["id"])

    recorte = store.main_messages_upto(chat["id"], dentro["id"])

    assert len(recorte) == 2


def test_node_chain_ordena_del_mas_lejano_al_padre(store, chat):
    abuelo = store.create_node(chat["id"], "m1", 0, 3, "abc")
    padre = store.create_node(chat["id"], "m2", 0, 3, "def", parent_node_id=abuelo["id"])
    hijo = store.create_node(chat["id"], "m3", 0, 3, "ghi", parent_node_id=padre["id"])

    cadena = store.node_chain(hijo["id"])

    assert [n["id"] for n in cadena] == [abuelo["id"], padre["id"]]
    assert store.node_chain(abuelo["id"]) == []


def test_delete_node_arrastra_descendientes_y_sus_mensajes(store, chat):
    raiz = store.create_node(chat["id"], "m1", 0, 3, "abc")
    hijo = store.create_node(chat["id"], "m2", 0, 3, "def", parent_node_id=raiz["id"])
    nieto = store.create_node(chat["id"], "m3", 0, 3, "ghi", parent_node_id=hijo["id"])
    otro = store.create_node(chat["id"], "m4", 0, 3, "jkl")
    store.add_message(chat["id"], "user", "duda", node_id=nieto["id"])
    store.add_message(chat["id"], "user", "otra duda", node_id=otro["id"])

    borrados = store.delete_node(raiz["id"])

    assert set(borrados) == {raiz["id"], hijo["id"], nieto["id"]}
    assert [n["id"] for n in store.list_nodes(chat["id"])] == [otro["id"]]
    assert store.thread_messages(chat["id"], nieto["id"]) == []
    assert len(store.thread_messages(chat["id"], otro["id"])) == 1


def test_update_node_persiste_estado_visual(store, chat):
    node = store.create_node(chat["id"], "m1", 0, 3, "abc")
    actualizado = store.update_node(node["id"], collapsed=1, width=420, height=300)
    assert (actualizado["collapsed"], actualizado["width"], actualizado["height"]) == (1, 420, 300)


def test_update_ignora_campos_no_permitidos(store, chat):
    node = store.create_node(chat["id"], "m1", 0, 3, "abc")
    actualizado = store.update_node(node["id"], chat_id="otro", title="titulo")
    assert actualizado["chat_id"] == chat["id"]
    assert actualizado["title"] == "titulo"


def test_material_del_chat_puede_ser_todo_un_subconjunto_o_nada(store, chat):
    assert store.chat_file_context(chat["id"]) is None
    assert store.set_chat_file_context(chat["id"], ["tema.pdf", "anexo.png"]) == [
        "tema.pdf", "anexo.png"
    ]
    assert store.chat_file_context(chat["id"]) == ["tema.pdf", "anexo.png"]
    assert store.set_chat_file_context(chat["id"], []) == []
    assert store.set_chat_file_context(chat["id"], None) is None


def test_borrar_chat_limpia_mensajes_y_nodos(store, project, chat):
    node = store.create_node(chat["id"], "m1", 0, 3, "abc")
    store.add_message(chat["id"], "user", "hola")
    store.add_message(chat["id"], "user", "duda", node_id=node["id"])

    store.delete_chat(chat["id"])

    assert store.list_chats(project["id"]) == []
    assert store.all_messages(chat["id"]) == []
    assert store.list_nodes(chat["id"]) == []


def test_entidad_inexistente_lanza_notfound(store):
    with pytest.raises(NotFound):
        store.get_node("no-existe")
