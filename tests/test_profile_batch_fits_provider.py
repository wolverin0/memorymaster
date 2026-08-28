"""El lote por defecto del perfil compilado debe CABER en el proveedor.

Medido el 2026-08-28: el run 3 del perfil llevaba OCHO DIAS atascado en
`mapping` devolviendo AntigravityError, y el perfil servia hechos del run 2
(13-08). Causa: `max_input_chars` default 200.000 contra el tope duro de
30.000 del AntigravityClient (limite de linea de comandos de Windows — `agy`
recibe el prompt en -p). Cada map call moria ANTES de invocar al proveedor,
asi que ninguna cantidad de reintentos podia avanzar.

No es un problema de configuracion: un default que no puede funcionar con el
cliente que el propio repo shippea es un bug. Verificado en vivo — con el
lote acotado a 20.000 el run avanzo en el primer intento (map_calls=1, ok).

Este test pina el ACOPLE, no el numero: si alguien baja el tope del cliente o
sube el default del lote, falla acá antes de que otro run se coma una semana.
"""
from __future__ import annotations

from memorymaster.core.antigravity_client import _MAX_PROMPT_CHARS
from memorymaster.profile.engine import DEFAULT_MAX_INPUT_CHARS as _DEFAULT
from memorymaster.profile.engine import _env_int


def test_default_batch_fits_in_provider_prompt_limit():
    assert _DEFAULT < _MAX_PROMPT_CHARS, (
        f"el lote por defecto del perfil ({_DEFAULT}) no entra en el tope del "
        f"proveedor ({_MAX_PROMPT_CHARS}): cada map call moriria con "
        "AntigravityError antes de invocar a agy"
    )


def test_default_leaves_room_for_the_prompt_scaffold():
    """El tope del cliente cubre el prompt COMPLETO, no solo los mensajes."""
    headroom = _MAX_PROMPT_CHARS - _DEFAULT
    assert headroom >= 5_000, (
        f"solo quedan {headroom} caracteres para el andamiaje del prompt; "
        "un lote al ras del tope falla en cuanto el template crece"
    )


def test_env_override_still_wins():
    import os

    prev = os.environ.get("MEMORYMASTER_PROFILE_MAX_INPUT_CHARS")
    os.environ["MEMORYMASTER_PROFILE_MAX_INPUT_CHARS"] = "12345"
    try:
        assert _env_int("MEMORYMASTER_PROFILE_MAX_INPUT_CHARS", _DEFAULT) == 12345
    finally:
        if prev is None:
            os.environ.pop("MEMORYMASTER_PROFILE_MAX_INPUT_CHARS", None)
        else:
            os.environ["MEMORYMASTER_PROFILE_MAX_INPUT_CHARS"] = prev
