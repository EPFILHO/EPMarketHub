from __future__ import annotations

import logging
import re
import shutil
import subprocess
import sys
from collections.abc import Iterable
from pathlib import Path
from uuid import uuid4

import psutil

from .models import TerminalProfile
from .terminal_states import InstanceIntegrityState

logger = logging.getLogger(__name__)


class TerminalManager:
    """Cria, abre, renomeia, detecta e encerra instâncias MT5 controladas."""

    def __init__(self, instances_dir: Path, base_mt5_dir: Path):
        self.instances_dir = instances_dir.resolve()
        self.base_mt5_dir = base_mt5_dir.resolve()
        self.instances_dir.mkdir(parents=True, exist_ok=True)
        self.base_mt5_dir.mkdir(parents=True, exist_ok=True)
        self._processes: dict[str, subprocess.Popen] = {}
        self._known_profiles: dict[str, TerminalProfile] = {}

    @staticmethod
    def sanitize_id(value: str) -> str:
        clean = re.sub(r"[^A-Za-z0-9_.-]+", "-", str(value or "").strip())
        clean = re.sub(r"-+", "-", clean).strip("-")
        return clean or "mt5-terminal"

    @classmethod
    def build_instance_slug(cls, broker_name: str, account_login: str) -> str:
        return cls.sanitize_id(f"{broker_name}-{account_login}").upper()

    def base_status(self) -> dict:
        terminal_exe = self.base_mt5_dir / "terminal64.exe"
        missing = [] if terminal_exe.is_file() else ["terminal64.exe"]
        return {
            "ok": not missing,
            "path": str(self.base_mt5_dir),
            "missing": missing,
            "message": (
                "Arquivo-base terminal64.exe pronto para criar novas instâncias."
                if not missing
                else "Copie apenas o terminal64.exe para a pasta MT5 do EP Market Hub."
            ),
        }

    def instance_status(self, profile: TerminalProfile) -> dict:
        """Descreve a integridade local sem alterar o cadastro ou a pasta."""

        instance_dir = Path(profile.instance_dir).resolve()
        terminal_exe = instance_dir / "terminal64.exe"
        if not self._is_inside_instances(instance_dir):
            state = InstanceIntegrityState.INVALID_PATH.value
            message = "A pasta cadastrada não pertence à área controlada do EP Market Hub."
        elif not instance_dir.exists():
            state = InstanceIntegrityState.DIRECTORY_MISSING.value
            message = "A pasta local desta instância não foi encontrada."
        elif not instance_dir.is_dir():
            state = InstanceIntegrityState.INVALID_PATH.value
            message = "O caminho esperado da instância existe, mas não é uma pasta."
        elif not terminal_exe.is_file():
            state = InstanceIntegrityState.EXECUTABLE_MISSING.value
            message = "A pasta existe, mas o terminal64.exe não foi encontrado."
        else:
            state = InstanceIntegrityState.READY.value
            message = "Instância local pronta."
        return {
            "ready": state == InstanceIntegrityState.READY.value,
            "state": state,
            "path": str(instance_dir),
            "terminal_exe": str(terminal_exe),
            "message": message,
        }

    def instance_status_for_slug(self, instance_slug: str) -> dict:
        slug = self.sanitize_id(instance_slug).upper()
        instance_dir = (self.instances_dir / slug).resolve()
        profile = TerminalProfile(
            id="",
            label=slug,
            instance_slug=slug,
            instance_dir=str(instance_dir),
            terminal_exe=str(instance_dir / "terminal64.exe"),
        )
        return self.instance_status(profile)

    def remember(self, profile: TerminalProfile) -> None:
        self._known_profiles[profile.id] = profile

    def create_instance_from_base(self, instance_slug: str, overwrite: bool = False) -> Path:
        status = self.base_status()
        if not status["ok"]:
            missing = ", ".join(status["missing"])
            raise FileNotFoundError(
                f"Instalação-base incompleta em {self.base_mt5_dir}. Itens ausentes: {missing}"
            )

        instance_slug = self.sanitize_id(instance_slug).upper()
        instance_dir = self.instances_dir / instance_slug
        if instance_dir.exists() and not overwrite:
            raise FileExistsError(f"Já existe uma pasta de instância com este nome: {instance_dir.name}")
        if instance_dir.exists() and overwrite:
            shutil.rmtree(instance_dir)

        logger.info("Criando instância MT5 controlada em %s", instance_dir)
        source_exe = self.base_mt5_dir / "terminal64.exe"
        instance_dir.mkdir(parents=True, exist_ok=False)
        terminal_exe = instance_dir / "terminal64.exe"
        try:
            shutil.copy2(source_exe, terminal_exe)
        except Exception:
            shutil.rmtree(instance_dir, ignore_errors=True)
            raise

        if not terminal_exe.is_file():
            shutil.rmtree(instance_dir, ignore_errors=True)
            raise FileNotFoundError(f"Instância criada sem terminal64.exe: {terminal_exe}")
        return terminal_exe

    def repair_instance_from_base(self, profile: TerminalProfile) -> Path:
        """Recria apenas o executável ausente, preservando uma pasta ainda existente."""

        status = self.base_status()
        if not status["ok"]:
            missing = ", ".join(status["missing"])
            raise FileNotFoundError(
                f"Instalação-base incompleta em {self.base_mt5_dir}. Itens ausentes: {missing}"
            )

        instance_dir = Path(profile.instance_dir).resolve()
        if not self._is_inside_instances(instance_dir):
            raise ValueError("A instância não está dentro da pasta controlada do EP Market Hub.")
        if self.is_running(profile.id, profile):
            raise RuntimeError("Feche o MT5 antes de recriar a instância local.")

        terminal_exe = instance_dir / "terminal64.exe"
        if terminal_exe.is_file():
            return terminal_exe
        if instance_dir.exists() and not instance_dir.is_dir():
            raise NotADirectoryError(f"O caminho da instância não é uma pasta: {instance_dir}")

        created_dir = not instance_dir.exists()
        if created_dir:
            instance_dir.mkdir(parents=True, exist_ok=False)

        temporary = instance_dir / f".terminal64.exe.recreate-{uuid4().hex[:10]}.tmp"
        try:
            shutil.copy2(self.base_mt5_dir / "terminal64.exe", temporary)
            temporary.replace(terminal_exe)
        except Exception:
            temporary.unlink(missing_ok=True)
            if created_dir:
                shutil.rmtree(instance_dir, ignore_errors=True)
            raise

        if not terminal_exe.is_file():
            if created_dir:
                shutil.rmtree(instance_dir, ignore_errors=True)
            raise FileNotFoundError(f"Instância recriada sem terminal64.exe: {terminal_exe}")
        return terminal_exe

    def rollback_created_instance(self, instance_dir: Path) -> bool:
        """Remove somente uma pasta recém-criada dentro da área controlada."""

        instance_dir = instance_dir.resolve()
        if instance_dir == self.instances_dir or not self._is_inside_instances(instance_dir):
            raise ValueError("A instância não está dentro da pasta controlada do EP Market Hub.")
        if not instance_dir.exists():
            return False
        logger.info("Removendo instância MT5 após falha no cadastro: %s", instance_dir)
        shutil.rmtree(instance_dir)
        return True

    def rename_instance(self, profile: TerminalProfile, new_slug: str) -> tuple[Path, Path]:
        """Renomeia uma instância já fechada e retorna (pasta, terminal64.exe)."""

        source = Path(profile.instance_dir).resolve()
        if not source.exists():
            raise FileNotFoundError(f"Pasta atual da instância não encontrada: {source}")
        if not self._is_inside_instances(source):
            raise ValueError("A instância não está dentro da pasta controlada do EP Market Hub.")

        new_slug = self.sanitize_id(new_slug).upper()
        target = (self.instances_dir / new_slug).resolve()
        if source.name == target.name:
            return source, source / "terminal64.exe"

        same_path_ignoring_case = self._normalized(source) == self._normalized(target)
        if target.exists() and not same_path_ignoring_case:
            raise FileExistsError(f"Já existe uma instância chamada {target.name}.")

        logger.info("Renomeando instância MT5 de %s para %s", source, target)
        if same_path_ignoring_case:
            # No Windows, uma troca apenas de caixa precisa passar por um nome temporário.
            temporary = source.with_name(f".RENAME-{uuid4().hex[:10]}")
            source.rename(temporary)
            temporary.rename(target)
        else:
            source.rename(target)
        terminal_exe = target / "terminal64.exe"
        if not terminal_exe.exists():
            try:
                target.rename(source)
            except OSError:
                logger.exception("Falha ao desfazer renomeação sem terminal64.exe")
            raise FileNotFoundError(f"A pasta renomeada não contém terminal64.exe: {terminal_exe}")
        return target, terminal_exe

    def rollback_rename(self, current_dir: Path, original_dir: Path) -> None:
        current_dir = current_dir.resolve()
        original_dir = original_dir.resolve()
        if current_dir == original_dir or not current_dir.exists() or original_dir.exists():
            return
        current_dir.rename(original_dir)


    @staticmethod
    def _post_windows_close(pid: int) -> bool:
        """Solicita WM_CLOSE às janelas do processo; retorna se encontrou alguma."""

        if not sys.platform.startswith("win"):
            return False
        try:
            import ctypes
            from ctypes import wintypes

            user32 = ctypes.windll.user32
            found = False
            WM_CLOSE = 0x0010
            callback_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

            @callback_type
            def enum_callback(hwnd, _lparam):
                nonlocal found
                window_pid = wintypes.DWORD()
                user32.GetWindowThreadProcessId(hwnd, ctypes.byref(window_pid))
                if window_pid.value == pid:
                    found = True
                    user32.PostMessageW(hwnd, WM_CLOSE, 0, 0)
                return True

            user32.EnumWindows(enum_callback, 0)
            return found
        except Exception:
            logger.exception("Não foi possível solicitar fechamento gracioso do PID %s", pid)
            return False

    @classmethod
    def _close_process(cls, process, timeout: int) -> bool:
        """Tenta fechar normalmente e usa terminate/kill apenas como fallback."""

        try:
            pid = int(process.pid)
        except Exception:
            pid = 0

        if pid and cls._post_windows_close(pid):
            try:
                process.wait(timeout=timeout)
                return True
            except (subprocess.TimeoutExpired, psutil.TimeoutExpired):
                pass
            except (psutil.NoSuchProcess, ProcessLookupError):
                return True

        try:
            process.terminate()
            process.wait(timeout=timeout)
            return True
        except (subprocess.TimeoutExpired, psutil.TimeoutExpired):
            try:
                process.kill()
                process.wait(timeout=2)
                return True
            except (psutil.NoSuchProcess, ProcessLookupError):
                return True
            except (subprocess.TimeoutExpired, psutil.TimeoutExpired):
                logger.error("PID %s permaneceu vivo após terminate/kill.", pid)
                return False
            except (psutil.AccessDenied, PermissionError):
                logger.exception("Sem permissão para forçar o encerramento do PID %s", pid)
                return False
            except Exception:
                logger.exception("Falha ao forçar o encerramento do PID %s", pid)
                return False
        except (psutil.NoSuchProcess, ProcessLookupError):
            return True
        except (psutil.AccessDenied, PermissionError):
            return False
        except Exception:
            logger.exception("Falha ao encerrar o PID %s", pid)
            return False

    def launch(self, profile: TerminalProfile, minimized: bool = True) -> subprocess.Popen | None:
        terminal_exe = Path(profile.terminal_exe)
        if not terminal_exe.exists():
            raise FileNotFoundError(f"terminal64.exe não encontrado: {terminal_exe}")

        self.remember(profile)
        existing = self._processes.get(profile.id)
        if existing and existing.poll() is None:
            return existing
        if self._find_processes(profile):
            return None

        args = [str(terminal_exe)]
        if profile.portable:
            args.append("/portable")

        startupinfo = None
        if sys.platform.startswith("win") and minimized:
            startupinfo = subprocess.STARTUPINFO()
            startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
            startupinfo.wShowWindow = 6  # SW_MINIMIZE

        proc = subprocess.Popen(args, cwd=str(terminal_exe.parent), startupinfo=startupinfo)
        self._processes[profile.id] = proc
        return proc

    def stop(self, terminal_id: str, timeout: int = 8, profile: TerminalProfile | None = None) -> bool:
        proc = self._processes.get(terminal_id)
        stopped = False
        if proc:
            if proc.poll() is None:
                stopped = self._close_process(proc, timeout) or stopped
            self._processes.pop(terminal_id, None)

        profile = profile or self._known_profiles.get(terminal_id)
        if profile:
            self.remember(profile)
            for external in self._find_processes(profile):
                stopped = self._close_process(external, timeout) or stopped
        return stopped


    def running_count(self, profiles: Iterable[TerminalProfile]) -> int:
        return sum(1 for profile in profiles if self.is_running(profile.id, profile))

    def forget(self, terminal_id: str) -> None:
        self._processes.pop(terminal_id, None)
        self._known_profiles.pop(terminal_id, None)

    def stage_delete_instance(self, profile: TerminalProfile) -> tuple[Path, Path]:
        """Move a pasta para um nome temporário antes da exclusão definitiva."""

        source = Path(profile.instance_dir).resolve()
        if not source.exists():
            raise FileNotFoundError(f"Pasta da instância não encontrada: {source}")
        if not self._is_inside_instances(source):
            raise ValueError("A instância não está dentro da pasta controlada do EP Market Hub.")
        if self.is_running(profile.id, profile):
            raise RuntimeError("Feche o MT5 antes de excluir a instância.")

        staged = self.instances_dir / f".DELETING-{profile.id}-{uuid4().hex[:8]}"
        if staged.exists():
            shutil.rmtree(staged, ignore_errors=True)
        source.rename(staged)
        return source, staged

    @staticmethod
    def restore_staged_instance(original: Path, staged: Path) -> None:
        if staged.exists() and not original.exists():
            staged.rename(original)

    @staticmethod
    def finalize_staged_delete(staged: Path) -> None:
        if staged.exists():
            shutil.rmtree(staged)

    def stop_all(self, profiles: Iterable[TerminalProfile], timeout: int = 8) -> int:
        """Fecha todos os MT5 controlados, inclusive os abertos fora desta execução."""

        stopped = 0
        for profile in profiles:
            try:
                self.remember(profile)
                stop_requested = self.stop(profile.id, timeout=timeout, profile=profile)
                still_running = self.is_running(profile.id, profile)
                if stop_requested and not still_running:
                    stopped += 1
                if still_running:
                    logger.error(
                        "O MT5 %s permaneceu aberto após o encerramento do aplicativo.",
                        profile.id,
                    )
            except Exception:
                logger.exception("Falha inesperada ao encerrar o MT5 %s", profile.id)
        return stopped

    def is_running(self, terminal_id: str, profile: TerminalProfile | None = None) -> bool:
        proc = self._processes.get(terminal_id)
        if proc and proc.poll() is None:
            return True
        profile = profile or self._known_profiles.get(terminal_id)
        if profile:
            self.remember(profile)
            return bool(self._find_processes(profile))
        return False

    def is_executable_running(self, terminal_exe: str | Path) -> bool:
        return bool(self._find_processes_for_executable(terminal_exe))

    def process_count(self, profile: TerminalProfile) -> int:
        processes = self._find_processes(profile)
        tracked = self._processes.get(profile.id)
        tracked_count = int(bool(tracked and tracked.poll() is None))
        # O terminal pode substituir o PID usado no lançamento durante seu bootstrap.
        # Nesse intervalo, o Popen e a varredura do Windows representam a mesma
        # instância lógica e não devem ser somados. Duplicidade real continua sendo
        # detectada quando a própria tabela de processos contém mais de uma entrada.
        return max(len(processes), tracked_count)

    def _is_inside_instances(self, path: Path) -> bool:
        try:
            relative = path.resolve().relative_to(self.instances_dir)
            return relative != Path(".")
        except ValueError:
            return False

    @staticmethod
    def _normalized(path: str | Path) -> str:
        try:
            return str(Path(path).resolve()).lower()
        except Exception:
            return str(path).lower()

    def _find_processes_for_executable(self, terminal_exe: str | Path) -> list[psutil.Process]:
        target = self._normalized(terminal_exe)
        found: list[psutil.Process] = []
        for process in psutil.process_iter(["pid", "exe", "name"]):
            try:
                exe = process.info.get("exe")
                if exe and self._normalized(exe) == target:
                    found.append(process)
            except (psutil.NoSuchProcess, psutil.AccessDenied, OSError):
                continue
        return found

    def _find_processes(self, profile: TerminalProfile) -> list[psutil.Process]:
        return self._find_processes_for_executable(profile.terminal_exe)
