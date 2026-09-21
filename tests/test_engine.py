from __future__ import annotations

import asyncio
import json
import shutil
import subprocess

import pytest

from chatnodes import engine as engine_mod
from chatnodes.engine import ClaudeEngine, Chunk, new_session_id, translate


def collect(agen):
    """Consume un generador asincrono desde un test sincrono."""

    async def run():
        return [chunk async for chunk in agen]

    return asyncio.run(run())


# --- traduccion de eventos --------------------------------------------------


def test_translate_init_expone_la_sesion():
    chunks = translate({"type": "system", "subtype": "init", "session_id": "abc"})
    assert chunks == [Chunk(kind="session", session_id="abc")]


def test_translate_delta_de_texto():
    event = {
        "type": "stream_event",
        "event": {"type": "content_block_delta", "delta": {"type": "text_delta", "text": "hola"}},
    }
    assert translate(event) == [Chunk(kind="text", text="hola")]


def test_translate_ignora_el_pensamiento():
    event = {
        "type": "stream_event",
        "event": {"type": "content_block_delta", "delta": {"type": "thinking_delta", "thinking": "x"}},
    }
    assert translate(event) == []


def test_translate_marca_el_uso_de_herramientas():
    event = {
        "type": "stream_event",
        "event": {"type": "content_block_start", "content_block": {"type": "tool_use", "name": "Read"}},
    }
    assert translate(event) == [Chunk(kind="tool", text="Read")]


def test_translate_result_correcto_trae_coste():
    chunks = translate({"type": "result", "session_id": "s1", "total_cost_usd": 0.012})
    assert chunks == [Chunk(kind="done", session_id="s1", cost_usd=0.012)]


def test_translate_result_con_error():
    chunks = translate(
        {"type": "result", "is_error": True, "subtype": "error_during_execution", "result": "boom"}
    )
    assert chunks[0].kind == "error"
    assert "boom" in chunks[0].text


# --- construccion del comando ----------------------------------------------


def test_argv_primer_turno_usa_session_id(tmp_path):
    cli = ClaudeEngine(cli=str(_fake_bin(tmp_path)))
    argv = cli.build_argv(session_id="uuid-1", model="opus", system_prompt="SP")
    assert "--session-id" in argv and argv[argv.index("--session-id") + 1] == "uuid-1"
    assert "--resume" not in argv
    assert argv[argv.index("--model") + 1] == "opus"
    assert argv[argv.index("--append-system-prompt") + 1] == "SP"


def test_argv_turno_siguiente_reanuda_y_no_crea_sesion(tmp_path):
    cli = ClaudeEngine(cli=str(_fake_bin(tmp_path)))
    argv = cli.build_argv(session_id="uuid-1", resume="uuid-0")
    assert argv[argv.index("--resume") + 1] == "uuid-0"
    assert "--session-id" not in argv


def test_argv_es_de_solo_lectura_y_no_pide_permisos(tmp_path):
    cli = ClaudeEngine(cli=str(_fake_bin(tmp_path)))
    argv = cli.build_argv()
    assert argv[argv.index("--allowed-tools") + 1] == "Read Glob Grep"
    assert argv[argv.index("--permission-mode") + 1] == "dontAsk"
    assert "--verbose" in argv  # stream-json lo exige


def test_argv_solo_usa_flags_que_el_cli_real_reconoce():
    """build_argv() se probaba solo contra un binario falso (ver _fake_bin),
    asi que un flag inventado (p.ej. el extinto --restricted) pasaba el test
    sin que nadie lo notara hasta ejecutar el tutor de verdad."""
    real_cli = shutil.which("claude")
    if not real_cli:
        pytest.skip("claude no esta en el PATH")
    help_text = subprocess.run(
        [real_cli, "--help"], capture_output=True, text=True, timeout=10
    ).stdout
    cli = ClaudeEngine(cli=real_cli)
    argv = cli.build_argv(session_id=new_session_id())
    flags = [a for a in argv if a.startswith("--")]
    faltantes = [f for f in flags if f not in help_text]
    assert not faltantes, f"flags no reconocidos por `claude --help`: {faltantes}"


def _fake_bin(tmp_path):
    path = tmp_path / "claude-falso.exe"
    path.write_text("no se ejecuta en estos tests")
    return path


# --- bucle de streaming -----------------------------------------------------


class FakeStdin:
    def __init__(self):
        self.written = b""

    def write(self, data):
        self.written += data

    async def drain(self):
        return None

    def close(self):
        return None


class FakeReader:
    def __init__(self, lines):
        self._lines = list(lines)

    async def readline(self):
        return self._lines.pop(0) if self._lines else b""


class FakeProcess:
    def __init__(self, stdout_lines, stderr_lines=(), returncode=0):
        self.stdin = FakeStdin()
        self.stdout = FakeReader(stdout_lines)
        self.stderr = FakeReader(stderr_lines)
        self.returncode = returncode

    async def wait(self):
        return self.returncode


def patch_process(monkeypatch, process):
    async def fake_exec(*args, **kwargs):
        fake_exec.argv = args
        fake_exec.kwargs = kwargs
        return process

    monkeypatch.setattr(engine_mod.asyncio, "create_subprocess_exec", fake_exec)
    return fake_exec


def ndjson(*events):
    return [json.dumps(event).encode("utf-8") + b"\n" for event in events]


