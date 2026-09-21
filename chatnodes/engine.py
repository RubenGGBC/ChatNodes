"""Motor: lanza el CLI de Claude Code en modo headless y traduce su stream-json.

Cada turno es una invocacion de ``claude -p`` con el prompt por stdin. La
continuidad la aporta el propio CLI: ``--session-id`` en el primer turno de un
hilo y ``--resume`` en los siguientes.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import sys
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import AsyncIterator

# Solo lectura: el tutor nunca escribe ni ejecuta nada.
ALLOWED_TOOLS = "Read Glob Grep"

# Las lineas de stream-json pueden ser grandes (resultados de Read sobre un PDF).
STREAM_LIMIT = 16 * 1024 * 1024


class EngineError(RuntimeError):
    """No se ha podido usar el CLI de Claude Code."""


@dataclass
class Chunk:
    """Un evento del stream, ya normalizado."""

    kind: str  # session | text | tool | limit | done | error
    text: str = ""
    session_id: str | None = None
    cost_usd: float | None = None
    data: dict | None = None


def find_cli(explicit: str | None = None) -> str:
    """Localiza el ejecutable del CLI."""
    candidate = explicit or os.environ.get("CHATNODES_CLAUDE_BIN")
    if candidate:
        if Path(candidate).exists() or shutil.which(candidate):
            return candidate
        raise EngineError(f"No existe el ejecutable indicado: {candidate}")
    found = shutil.which("claude")
    if found:
        return found
    local = Path.home() / ".local" / "bin" / ("claude.exe" if sys.platform == "win32" else "claude")
    if local.exists():
        return str(local)
    raise EngineError(
        "No encuentro el CLI de Claude Code en el PATH. Instalalo o define "
        "CHATNODES_CLAUDE_BIN con la ruta al ejecutable."
    )


def new_session_id() -> str:
    """El CLI exige un UUID valido en --session-id."""
    return str(uuid.uuid4())


def _child_env() -> dict[str, str]:
    """Entorno limpio: evita que una sesion padre de Claude Code confunda al hijo."""
    env = dict(os.environ)
    for key in ("CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT", "CLAUDE_CODE_SSE_PORT"):
        env.pop(key, None)
    return env


class ClaudeEngine:
    def __init__(self, cli: str | None = None) -> None:
        self.cli = find_cli(cli)

    def build_argv(
        self,
        *,
        session_id: str | None = None,
        resume: str | None = None,
        fork: bool = False,
        model: str | None = None,
        system_prompt: str | None = None,
    ) -> list[str]:
        argv = [
            self.cli,
            "-p",
            "--output-format", "stream-json",
            "--include-partial-messages",
            "--verbose",
            "--allowed-tools", ALLOWED_TOOLS,
            "--permission-mode", "dontAsk",
            "--disable-slash-commands",
        ]
        if resume:
            argv += ["--resume", resume]
            if fork:
                # Hereda el contexto (y su cache) pero estrena id de sesion, asi
                # que la rama no escribe en la conversacion de la que nace.
                argv.append("--fork-session")
        elif session_id:
            argv += ["--session-id", session_id]
        if model:
            argv += ["--model", model]
        if system_prompt:
            argv += ["--append-system-prompt", system_prompt]
        return argv

    async def stream(
        self,
        *,
        prompt: str,
        cwd: str | Path,
        session_id: str | None = None,
        resume: str | None = None,
        fork: bool = False,
        model: str | None = None,
        system_prompt: str | None = None,
        on_start=None,
    ) -> AsyncIterator[Chunk]:
        """Ejecuta un turno y emite Chunks a medida que llegan.

        ``on_start`` recibe el proceso en cuanto arranca, para que quien consuma
        el stream pueda matarlo si el usuario decide parar la respuesta.
        """
        argv = self.build_argv(
            session_id=session_id,
            resume=resume,
            fork=fork,
            model=model,
            system_prompt=system_prompt,
        )
        try:
            process = await asyncio.create_subprocess_exec(
                *argv,
                cwd=str(cwd),
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=_child_env(),
                limit=STREAM_LIMIT,
            )
        except OSError as exc:  # pragma: no cover - depende del sistema
            yield Chunk(kind="error", text=f"No se pudo lanzar el CLI: {exc}")
            return

        assert process.stdin and process.stdout and process.stderr
        if on_start is not None:
            on_start(process)
        stderr_chunks: list[bytes] = []

        async def drain_stderr() -> None:
            while True:
                line = await process.stderr.readline()
                if not line:
                    break
                stderr_chunks.append(line)

        stderr_task = asyncio.create_task(drain_stderr())

        try:
            process.stdin.write(prompt.encode("utf-8"))
            await process.stdin.drain()
        except (BrokenPipeError, ConnectionResetError):
            pass
        finally:
            try:
                process.stdin.close()
            except Exception:
                pass

        saw_result = False
        try:
            while True:
                try:
                    line = await process.stdout.readline()
                except (asyncio.LimitOverrunError, ValueError):
                    continue
                if not line:
                    break
                raw = line.decode("utf-8", "replace").strip()
                if not raw or not raw.startswith("{"):
                    continue
                try:
                    event = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                for chunk in translate(event):
                    if chunk.kind in ("done", "error"):
                        saw_result = True
                    yield chunk
        finally:
            # Si salimos sin haber visto el resultado, el turno se ha abortado:
            # no esperamos a que el CLI acabe por su cuenta. Cuando si hubo
            # resultado se espera de forma normal, para que el CLI termine de
            # escribir su sesion en disco y --resume siga funcionando.
            if not saw_result and process.returncode is None:
                try:
                    process.kill()
                except Exception:
                    pass
            await process.wait()
            await stderr_task
            # Cierra las tuberias del subproceso: si no, asyncio avisa de
            # transportes sin cerrar cuando el recolector pasa por ellos.
            transport = getattr(process, "_transport", None)
            if transport is not None:
                try:
                    transport.close()
                except Exception:
                    pass

        if not saw_result:
            detail = b"".join(stderr_chunks).decode("utf-8", "replace").strip()
            if process.returncode not in (0, None) or detail:
                message = detail or f"El CLI termino con codigo {process.returncode}."
                yield Chunk(kind="error", text=message)
            else:
                yield Chunk(kind="done")


def translate(event: dict) -> list[Chunk]:
    """Traduce un evento crudo de stream-json a cero o mas Chunks."""
    kind = event.get("type")

    if kind == "system" and event.get("subtype") == "init":
        return [Chunk(kind="session", session_id=event.get("session_id"))]

    if kind == "rate_limit_event":
        windows = ((event.get("rate_limit_info") or {}).get("unifiedWindows")) or {}
        data = {
            name: windows[name].get("utilization")
            for name in ("five_hour", "seven_day")
            if isinstance(windows.get(name), dict)
        }
        return [Chunk(kind="limit", data=data)] if data else []

    if kind == "stream_event":
        inner = event.get("event") or {}
        inner_type = inner.get("type")
        if inner_type == "content_block_delta":
            delta = inner.get("delta") or {}
            if delta.get("type") == "text_delta":
                return [Chunk(kind="text", text=delta.get("text") or "")]
            return []
        if inner_type == "content_block_start":
            block = inner.get("content_block") or {}
            if block.get("type") == "tool_use":
                return [Chunk(kind="tool", text=block.get("name") or "herramienta")]
        return []

    if kind == "result":
        if event.get("is_error") or event.get("subtype") not in (None, "success"):
            text = event.get("result") or event.get("error") or "El CLI devolvio un error."
            return [Chunk(kind="error", text=str(text), session_id=event.get("session_id"))]
        return [
            Chunk(
                kind="done",
                session_id=event.get("session_id"),
                cost_usd=event.get("total_cost_usd"),
            )
        ]

    return []
