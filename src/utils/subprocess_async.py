"""Ejecucion de subprocesos con timeout, serializacion y cortocircuito.

Prevencion de WSL colgado (causa raiz):
1. SERIALIZACION: todos los comandos wsl.exe se ejecutan de uno en uno
   (lock global). Evita que la app lance decenas de wsl.exe concurrentes
   que se apilan cuando el servicio esta lento.
2. CIRCUIT BREAKER: si un comando wsl.exe se cuelga (timeout), se abre un
   cortocircuito de 30s. Durante ese periodo TODOS los comandos wsl.exe
   fallan al instante SIN lanzar proceso (evita la acumulacion de wsl.exe
   huerfanos que saturan el servicio WSL).
3. TREE-KILL: al colgarse, mata wsl.exe y sus hijos (taskkill /T) para no
   dejar procesos huerfanos.
"""
from __future__ import annotations

import os
import subprocess
import sys
import threading
import time

from src.providers.base import CommandResult

_CREATE_NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0

_WSL_LOCK = threading.Lock()
_BREAKER_OPEN = False
_BREAKER_UNTIL = 0.0
_BREAKER_COOLDOWN = 30.0  # segundos con el cortocircuito abierto


def _is_wsl(args: list[str]) -> bool:
    if not args:
        return False
    base = os.path.basename(str(args[0])).lower()
    return base in ("wsl", "wsl.exe")


def _breaker_check() -> bool:
    """True si el cortocircuito esta abierto (fallar rapido, sin lanzar)."""
    global _BREAKER_OPEN, _BREAKER_UNTIL
    if not _BREAKER_OPEN:
        return False
    if time.time() >= _BREAKER_UNTIL:
        _BREAKER_OPEN = False  # cerrar: probar de nuevo
        return False
    return True


def _breaker_open_now() -> None:
    global _BREAKER_OPEN, _BREAKER_UNTIL
    _BREAKER_OPEN = True
    _BREAKER_UNTIL = time.time() + _BREAKER_COOLDOWN


def _kill_tree(pid: int) -> None:
    """Mata el proceso y SU arbol (hijos) sin tocar otros procesos.

    IMPORTANTE: NO usa 'taskkill /IM wsl.exe' (eso mataria TODAS las
    distros). Solo el PID especifico y sus descendientes.
    """
    try:
        subprocess.run(
            ["taskkill", "/F", "/T", "/PID", str(pid)],
            capture_output=True, timeout=5,
            creationflags=_CREATE_NO_WINDOW,
        )
    except Exception:
        pass


def run(args: list[str], timeout: float = 10.0, cwd: str | None = None,
        breaker: bool = True) -> CommandResult:
    """Ejecuta un comando y devuelve CommandResult. Nunca lanza excepcion.

    breaker=True: un timeout abre el cortocircuito global (comandos
    criticos: list/start/stop/export). breaker=False: el timeout NO abre
    el cortocircuito (sondeos opcionales como IP, que pueden fallar sin
    que WSL este caido).
    """
    is_wsl = _is_wsl(args)

    # Circuit breaker: si WSL esta colgado, fallar al instante sin lanzar wsl.exe
    if is_wsl and _breaker_check():
        return CommandResult(ok=False, error="WSL no responde (cortocircuito)", exit_code=-1)

    # Serializacion: solo un wsl.exe a la vez (evita acumulacion)
    if is_wsl:
        _WSL_LOCK.acquire()
    try:
        proc = subprocess.Popen(
            args,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=cwd,
            creationflags=_CREATE_NO_WINDOW,
        )
        try:
            stdout, stderr = proc.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            proc.kill()
            if is_wsl:
                _kill_tree(proc.pid)
                if breaker:
                    _breaker_open_now()
                    return CommandResult(
                        ok=False, error="WSL no responde (timeout, cortocircuito 30s)",
                        exit_code=-1,
                    )
                return CommandResult(ok=False, error=f"timeout tras {timeout}s", exit_code=-1)
            return CommandResult(ok=False, error=f"timeout tras {timeout}s", exit_code=-1)
    except FileNotFoundError as e:
        return CommandResult(ok=False, error=f"ejecutable no encontrado: {e}")
    except OSError as e:
        return CommandResult(ok=False, error=str(e), exit_code=-1)
    finally:
        if is_wsl:
            _WSL_LOCK.release()

    stdout = _decode(stdout)
    stderr = _decode(stderr)
    ok = proc.returncode == 0
    if not ok and not stderr and stdout:
        # wsl.exe escribe los errores en stdout (UTF-16) con rc != 0
        stderr, stdout = stdout, ""
    if is_wsl and ok:
        # WSL respondio: cerrar cortocircuito (si estaba abierto)
        global _BREAKER_OPEN, _BREAKER_UNTIL
        _BREAKER_OPEN = False
        _BREAKER_UNTIL = 0.0
    return CommandResult(
        ok=ok,
        output=stdout,
        error=stderr,
        exit_code=proc.returncode,
    )


def reset_breaker() -> None:
    """Cierra el cortocircuito manualmente (tras un reinicio de WSL)."""
    global _BREAKER_OPEN, _BREAKER_UNTIL
    _BREAKER_OPEN = False
    _BREAKER_UNTIL = 0.0


def breaker_state() -> dict:
    """Estado del cortocircuito (para diagnstico)."""
    return {
        "open": _BREAKER_OPEN,
        "until": _BREAKER_UNTIL,
        "cooldown": _BREAKER_COOLDOWN,
    }


def _decode(data: bytes) -> str:
    """wsl.exe escribe UTF-16-LE (con o sin BOM). Detecta y decodifica bien."""
    if not data:
        return ""
    # BOM UTF-16
    if data.startswith(b"\xff\xfe") or data.startswith(b"\xfe\xff"):
        try:
            return data.decode("utf-16")
        except (UnicodeDecodeError, UnicodeError):
            pass
    # Sin BOM: probar utf-8; si el resultado tiene bytes nulos, es UTF-16-LE
    try:
        s = data.decode("utf-8")
        if "\x00" in s:
            return data.decode("utf-16-le", errors="replace")
        return s
    except UnicodeDecodeError:
        pass
    try:
        return data.decode("utf-16-le")
    except (UnicodeDecodeError, UnicodeError):
        return data.decode("utf-8", errors="replace")


def spawn_detached(args: list[str]) -> CommandResult:
    """Lanza un proceso sin esperarlo (terminal/explorer). En Windows abre consola nueva."""
    try:
        creationflags = 0
        if sys.platform == "win32":
            creationflags = _CREATE_NO_WINDOW | getattr(
                subprocess, "DETACHED_PROCESS", 0
            )
        subprocess.Popen(args, creationflags=creationflags, close_fds=True)
        return CommandResult(ok=True)
    except OSError as e:
        return CommandResult(ok=False, error=str(e))