def text_delta(text):
    return {
        "type": "stream_event",
        "event": {"type": "content_block_delta", "delta": {"type": "text_delta", "text": text}},
    }


def test_stream_devuelve_sesion_texto_y_fin(monkeypatch, tmp_path):
    lines = ndjson(
        {"type": "system", "subtype": "init", "session_id": "s9"},
        text_delta("El reloj "),
        text_delta("va a 3 GHz."),
        {"type": "result", "session_id": "s9", "total_cost_usd": 0.02},
    )
    process = FakeProcess(lines)
    patch_process(monkeypatch, process)

    cli = ClaudeEngine(cli=str(_fake_bin(tmp_path)))
    chunks = collect(cli.stream(prompt="explicame", cwd=tmp_path))

    assert [c.kind for c in chunks] == ["session", "text", "text", "done"]
    assert "".join(c.text for c in chunks if c.kind == "text") == "El reloj va a 3 GHz."
    assert chunks[-1].cost_usd == 0.02
    assert process.stdin.written == "explicame".encode("utf-8")


def test_stream_ignora_lineas_basura(monkeypatch, tmp_path):
    lines = [b"esto no es json\n", b"\n"] + ndjson(
        text_delta("ok"), {"type": "result", "total_cost_usd": 0}
    )
    patch_process(monkeypatch, FakeProcess(lines))

    cli = ClaudeEngine(cli=str(_fake_bin(tmp_path)))
    chunks = collect(cli.stream(prompt="x", cwd=tmp_path))

    assert [c.kind for c in chunks] == ["text", "done"]


def test_stream_reporta_el_stderr_si_el_cli_muere(monkeypatch, tmp_path):
    process = FakeProcess([], [b"Error: not logged in\n"], returncode=1)
    patch_process(monkeypatch, process)

    cli = ClaudeEngine(cli=str(_fake_bin(tmp_path)))
    chunks = collect(cli.stream(prompt="x", cwd=tmp_path))

    assert len(chunks) == 1
    assert chunks[0].kind == "error"
    assert "not logged in" in chunks[0].text


def test_stream_pasa_el_directorio_del_proyecto(monkeypatch, tmp_path):
    fake = patch_process(monkeypatch, FakeProcess(ndjson({"type": "result"})))
    cli = ClaudeEngine(cli=str(_fake_bin(tmp_path)))
    collect(cli.stream(prompt="x", cwd=tmp_path / "material"))
    assert fake.kwargs["cwd"] == str(tmp_path / "material")


# --- fork de sesion y limites de uso ---------------------------------------


def test_argv_con_fork_reanuda_pero_estrena_id(tmp_path):
    cli = ClaudeEngine(cli=str(_fake_bin(tmp_path)))
    argv = cli.build_argv(resume="uuid-0", fork=True)
    assert argv[argv.index("--resume") + 1] == "uuid-0"
    assert "--fork-session" in argv
    assert "--session-id" not in argv


def test_sin_resume_no_se_forkea(tmp_path):
    cli = ClaudeEngine(cli=str(_fake_bin(tmp_path)))
    argv = cli.build_argv(session_id="uuid-1", fork=True)
    assert "--fork-session" not in argv


def test_translate_expone_el_consumo_de_las_ventanas():
    event = {
        "type": "rate_limit_event",
        "rate_limit_info": {
            "unifiedWindows": {
                "five_hour": {"utilization": 0.06},
                "seven_day": {"utilization": 0.6},
            }
        },
    }
    chunks = translate(event)
    assert chunks[0].kind == "limit"
    assert chunks[0].data == {"five_hour": 0.06, "seven_day": 0.6}


def test_translate_ignora_un_limite_sin_datos():
    assert translate({"type": "rate_limit_event", "rate_limit_info": {}}) == []


def test_stream_entrega_el_proceso_para_poder_matarlo(monkeypatch, tmp_path):
    process = FakeProcess(ndjson(text_delta("hola"), {"type": "result"}))
    patch_process(monkeypatch, process)
    visto = []

    cli = ClaudeEngine(cli=str(_fake_bin(tmp_path)))
    collect(cli.stream(prompt="x", cwd=tmp_path, on_start=visto.append))

    assert visto == [process]


def test_stream_mata_el_cli_si_no_hubo_resultado(monkeypatch, tmp_path):
    """Al abortar no se espera a que el CLI termine solo: se le corta."""

    class Vivo(FakeProcess):
        def __init__(self):
            super().__init__([], [], returncode=None)
            self.matado = False

        def kill(self):
            self.matado = True
            self.returncode = -9

    process = Vivo()
    patch_process(monkeypatch, process)

    cli = ClaudeEngine(cli=str(_fake_bin(tmp_path)))
    collect(cli.stream(prompt="x", cwd=tmp_path))

    assert process.matado is True


def test_stream_no_mata_el_cli_tras_un_resultado(monkeypatch, tmp_path):
    """Con resultado se le deja acabar, para que guarde su sesion en disco."""

    class Contador(FakeProcess):
        def __init__(self, lines):
            super().__init__(lines)
            self.matado = False

        def kill(self):
            self.matado = True

    process = Contador(ndjson({"type": "result", "session_id": "s"}))
    patch_process(monkeypatch, process)

    cli = ClaudeEngine(cli=str(_fake_bin(tmp_path)))
    collect(cli.stream(prompt="x", cwd=tmp_path))

    assert process.matado is False
