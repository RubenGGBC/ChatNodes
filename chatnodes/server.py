"""API HTTP local, turnos en segundo plano y streaming por SSE."""

from __future__ import annotations

import asyncio
import json
import mimetypes
import os
import subprocess
import sys
from pathlib import Path
from typing import Any, AsyncIterator

from fastapi import FastAPI, HTTPException, UploadFile
from fastapi.responses import FileResponse, PlainTextResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import config, context, export, runner
from .engine import ClaudeEngine, EngineError, new_session_id
from .store import NotFound, Store

# Cada cuantos fragmentos de texto se persiste el parcial, para que una respuesta
# larga no dependa de llegar al final.
FLUSH_EVERY = 40


class ProjectIn(BaseModel):
    name: str = "Nuevo proyecto"


class ProjectPatch(BaseModel):
    name: str | None = None
    model: str | None = None
    instructions: str | None = None


class ChatIn(BaseModel):
    title: str = "Nuevo chat"


class ChatPatch(BaseModel):
    title: str | None = None


class ChatFilesIn(BaseModel):
    names: list[str] | None = None


class StudyIn(BaseModel):
    mode: str = "flashcards"
    amount: int = 10


class CodeRunIn(BaseModel):
    language: str = "python"
    code: str
    stdin: str = ""
    timeout: float = runner.DEFAULT_TIMEOUT


class TurnIn(BaseModel):
    text: str


class NodeIn(BaseModel):
    anchor_message_id: str
    anchor_start: int
    anchor_end: int
    anchor_text: str
    question: str = ""
    parent_node_id: str | None = None


class NodePatch(BaseModel):
    title: str | None = None
    collapsed: bool | None = None
    width: int | None = None
    height: int | None = None


def list_files(project: dict[str, Any]) -> list[dict[str, Any]]:
    folder = Path(project["folder"])
    if not folder.exists():
        return []
    out = []
    for entry in sorted(folder.iterdir(), key=lambda p: p.name.lower()):
        if entry.is_file() and not entry.name.startswith("."):
            suffix = entry.suffix.lower()
            if suffix == ".pdf":
                preview = "pdf"
            elif suffix in {".png", ".jpg", ".jpeg", ".gif", ".webp"}:
                preview = "image"
            elif suffix in {".md", ".txt", ".csv", ".json", ".tex", ".ipynb"}:
                preview = "text"
            else:
                preview = "download"
            out.append({
                "name": entry.name,
                "size": entry.stat().st_size,
                "suffix": suffix,
                "preview": preview,
            })
    return out


class TurnStream:
    """Un turno en vuelo.

    Vive en el servidor, no en la peticion HTTP: sigue escribiendo en la base de
    datos aunque el navegador se cierre, y quien vuelva puede reengancharse
    porque se guarda el historial de eventos.
    """

    def __init__(self, key: str, chat_id: str, node_id: str | None, message_id: str) -> None:
        self.key = key
        self.chat_id = chat_id
        self.node_id = node_id
        self.message_id = message_id
        self.events: list[dict] = []
        self.waiters: set[asyncio.Queue] = set()
        self.finished = False
        self.stopping = False
        self.process = None
        self.task = None

    def publish(self, event: dict) -> None:
        self.events.append(event)
        for queue in list(self.waiters):
            queue.put_nowait(None)

    def finish(self) -> None:
        self.finished = True
        for queue in list(self.waiters):
            queue.put_nowait(None)

    def stop(self) -> bool:
        """Mata el CLI. El bucle de lectura vera el fin del stream y cerrara."""
        self.stopping = True
        process = self.process
        if process is None or process.returncode is not None:
            return False
        try:
            process.kill()
        except Exception:
            return False
        return True

    async def subscribe(self) -> AsyncIterator[dict]:
        """La lista de eventos es la fuente de verdad; la cola solo despierta."""
        queue: asyncio.Queue = asyncio.Queue()
        self.waiters.add(queue)
        sent = 0
        try:
            while True:
                while sent < len(self.events):
                    yield self.events[sent]
                    sent += 1
                if self.finished:
                    return
                await queue.get()
        finally:
            self.waiters.discard(queue)


