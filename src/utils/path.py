"""Utilidades de rutas Windows + parseo de salida de wsl.exe."""
from __future__ import annotations

import re
from pathlib import Path
from typing import Optional

# "  NAME            STATE           VERSION "
# "  docker-desktop   Running         2 "
_LINE_RE = re.compile(r"^\s*\*?\s*(?P<name>\S+)\s+(?P<state>\S+)\s+(?P<version>\d+)\s*$")


def parse_wsl_list_output(output: str) -> list[tuple[str, str, int]]:
    """Parsea 'wsl -l -v' -> [(name, state, version), ...]. Resiliente a UTF-16 y saltos."""
    rows: list[tuple[str, str, int]] = []
    for raw in output.splitlines():
        line = raw.replace("\x00", "").strip()
        if not line or line.startswith("NAME"):
            continue
        m = _LINE_RE.match(line)
        if not m:
            continue
        rows.append((m.group("name"), m.group("state"), int(m.group("version"))))
    return rows


def parse_running_output(output: str) -> list[str]:
    """Parsea 'wsl -l --running' -> [nombres]."""
    names = []
    for raw in output.splitlines():
        line = raw.replace("\x00", "").strip()
        if not line or "NAME" in line.upper():
            continue
        # Saltar encabezados localizados ("Distribuciones de subsistema...:")
        if line.endswith(":") or ":" in line and line.split(":")[0].strip().lower() in (
            "distribuciones de subsistema de windows para linux",
            "windows subsystem for linux distributions",
            "distribuciones",
        ):
            continue
        names.append(line.split()[0])
    return names


def first_ip(output: str) -> Optional[str]:
    """Primera IP valida de 'hostname -I'."""
    for raw in output.splitlines():
        for tok in raw.replace("\x00", "").split():
            if re.match(r"^\d{1,3}(\.\d{1,3}){3}$", tok) and not tok.startswith("169.254"):
                return tok
    return None


def wsl_localhost_path(distro: str, subpath: str = "") -> str:
    base = f"\\\\wsl.localhost\\{distro}"
    return base if not subpath else f"{base}\\{subpath}"
