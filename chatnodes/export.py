"""Exportar un chat a apuntes en Markdown, con las aclaraciones en su sitio."""

from __future__ import annotations

from typing import Any, Mapping, Sequence

MAX_QUOTE = 90


def _short(text: str, limit: int = MAX_QUOTE) -> str:
    clean = " ".join((text or "").split())
    return clean if len(clean) <= limit else clean[: limit - 1].rstrip() + "…"


def _prefix(lines: Sequence[str], depth: int) -> list[str]:
    """Indenta un bloque como cita anidada, segun la profundidad del nodo."""
    mark = "> " * depth
    return [(mark + line).rstrip() for line in lines]


def _node_block(
    node: Mapping[str, Any],
    children: Mapping[str | None, list[Mapping[str, Any]]],
    numbering: str,
    depth: int,
) -> list[str]:
    lines = [
        f"**Aclaración {numbering}** — sobre «{_short(node.get('anchor_text', ''))}»",
        "",
    ]
    for message in node.get("messages", []):
        content = (message.get("content") or "").strip()
        if not content:
            continue
        if message.get("role") == "user":
            lines += [f"*Duda:* {content}", ""]
        else:
            lines += content.splitlines() + [""]
        if message.get("error"):
            lines += [f"*(error: {message['error']})*", ""]

    out = _prefix(lines, depth)
    for index, child in enumerate(children.get(node["id"], []), start=1):
        out += [""] + _node_block(child, children, f"{numbering}.{index}", depth + 1)
    return out


def chat_to_markdown(
    *,
    project: Mapping[str, Any],
    chat: Mapping[str, Any],
    messages: Sequence[Mapping[str, Any]],
    nodes: Sequence[Mapping[str, Any]],
) -> str:
    """Hilo principal en orden, con los nodos colgando del mensaje que anclan."""
    children: dict[str | None, list[Mapping[str, Any]]] = {}
    for node in nodes:
        children.setdefault(node.get("parent_node_id"), []).append(node)

    roots_by_message: dict[str, list[Mapping[str, Any]]] = {}
    for node in children.get(None, []):
        roots_by_message.setdefault(node["anchor_message_id"], []).append(node)

    lines = [
        f"# {chat.get('title') or 'Chat'}",
        "",
        f"Proyecto: **{project.get('name', '')}** · exportado desde ChatNodes",
        "",
    ]

    counter = 0
    for message in messages:
        content = (message.get("content") or "").strip()
        if content:
            lines += ["---", ""]
            lines.append("## Pregunta" if message.get("role") == "user" else "## Explicación")
            lines += ["", *content.splitlines(), ""]
        for node in roots_by_message.get(message["id"], []):
            counter += 1
            lines += _node_block(node, children, str(counter), 1) + [""]

    # Nodos cuyo mensaje ancla ya no existe: no se pierden, van al final.
    conocidos = {m["id"] for m in messages}
    huerfanos = [n for n in children.get(None, []) if n["anchor_message_id"] not in conocidos]
    if huerfanos:
        lines += ["---", "", "## Aclaraciones sin ancla", ""]
        for node in huerfanos:
            counter += 1
            lines += _node_block(node, children, str(counter), 1) + [""]

    return "\n".join(lines).rstrip() + "\n"


def review_prompt(chat: Mapping[str, Any], nodes: Sequence[Mapping[str, Any]]) -> str:
    """Prompt de repaso construido con lo que el estudiante no entendio."""
    puntos: list[str] = []
    for index, node in enumerate(nodes, start=1):
        duda = next(
            (
                (m.get("content") or "").strip()
                for m in node.get("messages", [])
                if m.get("role") == "user"
            ),
            "",
        )
        puntos.append(
            f"{index}. Fragmento: «{_short(node.get('anchor_text', ''), 160)}»\n"
            f"   Mi duda fue: {duda or '(sin texto)'}"
        )

    return "\n".join(
        [
            f'Estudiando "{chat.get("title", "")}" hubo estos puntos que no entendi a la '
            "primera, y que por tanto son mis puntos flojos:",
            "",
            *puntos,
            "",
            "Hazme un repaso centrado exactamente en esos puntos, en este orden:",
            "",
            "1. Para cada punto, una pregunta corta que compruebe si ya lo entiendo "
            "(de test o de calculo, la que encaje mejor).",
            "2. Pon todas las preguntas juntas primero y espera mi respuesta antes de "
            "corregir: quiero intentarlo.",
            "3. No repitas las explicaciones que ya me diste.",
        ]
    )