def sse(events: AsyncIterator[dict]) -> StreamingResponse:
    async def body() -> AsyncIterator[bytes]:
        try:
            async for event in events:
                yield f"data: {json.dumps(event, ensure_ascii=False)}\n\n".encode("utf-8")
        except Exception as exc:  # el cliente debe enterarse del fallo
            payload = {"t": "error", "message": f"{type(exc).__name__}: {exc}"}
            yield f"data: {json.dumps(payload, ensure_ascii=False)}\n\n".encode("utf-8")

    return StreamingResponse(
        body(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


def create_app(store: Store | None = None, engine: ClaudeEngine | None = None) -> FastAPI:
    app = FastAPI(title=config.APP_NAME)
    app.state.store = store or Store(config.db_path())
    app.state.engine = engine
    app.state.turns = {}

    def db() -> Store:
        return app.state.store

    def cli() -> ClaudeEngine:
        if app.state.engine is None:
            app.state.engine = ClaudeEngine()
        return app.state.engine

    def turns() -> dict[str, TurnStream]:
        return app.state.turns

    def fetch_project(pid: str) -> dict[str, Any]:
        try:
            return db().get_project(pid)
        except NotFound:
            raise HTTPException(404, "Proyecto no encontrado")

    def fetch_chat(cid: str) -> dict[str, Any]:
        try:
            return db().get_chat(cid)
        except NotFound:
            raise HTTPException(404, "Chat no encontrado")

    def fetch_node(nid: str) -> dict[str, Any]:
        try:
            return db().get_node(nid)
        except NotFound:
            raise HTTPException(404, "Nodo no encontrado")

    def live(key: str) -> TurnStream | None:
        stream = turns().get(key)
        return stream if stream is not None and not stream.finished else None

    def require_idle(key: str) -> None:
        if live(key) is not None:
            raise HTTPException(409, "Ese hilo ya tiene una respuesta en curso")

    def material_for_chat(chat: dict[str, Any], project: dict[str, Any]) -> list[dict[str, Any]]:
        available = list_files(project)
        selected = db().chat_file_context(chat["id"])
        if selected is None:
            return available
        allowed = set(selected)
        return [file for file in available if file["name"] in allowed]

    # --- turnos -----------------------------------------------------------

    def start_turn(
        *,
        project: dict[str, Any],
        chat: dict[str, Any],
        node: dict[str, Any] | None,
        prompt: str,
        assistant_id: str,
        first_event: dict,
        resume: str | None = None,
        fork: bool = False,
    ) -> TurnStream:
        """Lanza el turno como tarea de fondo y devuelve su stream."""
        key = node["id"] if node else chat["id"]
        stream = TurnStream(key, chat["id"], node["id"] if node else None, assistant_id)
        stream.publish(first_event)
        turns()[key] = stream

        session_id = None if resume else new_session_id()
        system_prompt = context.build_system_prompt(
            project,
            [f["name"] for f in material_for_chat(chat, project)],
            kind="node" if node else "main",
        )

        async def runner() -> None:
            buffer: list[str] = []
            pending = 0
            try:
                engine = cli()
            except EngineError as exc:
                db().update_message(assistant_id, error=str(exc))
                stream.publish({"t": "error", "message": str(exc)})
                stream.finish()
                return

            try:
                async for chunk in engine.stream(
                    prompt=prompt,
                    cwd=project["folder"],
                    session_id=session_id,
                    resume=resume,
                    fork=fork,
                    model=project.get("model") or config.DEFAULT_MODEL,
                    system_prompt=system_prompt,
                    on_start=lambda process: setattr(stream, "process", process),
                ):
                    if chunk.kind == "session":
                        # Con --fork-session el id es nuevo, asi que siempre se
                        # guarda el que devuelve el CLI.
                        if chunk.session_id:
                            if node:
                                db().update_node(node["id"], session_id=chunk.session_id)
                            else:
                                db().update_chat(chat["id"], session_id=chunk.session_id)
                        stream.publish({"t": "started"})
                    elif chunk.kind == "text":
                        buffer.append(chunk.text)
                        pending += 1
                        if pending >= FLUSH_EVERY:
                            pending = 0
                            db().update_message(assistant_id, content="".join(buffer))
                        stream.publish({"t": "delta", "text": chunk.text})
                    elif chunk.kind == "tool":
                        stream.publish({"t": "tool", "name": chunk.text})
                    elif chunk.kind == "limit":
                        stream.publish({"t": "limit", "usage": chunk.data})
                    elif chunk.kind == "error":
                        if stream.stopping:
                            break
                        db().update_message(
                            assistant_id, content="".join(buffer), error=chunk.text
                        )
                        stream.publish({"t": "error", "message": chunk.text})
                        return
                    elif chunk.kind == "done":
                        db().update_message(
                            assistant_id, content="".join(buffer), cost_usd=chunk.cost_usd
                        )
                        stream.publish(
                            {"t": "done", "message_id": assistant_id, "cost_usd": chunk.cost_usd}
                        )
                        return
            except Exception as exc:  # nunca dejar el turno colgado
                db().update_message(
                    assistant_id,
                    content="".join(buffer),
                    error=f"{type(exc).__name__}: {exc}",
                )
                stream.publish({"t": "error", "message": f"{type(exc).__name__}: {exc}"})
                return
            finally:
                text = "".join(buffer)
                if text:
                    db().update_message(assistant_id, content=text)
                if stream.stopping:
                    stream.publish({"t": "stopped", "message_id": assistant_id})
                stream.finish()

        # La referencia hay que guardarla: una tarea sin dueño puede irse con
        # el recolector de basura a mitad del turno.
        stream.task = asyncio.ensure_future(runner())
        return stream

    def anchor_is_thread_tip(
        chat_id: str, node_id: str | None, anchor_message_id: str
    ) -> bool:
        """¿El ancla es la ultima respuesta del hilo del que nace el nodo?

        Solo en ese caso se puede forkear la sesion: lo que el CLI trae en la
        sesion coincide entonces con el contexto que el nodo necesita.
        """
        messages = db().thread_messages(chat_id, node_id)
        respuestas = [m for m in messages if m["role"] == "assistant" and m["content"]]
        return bool(respuestas) and respuestas[-1]["id"] == anchor_message_id

    # --- proyectos --------------------------------------------------------

    @app.get("/api/projects")
    def get_projects() -> dict:
        return {"projects": db().list_projects()}

    @app.post("/api/projects")
    def post_project(payload: ProjectIn) -> dict:
        name = payload.name.strip() or "Nuevo proyecto"
        project = db().create_project(name, folder="", model=config.DEFAULT_MODEL)
        folder = config.projects_dir() / f"{config.slugify(name)}-{project['id'][:6]}"
        folder.mkdir(parents=True, exist_ok=True)
        # el folder se deriva del id, asi que se fija justo despues de crear la fila
        return {"project": db().update_project(project["id"], folder=str(folder))}

    @app.patch("/api/projects/{pid}")
    def patch_project(pid: str, payload: ProjectPatch) -> dict:
        fetch_project(pid)
        data = payload.model_dump(exclude_none=True)
        if "model" in data and data["model"] not in config.MODELS:
            raise HTTPException(400, "Modelo no valido")
        return {"project": db().update_project(pid, **data)}

    @app.delete("/api/projects/{pid}")
    def delete_project(pid: str) -> dict:
        project = fetch_project(pid)
        db().delete_project(pid)
        return {"ok": True, "folder": project["folder"]}

    @app.get("/api/projects/{pid}/files")
    def get_files(pid: str) -> dict:
        return {"files": list_files(fetch_project(pid))}

    @app.post("/api/projects/{pid}/files")
    async def post_files(pid: str, files: list[UploadFile]) -> dict:
        project = fetch_project(pid)
        folder = Path(project["folder"])
        folder.mkdir(parents=True, exist_ok=True)
        rejected: list[str] = []
        for upload in files:
            name = Path(upload.filename or "sin-nombre").name
            if Path(name).suffix.lower() not in config.ALLOWED_UPLOAD_SUFFIXES:
                rejected.append(name)
                continue
            target = folder / name
            stem, suffix = target.stem, target.suffix
            counter = 2
            while target.exists():
                target = folder / f"{stem}-{counter}{suffix}"
                counter += 1
            target.write_bytes(await upload.read())
        return {"files": list_files(project), "rejected": rejected}

    @app.delete("/api/projects/{pid}/files/{name}")
    def delete_file(pid: str, name: str) -> dict:
        project = fetch_project(pid)
        target = Path(project["folder"]) / Path(name).name
        if target.exists() and target.is_file():
            target.unlink()
        return {"files": list_files(project)}

    @app.get("/api/projects/{pid}/files/{name}/content")
    def get_file_content(pid: str, name: str) -> FileResponse:
        """Sirve un archivo del proyecto para la previsualizacion integrada."""
        project = fetch_project(pid)
        safe_name = Path(name).name
        target = Path(project["folder"]) / safe_name
        if not target.exists() or not target.is_file() or target.name.startswith("."):
            raise HTTPException(404, "Archivo no encontrado")
        if target.suffix.lower() not in config.ALLOWED_UPLOAD_SUFFIXES:
            raise HTTPException(400, "Formato no permitido")
        media_type = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
        return FileResponse(target, media_type=media_type)

    @app.post("/api/projects/{pid}/reveal")
    def reveal_folder(pid: str) -> dict:
        folder = fetch_project(pid)["folder"]
        try:
            if sys.platform == "win32":
                os.startfile(folder)  # noqa: S606
            elif sys.platform == "darwin":
                subprocess.Popen(["open", folder])
            else:
                subprocess.Popen(["xdg-open", folder])
        except Exception as exc:
            raise HTTPException(500, f"No se pudo abrir la carpeta: {exc}")
        return {"ok": True, "folder": folder}

    # --- chats ------------------------------------------------------------

    @app.get("/api/projects/{pid}/chats")
    def get_chats(pid: str) -> dict:
        fetch_project(pid)
        return {"chats": db().list_chats(pid)}

    @app.post("/api/projects/{pid}/chats")
    def post_chat(pid: str, payload: ChatIn) -> dict:
        fetch_project(pid)
        return {"chat": db().create_chat(pid, payload.title.strip() or "Nuevo chat")}

    @app.patch("/api/chats/{cid}")
    def patch_chat(cid: str, payload: ChatPatch) -> dict:
        fetch_chat(cid)
        return {"chat": db().update_chat(cid, **payload.model_dump(exclude_none=True))}

    @app.delete("/api/chats/{cid}")
    def delete_chat(cid: str) -> dict:
        fetch_chat(cid)
        db().delete_chat(cid)
        return {"ok": True}

    def chat_tree(cid: str) -> dict:
        chat = fetch_chat(cid)
        chat["selected_files"] = db().chat_file_context(cid)
        messages = db().all_messages(cid)
        nodes = db().list_nodes(cid)
        by_node: dict[str | None, list[dict]] = {}
        for message in messages:
            by_node.setdefault(message["node_id"], []).append(message)
        for node in nodes:
            node["messages"] = by_node.get(node["id"], [])
            node["collapsed"] = bool(node["collapsed"])
        return {"chat": chat, "messages": by_node.get(None, []), "nodes": nodes}

    @app.get("/api/chats/{cid}")
    def get_chat_tree(cid: str) -> dict:
        return chat_tree(cid)

    @app.get("/api/chats/{cid}/live")
    def get_live(cid: str) -> dict:
        """Turnos de este chat que siguen escribiendo ahora mismo."""
        fetch_chat(cid)
        activos = [
            {"key": s.key, "node_id": s.node_id, "message_id": s.message_id}
            for s in turns().values()
            if s.chat_id == cid and not s.finished
        ]
        return {"live": activos}

    @app.put("/api/chats/{cid}/files")
    def put_chat_files(cid: str, payload: ChatFilesIn) -> dict:
        chat = fetch_chat(cid)
        project = fetch_project(chat["project_id"])
        available = {file["name"] for file in list_files(project)}
        if payload.names is None:
            selected = db().set_chat_file_context(cid, None)
        else:
            invalid = [name for name in payload.names if name not in available]
            if invalid:
                raise HTTPException(400, f"Material no encontrado: {', '.join(invalid)}")
            selected = db().set_chat_file_context(cid, payload.names)
        return {"selected_files": selected, "available": sorted(available)}

    @app.get("/api/chats/{cid}/export")
    def get_export(cid: str) -> PlainTextResponse:
        tree = chat_tree(cid)
        project = fetch_project(tree["chat"]["project_id"])
        text = export.chat_to_markdown(
            project=project,
            chat=tree["chat"],
            messages=tree["messages"],
            nodes=tree["nodes"],
        )
        nombre = config.slugify(tree["chat"]["title"] or "chat") or "chat"
        return PlainTextResponse(
            text,
            media_type="text/markdown; charset=utf-8",
            headers={"Content-Disposition": f'attachment; filename="{nombre}.md"'},
        )

    @app.post("/api/chats/{cid}/review")
    def post_review(cid: str) -> dict:
        """Prepara un repaso con los puntos que hicieron falta aclarar."""
        tree = chat_tree(cid)
        if not tree["nodes"]:
            raise HTTPException(400, "Todavía no hay aclaraciones que repasar")
        prompt = export.review_prompt(tree["chat"], tree["nodes"])
        titulo = f"Repaso: {tree['chat']['title']}"[:80]
        nuevo = db().create_chat(tree["chat"]["project_id"], titulo)
        return {"chat": nuevo, "prompt": prompt}

    @app.post("/api/chats/{cid}/study")
    def post_study(cid: str, payload: StudyIn) -> dict:
        """Crea una sesion independiente de flashcards, quiz o examen."""
        if payload.mode not in {"flashcards", "quiz", "exam"}:
            raise HTTPException(400, "Modo de estudio no valido")
        if not 3 <= payload.amount <= 30:
            raise HTTPException(400, "El numero de preguntas debe estar entre 3 y 30")
        tree = chat_tree(cid)
        labels = {"flashcards": "Flashcards", "quiz": "Quiz", "exam": "Examen"}
        title = f"{labels[payload.mode]}: {tree['chat']['title']}"[:80]
        created = db().create_chat(tree["chat"]["project_id"], title)
        # La actividad hereda la seleccion de material, pero abre su propia sesion.
        db().set_chat_file_context(created["id"], tree["chat"]["selected_files"])
        prompt = context.build_study_prompt(
            mode=payload.mode,
            amount=payload.amount,
            messages=tree["messages"],
            nodes=tree["nodes"],
        )
        return {"chat": created, "prompt": prompt, "mode": payload.mode}

    @app.post("/api/chats/{cid}/turn")
    async def post_turn(cid: str, payload: TurnIn) -> StreamingResponse:
        chat = fetch_chat(cid)
        project = fetch_project(chat["project_id"])
        text = payload.text.strip()
        if not text:
            raise HTTPException(400, "Mensaje vacio")
        require_idle(cid)

        existing = db().thread_messages(cid, None)
        if not existing and (chat["title"] or "").startswith("Nuevo chat"):
            chat = db().update_chat(cid, title=context.derive_title(text, limit=40))

        user_message = db().add_message(cid, "user", text)
        assistant = db().add_message(cid, "assistant", "")
        stream = start_turn(
            project=project,
            chat=chat,
            node=None,
            prompt=text,
            assistant_id=assistant["id"],
            resume=chat.get("session_id"),
            first_event={
                "t": "accepted",
                "key": cid,
                "user_message": user_message,
                "assistant_message": assistant,
                "chat_title": chat["title"],
            },
        )
        return sse(stream.subscribe())

    # --- nodos ------------------------------------------------------------

    @app.post("/api/chats/{cid}/nodes")
    async def post_node(cid: str, payload: NodeIn) -> StreamingResponse:
        chat = fetch_chat(cid)
        project = fetch_project(chat["project_id"])
        anchor_text = payload.anchor_text.strip()
        if not anchor_text:
            raise HTTPException(400, "Hay que seleccionar texto para anclar el nodo")
        parent = fetch_node(payload.parent_node_id) if payload.parent_node_id else None

        question = payload.question.strip()
        node = db().create_node(
            chat_id=cid,
            anchor_message_id=payload.anchor_message_id,
            anchor_start=payload.anchor_start,
            anchor_end=payload.anchor_end,
            anchor_text=anchor_text,
            title=context.derive_title(question, anchor_text),
            parent_node_id=payload.parent_node_id,
        )

        # Si el ancla es la ultima respuesta del hilo del que nace, se forkea su
        # sesion: el contexto ya esta dentro y se reaprovecha su cache. Si no,
        # se redacta el hilo recortado hasta el mensaje anclado.
        dueño = parent or chat
        sesion_dueño = dueño.get("session_id")
        if sesion_dueño and anchor_is_thread_tip(
            cid, parent["id"] if parent else None, payload.anchor_message_id
        ):
            prompt = context.build_node_fork_prompt(anchor_text=anchor_text, question=question)
            resume, fork = sesion_dueño, True
        else:
            ancestors = db().node_chain(node["id"])
            bound_message_id = (
                ancestors[0]["anchor_message_id"] if ancestors else payload.anchor_message_id
            )
            prompt = context.build_node_opening_prompt(
                main_messages=db().main_messages_upto(cid, bound_message_id),
                ancestors=[(a, db().thread_messages(cid, a["id"])) for a in ancestors],
                anchor_text=anchor_text,
                question=question,
                anchor_in_node=bool(parent),
            )
            resume, fork = None, False

        user_message = db().add_message(
            cid, "user", question or context.FALLBACK_QUESTION, node_id=node["id"]
        )
        assistant = db().add_message(cid, "assistant", "", node_id=node["id"])
        node["collapsed"] = False
        node["messages"] = [user_message, assistant]
        stream = start_turn(
            project=project,
            chat=chat,
            node=node,
            prompt=prompt,
            assistant_id=assistant["id"],
            resume=resume,
            fork=fork,
            first_event={
                "t": "node_created",
                "key": node["id"],
                "node": node,
                "user_message": user_message,
                "assistant_message": assistant,
            },
        )
        return sse(stream.subscribe())

    @app.post("/api/nodes/{nid}/turn")
    async def post_node_turn(nid: str, payload: TurnIn) -> StreamingResponse:
        node = fetch_node(nid)
        chat = fetch_chat(node["chat_id"])
        project = fetch_project(chat["project_id"])
        text = payload.text.strip()
        if not text:
            raise HTTPException(400, "Mensaje vacio")
        require_idle(nid)

        user_message = db().add_message(chat["id"], "user", text, node_id=nid)
        assistant = db().add_message(chat["id"], "assistant", "", node_id=nid)
        stream = start_turn(
            project=project,
            chat=chat,
            node=node,
            prompt=text,
            assistant_id=assistant["id"],
            resume=node.get("session_id"),
            first_event={
                "t": "accepted",
                "key": nid,
                "node_id": nid,
                "user_message": user_message,
                "assistant_message": assistant,
            },
        )
        return sse(stream.subscribe())

    @app.patch("/api/nodes/{nid}")
    def patch_node(nid: str, payload: NodePatch) -> dict:
        fetch_node(nid)
        data = payload.model_dump(exclude_none=True)
        if "collapsed" in data:
            data["collapsed"] = 1 if data["collapsed"] else 0
        node = db().update_node(nid, **data)
        node["collapsed"] = bool(node["collapsed"])
        return {"node": node}

    @app.delete("/api/nodes/{nid}")
    def delete_node(nid: str) -> dict:
        fetch_node(nid)
        borrados = db().delete_node(nid)
        for ident in borrados:
            stream = turns().pop(ident, None)
            if stream is not None:
                stream.stop()
        return {"deleted": borrados}

    # --- control de turnos en curso ---------------------------------------

    @app.post("/api/turns/{key}/stop")
    def post_stop(key: str) -> dict:
        stream = live(key)
        if stream is None:
            raise HTTPException(404, "No hay ninguna respuesta en curso en ese hilo")
        return {"stopped": stream.stop()}

    @app.get("/api/turns/{key}/stream")
    def get_turn_stream(key: str) -> StreamingResponse:
        """Reengancharse a un turno que ya estaba escribiendo."""
        stream = turns().get(key)
        if stream is None:
            raise HTTPException(404, "Ese turno ya no está disponible")
        return sse(stream.subscribe())

    # --- laboratorio de codigo -------------------------------------------

    @app.get("/api/runner/languages")
    def get_runner_languages() -> dict:
        return {
            "languages": runner.language_info(),
            "limits": {
                "code_bytes": runner.MAX_CODE_BYTES,
                "stdin_bytes": runner.MAX_STDIN_BYTES,
                "output_bytes": runner.MAX_OUTPUT_BYTES,
                "timeout_seconds": runner.MAX_TIMEOUT,
            },
        }

    @app.post("/api/runner/run")
    async def post_runner_run(payload: CodeRunIn) -> dict:
        try:
            return await runner.run_code(
                payload.language,
                payload.code,
                payload.stdin,
                payload.timeout,
            )
        except ValueError as exc:
            raise HTTPException(400, str(exc))
        except OSError as exc:
            raise HTTPException(500, f"No se pudo ejecutar el runtime: {exc}")

    # --- estatico ---------------------------------------------------------

    web = config.web_dir()

    @app.get("/")
    def index() -> FileResponse:
        return FileResponse(web / "index.html")

    app.mount("/static", StaticFiles(directory=str(web)), name="static")
    return app
