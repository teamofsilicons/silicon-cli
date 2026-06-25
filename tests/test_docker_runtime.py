from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from silicon_cli import docker_runtime, registry, ui


class DockerRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.old_registry_dir = registry.REGISTRY_DIR
        self.old_registry_file = registry.REGISTRY_FILE
        self.old_config_file = docker_runtime.CONFIG_FILE
        self.old_env = {
            key: os.environ.get(key)
            for key in (
                "SILICON_CONTAINER_MODE",
                "SILICON_RUNTIME",
                "SILICON_RUNTIME_DOCKER",
                "SILICON_DOCKER_ROOT",
                "SILICON_DOCKER_COMPOSE",
                "SILICON_DOCKER_SHARED_HOME",
                "SILICON_RUNTIME_IMAGE",
                "SILICON_DOCKER_SUDO",
                "SILICON_DOCKER_AUTO_INSTALL",
            )
        }
        for key in self.old_env:
            os.environ.pop(key, None)
        registry.REGISTRY_DIR = self.root / ".silicon"
        registry.REGISTRY_FILE = registry.REGISTRY_DIR / "registry.json"
        docker_runtime.CONFIG_FILE = registry.REGISTRY_DIR / "docker.json"

    def tearDown(self):
        registry.REGISTRY_DIR = self.old_registry_dir
        registry.REGISTRY_FILE = self.old_registry_file
        docker_runtime.CONFIG_FILE = self.old_config_file
        for key, value in self.old_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        self.tmp.cleanup()

    def write_docker_config(self, image: str = "example/silicon:latest") -> dict:
        cfg = {
            "enabled": True,
            "root": str(self.root / "silicons"),
            "compose_file": str(self.root / "silicons" / "compose.yml"),
            "shared_home": str(self.root / "silicons" / ".shared-home"),
            "image": image,
        }
        docker_runtime.CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
        docker_runtime.CONFIG_FILE.write_text(json.dumps(cfg))
        return cfg

    def test_default_runtime_image_uses_published_registry(self):
        cfg = docker_runtime.load_config()

        self.assertEqual(cfg["image"], "ghcr.io/teamofsilicons/silicon-runtime:latest")

    def test_legacy_registry_rows_load_as_local(self):
        registry.REGISTRY_FILE.parent.mkdir(parents=True, exist_ok=True)
        registry.REGISTRY_FILE.write_text(json.dumps({
            "installations": [
                {"name": "ada", "path": "/tmp/ada", "pid_file": "/tmp/ada/.silicon.pid"}
            ]
        }))

        [inst] = registry.installs()

        self.assertEqual(inst.name, "ada")
        self.assertEqual(inst.runtime, "local")
        self.assertFalse(inst.is_docker)

    def test_register_instance_writes_compose_and_metadata(self):
        cfg = self.write_docker_config()
        instance = self.root / "silicons" / "ada"
        instance.mkdir(parents=True)

        inst = docker_runtime.register_instance("ada", instance)

        self.assertTrue(inst.is_docker)
        self.assertEqual(inst.service, "silicon-ada")
        self.assertEqual(inst.container_name, "silicon-ada")
        compose = Path(cfg["compose_file"]).read_text()
        self.assertIn("services:", compose)
        self.assertIn("silicon-ada:", compose)
        self.assertIn(f'{instance.resolve()}:/silicon', compose)
        self.assertIn(f'{Path(cfg["shared_home"]).resolve()}:/silicon-shared-home', compose)
        self.assertIn("SILICON_SHARED_HOME", compose)
        self.assertIn("example/silicon:latest", compose)

    def test_enabled_is_false_inside_container(self):
        self.write_docker_config()
        os.environ["SILICON_CONTAINER_MODE"] = "1"

        self.assertFalse(docker_runtime.enabled())

    def test_maintenance_run_uses_runtime_entrypoint(self):
        self.write_docker_config()
        instance = self.root / "silicons" / "ada"
        instance.mkdir(parents=True)
        inst = registry.Install(
            0,
            "ada",
            str(instance),
            str(instance / ".silicon.pid"),
            "docker",
            "silicon-ada",
            str(self.root / "silicons" / "compose.yml"),
            "example/silicon:latest",
            "silicon-ada",
        )
        captured = {}
        old_run = docker_runtime._run

        def fake_run(cmd, *, check=False, capture=False):
            captured["cmd"] = cmd
            return SimpleNamespace(returncode=0)

        docker_runtime._run = fake_run
        try:
            docker_runtime.maintenance_silicon(inst, ["update", "ada"])
        finally:
            docker_runtime._run = old_run

        cmd = captured["cmd"]
        self.assertEqual(cmd[:3], ["docker", "run", "--rm"])
        self.assertIn("--entrypoint", cmd)
        self.assertIn("/usr/local/bin/silicon-runtime-entrypoint", cmd)
        self.assertIn("SILICON_SHARED_HOME=/silicon-shared-home", cmd)
        self.assertIn(f'{Path(self.root / "silicons" / ".shared-home").resolve()}:/silicon-shared-home', cmd)
        self.assertEqual(cmd[-4:], ["run", "silicon", "update", "ada"])

    def test_maintenance_run_uses_sudo_docker_when_configured(self):
        self.write_docker_config()
        data = json.loads(docker_runtime.CONFIG_FILE.read_text())
        data["docker_sudo"] = True
        docker_runtime.CONFIG_FILE.write_text(json.dumps(data))
        instance = self.root / "silicons" / "ada"
        instance.mkdir(parents=True)
        inst = registry.Install(
            0,
            "ada",
            str(instance),
            str(instance / ".silicon.pid"),
            "docker",
            "silicon-ada",
            str(self.root / "silicons" / "compose.yml"),
            "example/silicon:latest",
            "silicon-ada",
        )
        captured = {}
        old_run = docker_runtime._run

        def fake_run(cmd, *, check=False, capture=False):
            captured["cmd"] = cmd
            return SimpleNamespace(returncode=0)

        docker_runtime._run = fake_run
        try:
            docker_runtime.maintenance_silicon(inst, ["update", "ada"])
        finally:
            docker_runtime._run = old_run

        self.assertEqual(captured["cmd"][:2], ["sudo", "docker"])

    def test_ensure_ready_auto_initializes_and_pulls_image(self):
        calls = []
        old_binary = docker_runtime._ensure_docker_binary
        old_daemon = docker_runtime._ensure_daemon
        old_compose = docker_runtime._ensure_compose
        old_image = docker_runtime._ensure_image

        def fake_binary(install):
            calls.append(("binary", install))

        def fake_daemon(config):
            calls.append(("daemon", config["image"]))
            return {**config, "docker_sudo": True}

        def fake_compose(config):
            calls.append(("compose", config["docker_sudo"]))

        def fake_image(config):
            calls.append(("image", config["image"]))

        docker_runtime._ensure_docker_binary = fake_binary
        docker_runtime._ensure_daemon = fake_daemon
        docker_runtime._ensure_compose = fake_compose
        docker_runtime._ensure_image = fake_image
        try:
            cfg = docker_runtime.ensure_ready(
                auto_init=True,
                root=str(self.root / "silicons"),
                image="example/silicon:latest",
            )
        finally:
            docker_runtime._ensure_docker_binary = old_binary
            docker_runtime._ensure_daemon = old_daemon
            docker_runtime._ensure_compose = old_compose
            docker_runtime._ensure_image = old_image

        self.assertTrue(cfg["enabled"])
        self.assertTrue(cfg["docker_sudo"])
        self.assertEqual(cfg["image"], "example/silicon:latest")
        self.assertEqual(cfg["shared_home"], str((self.root / "silicons" / ".shared-home").resolve()))
        self.assertTrue(Path(cfg["compose_file"]).exists())
        self.assertIn(("image", "example/silicon:latest"), calls)

    def test_pull_runtime_can_be_opted_out(self):
        os.environ["SILICON_RUNTIME"] = "local"
        old_ensure = docker_runtime.ensure_ready

        def fail_if_called(**_kwargs):
            raise AssertionError("ensure_ready should not be called")

        docker_runtime.ensure_ready = fail_if_called
        try:
            self.assertFalse(docker_runtime.ensure_pull_runtime())
        finally:
            docker_runtime.ensure_ready = old_ensure

    def test_noninteractive_docker_install_requires_explicit_env_opt_in(self):
        old_is_linux = docker_runtime._is_linux
        old_interactive = ui.interactive
        old_manual = docker_runtime._manual_docker_steps
        old_download = docker_runtime._download_docker_installer
        calls = []

        docker_runtime._is_linux = lambda: True
        ui.interactive = lambda: False
        docker_runtime._manual_docker_steps = lambda: calls.append("manual")

        def fail_download():
            raise AssertionError("installer should not be downloaded without opt-in")

        docker_runtime._download_docker_installer = fail_download
        try:
            self.assertFalse(docker_runtime._install_docker_engine())
        finally:
            docker_runtime._is_linux = old_is_linux
            ui.interactive = old_interactive
            docker_runtime._manual_docker_steps = old_manual
            docker_runtime._download_docker_installer = old_download

        self.assertEqual(calls, ["manual"])

    def test_auth_container_mounts_shared_home(self):
        cfg = self.write_docker_config()
        captured = {}
        old_run = docker_runtime._run

        def fake_run(cmd, *, check=False, capture=False):
            captured["cmd"] = cmd
            return SimpleNamespace(returncode=0)

        docker_runtime._run = fake_run
        try:
            self.assertEqual(docker_runtime._auth_container(cfg, "codex"), 0)
        finally:
            docker_runtime._run = old_run

        cmd = captured["cmd"]
        self.assertEqual(cmd[:3], ["docker", "run", "--rm"])
        self.assertIn("-it", cmd)
        self.assertIn(f'{Path(cfg["shared_home"]).resolve()}:/silicon-shared-home', cmd)
        self.assertIn("SILICON_SHARED_HOME=/silicon-shared-home", cmd)
        self.assertEqual(cmd[-2:], ["auth", "codex"])

    def test_shared_tool_container_forwards_args(self):
        cfg = self.write_docker_config()
        captured = {}
        old_run = docker_runtime._run
        old_interactive = ui.interactive

        def fake_run(cmd, *, check=False, capture=False):
            captured["cmd"] = cmd
            return SimpleNamespace(returncode=0)

        docker_runtime._run = fake_run
        ui.interactive = lambda: True
        try:
            self.assertEqual(docker_runtime._shared_tool_container(cfg, "claude", ["--version"]), 0)
        finally:
            docker_runtime._run = old_run
            ui.interactive = old_interactive

        cmd = captured["cmd"]
        self.assertEqual(cmd[:3], ["docker", "run", "--rm"])
        self.assertIn("-it", cmd)
        self.assertIn(f'{Path(cfg["shared_home"]).resolve()}:/silicon-shared-home', cmd)
        self.assertIn("SILICON_SHARED_HOME=/silicon-shared-home", cmd)
        self.assertEqual(cmd[-3:], ["shared", "claude", "--version"])


if __name__ == "__main__":
    unittest.main()
