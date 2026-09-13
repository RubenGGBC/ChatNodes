"""Construccion de los prompts que se envian al CLI de Claude Code.

Funciones puras: reciben datos ya cargados del store y devuelven texto. Toda la
politica de "que contexto ve cada hilo" vive aqui.
"""

from __future__ import annotations

from typing import Any, Iterable, Mapping, Sequence

FALLBACK_QUESTION = "Explicame este fragmento, no lo he entendido."

_BASE_ROLE = """Eres el tutor personal de un estudiante universitario dentro de ChatNodes,
una aplicacion de estudio. El estudiante te da el material de su asignatura y tu se lo explicas.

Proyecto: {project_name}
"""

_FILES_WITH = """
Material del proyecto, en tu directorio de trabajo actual. Leelo con la herramienta Read
cuando lo necesites. Analiza tambien el texto visible en PDFs e imagenes, incluidos diagramas,
capturas, documentos escaneados y formulas:
{file_list}
"""

_FILES_WITHOUT = """
El proyecto todavia no tiene material subido. Si el estudiante se refiere a apuntes que no
encuentras, dilo en lugar de inventarte el contenido.
"""

_MAIN_RULES = """
Como trabajas en el hilo principal:
- Explicas como un buen profesor: de lo concreto a lo abstracto, con ejemplos numericos
  cuando el tema lo permita.
- Escribes en espanol y en Markdown, con parrafos cortos y bien separados. El estudiante
  selecciona parrafos sueltos para pedir aclaraciones al margen, asi que cada parrafo debe
  ser una unidad de sentido cerrada.
- Avanzas de forma ordenada por el material y te detienes donde toque para no soltar un muro
  de texto.
- No te inventas contenido que no este en el material: si algo no aparece, lo dices.
- No modificas ficheros ni ejecutas codigo. Solo lees el material.
"""

_NODE_RULES = """
Estas atendiendo una ACLARACION AL MARGEN, no el hilo principal:
- Resuelves una duda puntual sobre un fragmento concreto que el estudiante ha seleccionado.
- Vas al grano. No repites la explicacion anterior ni resumes el contexto que ya tienes.
- Prefieres el ejemplo concreto a la definicion abstracta.
- Eres breve: es una nota al margen. Si la duda es pequena, la respuesta tambien.
- Escribes en espanol y en Markdown, con parrafos cortos, porque el estudiante tambien puede
  seleccionar texto de tu respuesta para abrir otra aclaracion.
"""


def build_system_prompt(
    project: Mapping[str, Any],
    files: Sequence[str],
    kind: str = "main",
) -> str:
    """Prompt de sistema que se anade al del CLI, distinto para hilo y para nodo."""
    parts = [_BASE_ROLE.format(project_name=project.get("name") or "sin nombre")]
    if files:
        listing = "\n".join(f"- {name}" for name in files)
        parts.append(_FILES_WITH.format(file_list=listing))
    else:
        parts.append(_FILES_WITHOUT)
    parts.append(_NODE_RULES if kind == "node" else _MAIN_RULES)
    instructions = (project.get("instructions") or "").strip()
    if instructions:
        parts.append(
            "\nInstrucciones adicionales del estudiante para este proyecto:\n" + instructions + "\n"
        )
    return "".join(parts)


def _label(role: str) -> str:
    return "ESTUDIANTE" if role == "user" else "TU RESPUESTA"


def render_transcript(messages: Iterable[Mapping[str, Any]]) -> str:
    """Convierte una lista de mensajes en texto plano etiquetado."""
    blocks: list[str] = []
    for message in messages:
        content = (message.get("content") or "").strip()
        if not content:
            continue
        blocks.append(f"[{_label(message.get('role', 'user'))}]\n{content}")
    return "\n\n".join(blocks)


def _fence(text: str) -> str:
    """Envuelve un fragmento en delimitadores que no colisionan con su contenido."""
    marker = "~~~"
    while marker in text:
        marker += "~"
    return f"{marker}\n{text.strip()}\n{marker}"


