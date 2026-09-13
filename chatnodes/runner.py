"""Ejecucion local y limitada de ejercicios de programacion.

No pretende sustituir a un contenedor de seguridad. Evita el shell, usa una
carpeta temporal, limpia el entorno y limita tiempo, entrada y salida.
"""

from __future__ import annotations

import asyncio
import importlib.util
import os
import shutil
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

MAX_CODE_BYTES = 100_000
MAX_STDIN_BYTES = 20_000
MAX_OUTPUT_BYTES = 64_000
DEFAULT_TIMEOUT = 6.0
MAX_TIMEOUT = 10.0


@dataclass(frozen=True)
class Language:
    id: str
    name: str
    extension: str
    source_name: str
    executable: str
    compile_executable: str | None = None


CLIPS_WRAPPER = r'''import sys
import clips

class Capture(clips.Router):
    def __init__(self):
        super().__init__("chatnodes-capture", 20)
        self.parts = []

    def query(self, logical_name):
        return logical_name in {
            "stdout", "wtrace", "wdialog", "wdisplay", "wwarning", "werror"
        }

    def write(self, logical_name, message):
        self.parts.append(message)

capture = Capture()
environment = clips.Environment()
environment.add_router(capture)
try:
    environment.load("main.clp")
    environment.reset()
    for line in sys.stdin:
        fact = line.strip()
        if fact:
            environment.assert_string(fact)
    environment.run()
    sys.stdout.write("".join(capture.parts))
except Exception as exc:
    sys.stdout.write("".join(capture.parts))
    sys.stderr.write(f"{type(exc).__name__}: {exc}\n")
    raise SystemExit(1)
'''


def _which(*names: str) -> str | None:
    for name in names:
        found = shutil.which(name)
        if found:
            return found
    return None


def _installed_swipl() -> str | None:
    found = _which("swipl", "swipl-win", "prolog")
    if found:
        return found
    roots = [
        Path(os.environ.get("LOCALAPPDATA", "")) / "ChatNodes" / "runtimes" / "swipl",
        Path(os.environ.get("ProgramFiles", "")) / "swipl",
    ]
    for root in roots:
        for name in ("swipl.exe", "swipl-win.exe"):
            candidate = root / "bin" / name
            if candidate.is_file():
                return str(candidate)
    return None


def available_languages() -> list[Language]:
    """Lenguajes realmente ejecutables en este ordenador."""
    languages = [
        Language("python", "Python", ".py", "main.py", sys.executable),
    ]
    node = _which("node")
    if node:
        languages.append(Language("javascript", "JavaScript", ".js", "main.js", node))
    gcc = _which("gcc")
    if gcc:
        languages.append(Language("c", "C", ".c", "main.c", gcc, gcc))
    gpp = _which("g++", "clang++")
    if gpp:
        languages.append(Language("cpp", "C++", ".cpp", "main.cpp", gpp, gpp))
    java = _which("java")
    javac = _which("javac")
    if java and javac:
        languages.append(Language("java", "Java", ".java", "Main.java", java, javac))
    swipl = _installed_swipl()
    if swipl:
        languages.append(Language("prolog", "Prolog (SWI)", ".pl", "main.pl", swipl))
    if importlib.util.find_spec("clips") is not None:
        languages.append(Language("clips", "CLIPS", ".clp", "main.clp", sys.executable))
    return languages


def language_info() -> list[dict[str, str]]:
    return [
        {"id": lang.id, "name": lang.name, "extension": lang.extension}
        for lang in available_languages()
    ]


def _environment(cwd: Path) -> dict[str, str]:
    """Entorno minimo que conserva lo necesario para localizar runtimes."""
    keep = ("PATH", "PATHEXT", "SYSTEMROOT", "WINDIR", "COMSPEC", "TEMP", "TMP")
    env = {key: os.environ[key] for key in keep if key in os.environ}
    env.update(
        {
            "HOME": str(cwd),
            "USERPROFILE": str(cwd),
            "PYTHONIOENCODING": "utf-8",
            "PYTHONUNBUFFERED": "1",
            "NO_COLOR": "1",
        }
    )
    # CLIPS se instala como modulo Python. Al cambiar HOME para aislar la
    # ejecucion hay que conservar explicitamente la ruta del paquete y cffi.
    clips_spec = importlib.util.find_spec("clips")
    if clips_spec and clips_spec.origin:
        env["PYTHONPATH"] = str(Path(clips_spec.origin).parent.parent)
    return env


