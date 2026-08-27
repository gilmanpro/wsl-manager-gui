"""Tests de parseo y del WslProvider con subprocess mockeado."""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from src.providers.base import CommandResult, Distro
from src.providers.wsl_provider import WslProvider
from src.utils.path import first_ip, parse_running_output, parse_wsl_list_output


class TestParsers:
    def test_parse_wsl_list(self):
        out = (
            "  NAME            STATE           VERSION\n"
            "  ubuntu-dev      Running         2\n"
            "* docker-desktop  Stopped         2\n"
        )
        rows = parse_wsl_list_output(out)
        assert ("ubuntu-dev", "Running", 2) in rows
        assert ("docker-desktop", "Stopped", 2) in rows

    def test_parse_wsl_list_utf16_garbage(self):
        out = "\x00 \x00 ubuntu-dev\x00 Running\x00 2\x00"
        rows = parse_wsl_list_output(out)
        assert ("ubuntu-dev", "Running", 2) in rows

    def test_parse_running(self):
        names = parse_running_output("NAME\nubuntu-dev\nrancher-desktop")
        assert names == ["ubuntu-dev", "rancher-desktop"]

    def test_first_ip(self):
        assert first_ip("  172.18.123.45  \n") == "172.18.123.45"
        assert first_ip("169.254.1.2\n") is None  # link-local ignorada


@pytest.fixture
def provider():
    return WslProvider(wsl_exe="wsl.exe")


class TestWslProvider:
    def test_list_distros_ok(self, provider):
        with patch("src.providers.wsl_provider.run") as mock_run:
            mock_run.return_value = CommandResult(ok=True, output="  ubuntu-dev  Running  2\n* debian  Stopped  2\n")
            distros = provider.list_distros()
        assert distros == [Distro(name="ubuntu-dev", state="Running", version=2, default=False),
                           Distro(name="debian", state="Stopped", version=2, default=True)]

    def test_list_distros_fail(self, provider):
        with patch("src.providers.wsl_provider.run") as mock_run:
            mock_run.return_value = CommandResult(ok=False, error="Windows Subsystem for Linux has no installed distributions.")
            assert provider.list_distros() == []

    def test_start_uses_wsl_d(self, provider):
        with patch("src.providers.wsl_provider.run") as mock_run:
            mock_run.return_value = CommandResult(ok=True)
            r = provider.start("ubuntu-dev")
        assert r.ok
        mock_run.assert_called_once_with(["wsl.exe", "-d", "ubuntu-dev", "--", "true"], timeout=15, breaker=True)

    def test_stop(self, provider):
        with patch("src.providers.wsl_provider.run") as mock_run:
            mock_run.return_value = CommandResult(ok=True)
            provider.stop("ubuntu-dev")
        mock_run.assert_called_once_with(["wsl.exe", "--terminate", "ubuntu-dev"], timeout=10, breaker=True)

    def test_get_ip_only_when_running(self, provider):
        with patch.object(provider, "running_distros", return_value=["ubuntu-dev"]), patch(
            "src.providers.wsl_provider.run", return_value=CommandResult(ok=True, output="172.18.9.9\n")
        ):
            assert provider.get_ip("ubuntu-dev") == "172.18.9.9"
        with patch.object(provider, "running_distros", return_value=[]):
            assert provider.get_ip("ubuntu-dev") is None

    def test_wait_port(self, provider):
        import socket

        with patch.object(provider, "get_ip", return_value="10.0.0.1"), patch(
            "socket.create_connection", side_effect=OSError
        ):
            assert provider.wait_port("ubuntu-dev", 5432, timeout=0.1) is False
        with patch.object(provider, "get_ip", return_value="10.0.0.1"), patch(
            "socket.create_connection", return_value=MagicMock()
        ):
            assert provider.wait_port("ubuntu-dev", 5432, timeout=1) is True

    def test_export(self, provider, tmp_path):
        target = tmp_path / "x.tar"
        with patch("src.providers.wsl_provider.run") as mock_run:
            mock_run.return_value = CommandResult(ok=True)
            r = provider.export("ubuntu-dev", str(target))
        assert r.ok
        assert mock_run.call_args[0][0][:3] == ["wsl.exe", "--export", "ubuntu-dev"]

    def test_install_new(self, provider):
        with patch("src.providers.wsl_provider.run") as mock_run:
            mock_run.return_value = CommandResult(ok=True)
            r = provider.install_new("Ubuntu-24.04")
        assert r.ok
        args = mock_run.call_args[0][0]
        assert args[:3] == ["wsl.exe", "--install", "--distribution"]
        assert "Ubuntu-24.04" in args and "--no-launch" in args

    def test_import_distro(self, provider, tmp_path):
        src = tmp_path / "imagen.tar"
        src.write_bytes(b"x")
        with patch("src.providers.wsl_provider.run") as mock_run:
            mock_run.return_value = CommandResult(ok=True)
            r = provider.import_distro(str(src), "nueva", str(tmp_path / "dir"))
        assert r.ok
        args = mock_run.call_args[0][0]
        assert args[1:3] == ["--import", "nueva"]
        assert str(tmp_path / "dir") in args and str(src) in args

    def test_metrics_running(self, provider):
        with patch.object(provider, "list_distros", return_value=[Distro(name="u", state="Running", version=2)]), patch(
            "src.providers.wsl_provider.run"
        ) as mock_run:
            def fake(args, timeout=120, **kwargs):
                # metrics() combina todo en una sola llamada bash -lc
                if "bash" in args and "-lc" in args:
                    return CommandResult(
                        ok=True,
                        output="10.0.0.5\nMem:         8192        2048        6144\n8\n123.45 67.89\n",
                    )
                return CommandResult(ok=True)

            mock_run.side_effect = fake
            m = provider.metrics("u")
        assert m.running
        assert m.ip == "10.0.0.5"
        assert m.ram_total_mb == 8192
        assert m.ram_used_mb == 2048
        assert m.ram_percent == 25.0
        assert m.cpus == 8
        assert m.uptime_s == 123