def build_node_opening_prompt(
    *,
    main_messages: Sequence[Mapping[str, Any]],
    ancestors: Sequence[tuple[Mapping[str, Any], Sequence[Mapping[str, Any]]]],
    anchor_text: str,
    question: str,
    anchor_in_node: bool = False,
) -> str:
    """Primer prompt de un nodo de aclaracion.

    ``main_messages`` es el hilo principal recortado hasta el mensaje anclado.
    ``ancestors`` son los nodos padre (del mas lejano al directo) con sus mensajes.
    """
    question = (question or "").strip() or FALLBACK_QUESTION
    sections: list[str] = [
        "El estudiante esta leyendo una explicacion y ha seleccionado un fragmento que no "
        "entiende. Abre una aclaracion al margen sobre ese fragmento.",
        "",
        "=== CONVERSACION PRINCIPAL HASTA ESE PUNTO (contexto; NO la continues) ===",
        render_transcript(main_messages) or "(todavia no hay conversacion principal)",
        "=== FIN DE LA CONVERSACION PRINCIPAL ===",
    ]

    for node, messages in ancestors:
        transcript = render_transcript(messages)
        if not transcript:
            continue
        sections += [
            "",
            "=== ACLARACION PREVIA sobre el fragmento "
            f"<<{(node.get('anchor_text') or '').strip()}>> ===",
            transcript,
            "=== FIN DE LA ACLARACION PREVIA ===",
        ]

    origen = "de esa aclaracion previa" if anchor_in_node else "de tu ultima explicacion"
    sections += [
        "",
        f"FRAGMENTO SELECCIONADO {origen}:",
        _fence(anchor_text),
        "",
        "DUDA DEL ESTUDIANTE:",
        _fence(question),
        "",
        "Responde solo a esta duda, centrada en el fragmento seleccionado.",
    ]
    return "\n".join(sections)


def build_node_fork_prompt(*, anchor_text: str, question: str) -> str:
    """Primer prompt de un nodo que hereda la sesion del hilo del que nace.

    No hace falta redactar el contexto: la sesion forkeada ya lo trae, y asi se
    reaprovecha su cache en lugar de reenviar toda la conversacion.
    """
    question = (question or "").strip() or FALLBACK_QUESTION
    return "\n".join(
        [
            "Pausa el hilo. El estudiante ha seleccionado un fragmento de tu ultima "
            "explicacion porque no lo ha entendido y abre una aclaracion al margen.",
            "",
            "FRAGMENTO SELECCIONADO:",
            _fence(anchor_text),
            "",
            "DUDA DEL ESTUDIANTE:",
            _fence(question),
            "",
            "Responde solo a esta duda, centrada en el fragmento. No sigas con la "
            "explicacion que llevabas ni resumas lo anterior.",
        ]
    )


def derive_title(question: str, anchor_text: str = "", limit: int = 70) -> str:
    """Titulo corto para la tarjeta del nodo."""
    source = (question or "").strip() or (anchor_text or "").strip() or "Aclaracion"
    source = " ".join(source.split())
    if len(source) <= limit:
        return source
    return source[: limit - 1].rstrip() + "…"


def build_study_prompt(
    *,
    mode: str,
    amount: int,
    messages: Sequence[Mapping[str, Any]],
    nodes: Sequence[Mapping[str, Any]],
) -> str:
    """Crea una actividad de aprendizaje a partir del chat y sus dudas."""
    transcript = render_transcript(messages)
    doubts = "\n".join(
        f"- {(node.get('anchor_text') or '').strip()}: "
        f"{next((m.get('content', '') for m in node.get('messages', []) if m.get('role') == 'user'), '')}"
        for node in nodes
    )
    source = "\n\n".join(
        part for part in (
            "CONVERSACION DE ORIGEN:\n" + transcript if transcript else "",
            "DUDAS QUE TUVO EL ESTUDIANTE:\n" + doubts if doubts else "",
        ) if part
    ) or "Usa el material disponible en la carpeta del proyecto."

    instructions = {
        "flashcards": (
            f"Crea {amount} tarjetas de estudio. Para cada una escribe exactamente un titulo "
            "'Tarjeta N', una linea '**Pregunta:**' y otra '**Respuesta:**'. Prioriza ideas "
            "fundamentales y las dudas detectadas; respuestas breves y memorizables."
        ),
        "quiz": (
            f"Haz un cuestionario interactivo de {amount} preguntas. Formula SOLO la primera "
            "pregunta ahora, con cuatro opciones A-D y sin revelar la respuesta. Tras cada "
            "contestacion, explica brevemente el resultado, registra el marcador y presenta la siguiente."
        ),
        "exam": (
            f"Prepara un examen de {amount} preguntas variadas y numeradas, sin soluciones ni pistas. "
            "Espera a que el estudiante entregue todas sus respuestas; entonces corrige con una nota "
            "sobre 10, explica los errores y propone que conceptos repasar."
        ),
    }
    return (
        "Inicia una sesion de estudio independiente basada exclusivamente en el material y contexto "
        "siguientes. No inventes datos que no aparezcan en ellos.\n\n"
        + source
        + "\n\nACTIVIDAD:\n"
        + instructions[mode]
    )