async def _process(
    argv: list[str],
    *,
    cwd: Path,
    stdin: str = "",
    timeout: float,
) -> dict[str, Any]:
    started = time.perf_counter()
    process = await asyncio.create_subprocess_exec(
        *argv,
        cwd=str(cwd),
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=_environment(cwd),
        limit=16 * 1024,
    )
    assert process.stdin and process.stdout and process.stderr
    truncated = False
    output_left = MAX_OUTPUT_BYTES

    async def read_limited(stream: asyncio.StreamReader) -> bytes:
        nonlocal truncated, output_left
        collected = bytearray()
        while True:
            chunk = await stream.read(4096)
            if not chunk:
                break
            room = output_left
            if room > 0:
                piece = chunk[:room]
                collected.extend(piece)
                output_left -= len(piece)
            if len(chunk) > room:
                truncated = True
        return bytes(collected)

    out_task = asyncio.create_task(read_limited(process.stdout))
    err_task = asyncio.create_task(read_limited(process.stderr))
    process.stdin.write(stdin.encode("utf-8"))
    try:
        await process.stdin.drain()
    except (BrokenPipeError, ConnectionResetError):
        pass
    process.stdin.close()

    timed_out = False
    try:
        await asyncio.wait_for(process.wait(), timeout=timeout)
    except asyncio.TimeoutError:
        timed_out = True
        process.kill()
        await process.wait()

    stdout, stderr = await asyncio.gather(out_task, err_task)
    return {
        "stdout": stdout.decode("utf-8", "replace"),
        "stderr": stderr.decode("utf-8", "replace"),
        "exit_code": process.returncode,
        "timed_out": timed_out,
        "truncated": truncated,
        "duration_ms": round((time.perf_counter() - started) * 1000),
    }


def _commands(language: Language, folder: Path) -> tuple[list[str] | None, list[str]]:
    source = folder / language.source_name
    if language.id == "python":
        return None, [language.executable, "-I", "-S", str(source)]
    if language.id == "javascript":
        return None, [language.executable, str(source)]
    if language.id == "c":
        output = folder / ("program.exe" if os.name == "nt" else "program")
        return (
            [language.compile_executable or language.executable, str(source), "-O0", "-o", str(output)],
            [str(output)],
        )
    if language.id == "cpp":
        output = folder / ("program.exe" if os.name == "nt" else "program")
        return (
            [
                language.compile_executable or language.executable,
                str(source),
                "-std=c++17",
                "-O0",
                "-o",
                str(output),
            ],
            [str(output)],
        )
    if language.id == "java":
        return (
            [language.compile_executable or "javac", str(source)],
            [language.executable, "-cp", str(folder), "Main"],
        )
    if language.id == "prolog":
        return None, [
            language.executable,
            "-q",
            "-s",
            str(source),
            "-g",
            "main",
            "-t",
            "halt",
        ]
    if language.id == "clips":
        wrapper = folder / "clips_runner.py"
        wrapper.write_text(CLIPS_WRAPPER, encoding="utf-8")
        return None, [language.executable, str(wrapper)]
    raise ValueError("Lenguaje no compatible")


async def run_code(
    language_id: str,
    code: str,
    stdin: str = "",
    timeout: float = DEFAULT_TIMEOUT,
) -> dict[str, Any]:
    """Compila cuando toca y ejecuta el codigo con limites conservadores."""
    if len(code.encode("utf-8")) > MAX_CODE_BYTES:
        raise ValueError("El codigo supera el limite de 100 KB")
    if len(stdin.encode("utf-8")) > MAX_STDIN_BYTES:
        raise ValueError("La entrada supera el limite de 20 KB")
    if not code.strip():
        raise ValueError("Escribe algo de codigo antes de ejecutar")
    timeout = max(1.0, min(float(timeout), MAX_TIMEOUT))
    languages = {language.id: language for language in available_languages()}
    language = languages.get(language_id)
    if language is None:
        raise ValueError("Ese lenguaje no esta instalado o no esta disponible")

    with tempfile.TemporaryDirectory(prefix="chatnodes-run-") as temporary:
        folder = Path(temporary)
        (folder / language.source_name).write_text(code, encoding="utf-8")
        compile_command, run_command = _commands(language, folder)
        compile_result = None
        if compile_command:
            compile_result = await _process(
                compile_command,
                cwd=folder,
                timeout=min(MAX_TIMEOUT, timeout + 2),
            )
            if compile_result["timed_out"] or compile_result["exit_code"] != 0:
                return {
                    **compile_result,
                    "phase": "compile",
                    "compile_output": compile_result["stdout"] + compile_result["stderr"],
                    "language": language.id,
                }

        result = await _process(run_command, cwd=folder, stdin=stdin, timeout=timeout)
        result.update(
            {
                "phase": "run",
                "compile_output": (
                    compile_result["stdout"] + compile_result["stderr"]
                    if compile_result else ""
                ),
                "language": language.id,
            }
        )
        return result
