"""WslProvider: ciclo de vida, IPs, export/import, snapshots, clonado, comandos.

Toda la interaccion con wsl.exe pasa por aqui. Devuelve CommandResult o
datos tipados; nunca lanza excepciones salvo error de programacion.
"""
from __future__ import annotations

import re
import shutil
import socket
import time
from datetime import datetime
from pathlib import Path
from typing import Optional

from src.core.config import ConfigStore, snapshot_dir
from src.providers.base import CommandResult, Distro, DistroMetrics
from src.utils.path import first_ip, parse_running_output, parse_wsl_list_output, wsl_localhost_path
from src.utils.subprocess_async import run, spawn_detached


class WslProvider:
    def __init__(self, config_store: ConfigStore | None = None, wsl_exe: str = "wsl.exe") -> None:
        self._store = config_store
        self.wsl_exe = wsl_exe or "wsl.exe"

    # -- helpers -----------------------------------------------------------

    def _wsl(self, args: list[str], timeout: float = 30.0, breaker: bool = True) -> CommandResult:
        return run([self.wsl_exe, *args], timeout=timeout, breaker=breaker)

    def _wsl_d(self, distro: str, args: list[str], timeout: float = 30.0,
               breaker: bool = True) -> CommandResult:
        return run([self.wsl_exe, "-d", distro, *args], timeout=timeout, breaker=breaker)

    # -- ciclo de vida (W2) -------------------------------------------------

    def list_distros(self) -> list[Distro]:
        """Lista distros con estado/version. La IP se rellena por el watcher."""
        result = self._wsl(["-l", "-v"], timeout=5)
        if not result.ok:
            # wsl -l -v falla si WSL no esta inicializado; intenta -l --running
            if "no installed" in result.error.lower():
                return []
            raise RuntimeError(f"wsl -l -v fallo: {result.error or result.output}")
        rows = parse_wsl_list_output(result.output)
        default_name = self._default_name()
        return [
            Distro(name=name, state=state, version=ver, default=(name == default_name))
            for name, state, ver in rows
        ]

    def _default_name(self) -> str:
        for raw in self._wsl(["-l", "-v"], timeout=5).output.splitlines():
            if raw.startswith("*") or raw.startswith(" *"):
                m = re.match(r"^\s*\*?\s*(\S+)", raw)
                if m:
                    return m.group(1)
        return ""

    def running_distros(self) -> list[str]:
        return parse_running_output(self._wsl(["-l", "--running"], timeout=5).output)

    def start(self, name: str) -> CommandResult:
        # Arrancar sin abrir consola: ejecutar un comando trivial dentro.
        return self._wsl_d(name, ["--", "true"], timeout=15)

    def stop(self, name: str) -> CommandResult:
        return self._wsl(["--terminate", name], timeout=10)

    def restart(self, name: str) -> CommandResult:
        self.stop(name)
        time.sleep(1.5)
        return self.start(name)

    def shutdown_all(self) -> CommandResult:
        """Stop each running distro individually (wsl --shutdown can hang)."""
        results = []
        for d in self.list_distros():
            if d.state == "Running":
                r = self.stop(d.name)
                results.append(f"{d.name}: {'ok' if r.ok else r.error}")
        return CommandResult(ok=True, output="\n".join(results), error="")

    def version(self) -> CommandResult:
        return self._wsl(["--version"], timeout=5)

    def is_installed(self) -> bool:
        return self._wsl(["--version"], timeout=5).ok

    # -- IPs (W3) ------------------------------------------------------------

    def get_ip(self, name: str) -> Optional[str]:
        if name not in self.running_distros():
            return None
        out = self._wsl_d(name, ["hostname", "-I"], timeout=8, breaker=False).output
        return first_ip(out)

    def get_all_ips(self) -> dict[str, Optional[str]]:
        ips: dict[str, Optional[str]] = {}
        for d in self.list_distros():
            ips[d.name] = self.get_ip(d.name) if d.state == "Running" else None
        return ips

    # -- shell / explorador ---------------------------------------------------

    def open_shell(self, name: str) -> CommandResult:
        return spawn_detached([self.wsl_exe, "-d", name])

    def open_explorer(self, name: str) -> CommandResult:
        return spawn_detached(["explorer.exe", wsl_localhost_path(name)])

    # -- export / import / clone / snapshot (W4, W6, W7) -----------------------

    def export(self, name: str, target: str) -> CommandResult:
        target_path = Path(target)
        target_path.parent.mkdir(parents=True, exist_ok=True)
        if target_path.exists():
            target_path.unlink()
        return self._wsl(["--export", name, str(target_path)], timeout=600)

    def import_distro(self, source: str, name: str, install_dir: str) -> CommandResult:
        Path(install_dir).mkdir(parents=True, exist_ok=True)
        return self._wsl(["--import", name, install_dir, source], timeout=600)

    def install_new(self, name: str, no_launch: bool = True) -> CommandResult:
        """Instala una distro nueva desde el catalogo de WSL (wsl --install).

        Descarga y registra la distro sin arrancarla. Requiere internet y
        puede tardar varios minutos.
        """
        args = ["--install", "--distribution", name]
        if no_launch:
            args.append("--no-launch")
        return self._wsl(args, timeout=1800)

    def set_default(self, name: str) -> CommandResult:
        return self._wsl(["--set-default", name], timeout=60)

    def clone(self, name: str, new_name: str) -> CommandResult:
        """Exporta a un tar temporal e importa con nombre nuevo (W7)."""
        tmp = snapshot_dir() / f"clone-tmp-{new_name}-{int(time.time())}.tar"
        try:
            r = self.export(name, str(tmp))
            if not r.ok:
                return r
            install = str(snapshot_dir() / f"install-{new_name}")
            r2 = self.import_distro(str(tmp), new_name, install)
            return r2
        finally:
            if tmp.exists():
                tmp.unlink()

    def snapshot(self, name: str, retention_days: int | None = None, target_dir: str | None = None) -> Path:
        """Export fechado en snapshots/. Devuelve la ruta creada."""
        base = Path(target_dir) if target_dir else snapshot_dir()
        base.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d-%H%M")
        target = base / f"snapshot-{name}-{stamp}.tar"
        r = self.export(name, str(target))
        if not r.ok:
            raise RuntimeError(f"snapshot fallo: {r.error}")
        return target

    def prune_snapshots(self, retention_days: int, target_dir: str | None = None) -> list[Path]:
        base = Path(target_dir) if target_dir else snapshot_dir()
        purged: list[Path] = []
        cutoff = time.time() - retention_days * 86400
        for f in base.glob("snapshot-*.tar"):
            if f.stat().st_mtime < cutoff:
                f.unlink(missing_ok=True)
                purged.append(f)
        return purged

    # -- dependencias de arranque (W8) -----------------------------------------

    def wait_port(self, name: str, port: int, timeout: float = 60.0) -> bool:
        ip = self.get_ip(name)
        if not ip:
            return False
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                with socket.create_connection((ip, port), timeout=2):
                    return True
            except OSError:
                time.sleep(1.5)
        return False

    # -- comandos (W10/W11) ------------------------------------------------------

    def run_command(self, name: str, cmd: str) -> CommandResult:
        return self._wsl_d(name, ["--", "sh", "-lc", cmd], timeout=300)

    # -- metricas (R3) ------------------------------------------------------------

    def metrics(self, name: str) -> Optional[DistroMetrics]:
        distros = {d.name: d for d in self.list_distros()}
        d = distros.get(name)
        if d is None:
            return None
        m = DistroMetrics(name=name, running=(d.state == "Running"), ip=d.ip)
        if not m.running:
            return m
        # Una sola llamada bash por distro (antes: 4-5 lanzamientos wsl.exe).
        # Sin $ ni comillas en el script: wsl.exe re-parsea la linea de
        # comandos con reglas de cmd, asi que se evitan caracteres especiales.
        script = "hostname -I; free -m | sed -n 2p; nproc; cat /proc/uptime"
        out = self._wsl_d(name, ["bash", "-lc", script], timeout=30).output
        lines = [ln.replace("\x00", "").strip() for ln in out.splitlines() if ln.strip()]
        if lines:
            m.ip = first_ip(lines[0]) or m.ip
        if len(lines) >= 2:
            parts = lines[1].split()
            if len(parts) >= 3 and parts[1].isdigit() and parts[2].isdigit():
                total, used = int(parts[1]), int(parts[2])
                m.ram_total_mb = total
                m.ram_used_mb = used
                m.ram_percent = round(used / total * 100, 1) if total else 0.0
        if len(lines) >= 3 and lines[2].isdigit():
            m.cpus = int(lines[2])
        if len(lines) >= 4:
            try:
                m.uptime_s = int(float(lines[3].split()[0]))
            except (ValueError, IndexError):
                pass
        return m

    # -- utilidades ---------------------------------------------------------------

    def get_free_disk(self) -> float:
        """GB libres del disco donde vive la VM (para alertas VHD, W18)."""
        try:
            return shutil.disk_usage(snapshot_dir()).free / (1024 ** 3)
        except OSError:
            return -1.0
