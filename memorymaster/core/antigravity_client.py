"""Gemini a traves del CLI de Antigravity (`agy`) en modo headless, sobre OAuth.

POR QUE EXISTE. El plan de zai-coding-plan (GLM) se dio de baja el 2026-08-20, y
con el quedaron sin proveedor la consolidacion de Dreaming y el map/reduce del
perfil compilado. La alternativa que ya estaba en el codigo, `GeminiExtractor`,
usa GEMINI_API_KEY — una clave paga. Este cliente usa la sesion OAuth que el
operador ya tiene, sin clave y sin costo por token adicional.

EL COSTO REAL NO ES EL MODELO, ES LA LLAMADA. Medido el 2026-08-20 con tres
invocaciones distintas: la entrada fue 20015, 20011 y 20009 tokens para prompts
de ~150 tokens. Ese piso de ~20k es andamiaje fijo del agente y NO baja
eligiendo un modelo mas barato ni acortando el prompt.

    diez llamadas chicas  = ~200k tokens de entrada
    una llamada con diez items = ~20k

Por eso este cliente NO expone un helper de "una llamada por item". Quien lo use
tiene que agrupar. Un bucle sobre `complete()` es un error de diseño, no una
ineficiencia menor: multiplica el piso por la cantidad de items.

AUTENTICACION. Headless reutiliza las credenciales cacheadas de una sesion
interactiva previa de `agy`. Sin ellas la corrida sale con error de autenticacion
en vez de colgarse esperando una terminal, que es el comportamiento correcto para
una tarea programada.

MODELOS. `agy models` lista las variantes; los sufijos -low/-medium/-high son
NIVELES DE ESFUERZO de la misma generacion, no modelos distintos. Un modelo
desconocido hace salir a `agy` con codigo 1 en vez de caer a un default en
silencio, asi que un nombre mal escrito falla ruidosamente. No lo tapamos.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

# Margen bajo los 32.767 de CreateProcess: el resto de la linea (ejecutable,
# --model, --output-format, --print-timeout) tambien cuenta.
_MAX_PROMPT_CHARS = 30_000

CommandRunner = Callable[
    [list[str], str, int, Path, dict[str, str]], subprocess.CompletedProcess[str]
]

# Piso de entrada por llamada, medido el 2026-08-20 (20015 / 20011 / 20009).
# No es una estimacion: es lo que cuesta abrir la boca.
MEASURED_CALL_OVERHEAD_TOKENS = 20_000

DEFAULT_MODEL = "gemini-3.7-flash-low"
DEFAULT_TIMEOUT_SECONDS = 300


class AntigravityError(RuntimeError):
    """Fallo al invocar `agy`. Nunca se traga: una llamada que no ocurrio no es un resultado vacio."""


@dataclass(frozen=True, slots=True)
class AntigravityResponse:
    text: str
    model: str
    input_tokens: int
    output_tokens: int
    thinking_tokens: int
    duration_seconds: float
    conversation_id: str


def _default_runner(
    command: list[str], prompt: str, timeout: int, cwd: Path, env: dict[str, str]
) -> subprocess.CompletedProcess[str]:
    from memorymaster.dreaming.providers import _default_command_runner

    return _default_command_runner(command, prompt, timeout, cwd, env)


class AntigravityClient:
    """Cliente headless de `agy`. Una instancia por modelo."""

    def __init__(
        self,
        *,
        model: str | None = None,
        command: str | None = None,
        timeout: int | None = None,
        runner: CommandRunner = _default_runner,
        work_dir: str | Path | None = None,
    ) -> None:
        self.model = model or os.environ.get("MEMORYMASTER_AGY_MODEL", DEFAULT_MODEL)
        self.command = command or os.environ.get("MEMORYMASTER_AGY_COMMAND", "agy")
        self.timeout = int(
            timeout
            if timeout is not None
            else os.environ.get("MEMORYMASTER_AGY_TIMEOUT", DEFAULT_TIMEOUT_SECONDS)
        )
        self.runner = runner
        self.work_dir = (
            Path(work_dir)
            if work_dir is not None
            else Path.home() / ".memorymaster" / "agy"
        )

    def available(self) -> bool:
        """Esta `agy` en el PATH? No dice nada sobre autenticacion."""
        return shutil.which(self.command) is not None

    def complete(self, prompt: str) -> AntigravityResponse:
        """UNA llamada. Agrupar los items ANTES de llamar — ver el encabezado del modulo."""
        if not self.available():
            raise AntigravityError(
                f"el CLI '{self.command}' no esta en el PATH; "
                "instalar Antigravity o fijar MEMORYMASTER_AGY_COMMAND"
            )
        # `agy` recibe el prompt como ARGUMENTO (-p), y Windows corta la linea de
        # comandos en 32.767 caracteres. Pasado ese punto CreateProcess falla y
        # Python levanta FileNotFoundError — que el except de abajo traducia a
        # "no se pudo ejecutar 'agy'", mandando a buscar un CLI que si esta
        # instalado. Medido el 2026-08-25: 32.014 caracteres pasan, 40.014 no.
        #
        # Costo real de ese mensaje: el perfil compilado quedo cinco dias sin
        # avanzar con `AntigravityError` como unica pista, porque su lote por
        # defecto era de 200.000 caracteres —seis veces el limite— y el error
        # apuntaba al lugar equivocado.
        #
        # Se corta ANTES de invocar y se dice el numero, que es lo unico que
        # permite elegir un lote nuevo sin volver a bisectar.
        if len(prompt) > _MAX_PROMPT_CHARS:
            raise AntigravityError(
                f"el prompt tiene {len(prompt)} caracteres y el limite de linea de "
                f"comandos de Windows deja pasar ~{_MAX_PROMPT_CHARS}; `agy` recibe el "
                "prompt en -p, asi que hay que reducir el lote "
                "(MEMORYMASTER_PROFILE_MAX_INPUT_CHARS para el perfil compilado)"
            )
        self.work_dir.mkdir(parents=True, exist_ok=True)
        command = [
            self.command,
            "-p", prompt,
            "--model", self.model,
            "--output-format", "json",
            "--print-timeout", f"{self.timeout}s",
        ]
        try:
            completed = self.runner(command, "", self.timeout, self.work_dir, dict(os.environ))
        except subprocess.TimeoutExpired as exc:
            raise AntigravityError(
                f"`agy` no respondio en {self.timeout}s con el modelo {self.model}"
            ) from exc
        except FileNotFoundError as exc:
            raise AntigravityError(f"no se pudo ejecutar '{self.command}'") from exc

        if completed.returncode != 0:
            detail = (completed.stderr or completed.stdout or "").strip()[:400]
            # Un modelo desconocido sale 1 a proposito. Se reporta tal cual: caer a
            # un default enmascararia un nombre mal escrito como "anduvo peor".
            raise AntigravityError(
                f"`agy` salio con codigo {completed.returncode} (modelo {self.model}): {detail}"
            )
        return self._parse(completed.stdout)

    def _parse(self, stdout: str) -> AntigravityResponse:
        raw = (stdout or "").strip()
        if not raw:
            raise AntigravityError("`agy` no devolvio salida")
        try:
            payload: dict[str, Any] = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise AntigravityError(f"`agy` devolvio JSON invalido: {raw[:200]}") from exc

        status = str(payload.get("status") or "").upper()
        if status != "SUCCESS":
            # El codigo de salida puede ser 0 con un status no exitoso adentro. Mirar
            # solo returncode dejaria pasar una respuesta fallida como buena.
            raise AntigravityError(f"`agy` reporto status={status or 'desconocido'}")

        usage = payload.get("usage") or {}
        return AntigravityResponse(
            text=str(payload.get("response") or ""),
            model=self.model,
            input_tokens=int(usage.get("input_tokens") or 0),
            output_tokens=int(usage.get("output_tokens") or 0),
            thinking_tokens=int(usage.get("thinking_tokens") or 0),
            duration_seconds=float(payload.get("duration_seconds") or 0.0),
            conversation_id=str(payload.get("conversation_id") or ""),
        )


def strip_code_fence(text: str) -> str:
    """Saca el cerco markdown que los modelos ponen alrededor del JSON aunque se les pida que no.

    Observado en las tres generaciones de Flash durante la evaluacion del
    2026-08-20, incluso con "sin markdown" explicito en el prompt.
    """
    cleaned = (text or "").strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.split("\n", 1)[-1] if "\n" in cleaned else cleaned[3:]
        if cleaned.rstrip().endswith("```"):
            cleaned = cleaned.rstrip()[:-3]
    return cleaned.strip()
