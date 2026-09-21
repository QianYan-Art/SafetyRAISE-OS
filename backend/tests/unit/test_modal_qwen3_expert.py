from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace


class _FakeImage:
    def __init__(self, registry: str, *, add_python: str):
        self.registry = registry
        self.add_python = add_python
        self.packages: tuple[str, ...] = ()
        self.environment: dict[str, str] = {}

    @classmethod
    def from_registry(cls, registry: str, *, add_python: str):
        return cls(registry, add_python=add_python)

    def entrypoint(self, _value: list[str]):
        return self

    def uv_pip_install(self, *packages: str):
        self.packages = packages
        return self

    def env(self, values: dict[str, str]):
        self.environment = values
        return self


class _FakeVolume:
    @classmethod
    def from_name(cls, name: str, *, create_if_missing: bool):
        return SimpleNamespace(name=name, create_if_missing=create_if_missing)


class _FakeApp:
    def __init__(self, name: str):
        self.name = name

    def function(self, **kwargs):
        return _record_decorator("_modal_function_kwargs", kwargs)


class _ModalStub(ModuleType):
    App = _FakeApp
    Image = _FakeImage
    Volume = _FakeVolume

    @staticmethod
    def concurrent(**kwargs):
        return _record_decorator("_modal_concurrent_kwargs", kwargs)

    @staticmethod
    def web_server(*args, **kwargs):
        return _record_decorator(
            "_modal_web_server_kwargs",
            {"args": args, **kwargs},
        )


def _record_decorator(attribute: str, values: dict[str, object]):
    def decorator(function):
        setattr(function, attribute, values)
        return function

    return decorator


def _load_script(monkeypatch) -> ModuleType:
    modal_stub = _ModalStub("modal")
    monkeypatch.setitem(sys.modules, "modal", modal_stub)

    path = Path(__file__).resolve().parents[3] / "deployment" / "modal" / "qwen3_expert.py"
    spec = importlib.util.spec_from_file_location("safetyraise_modal_qwen3_expert", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_modal_expert_deployment_contract(monkeypatch):
    module = _load_script(monkeypatch)

    assert module.APP_NAME == "safetyraise-qwen3-expert"
    assert module.MODEL_VOLUME_NAME == "safetyraise-qwen3-f16"
    assert module.SERVED_MODEL_NAME == "suyuan37/SafetyRAISE-TS-Qwen3"
    assert module.MAX_MODEL_LEN == 12_288
    assert module.app.name == module.APP_NAME
    assert module.model_volume.name == module.MODEL_VOLUME_NAME
    assert module.model_volume.create_if_missing is False
    assert module.image.packages == ("vllm==0.21.0",)

    function_options = module.serve._modal_function_kwargs
    assert function_options["gpu"] == "L4"
    assert function_options["min_containers"] == 0
    assert function_options["max_containers"] == 1
    assert function_options["scaledown_window"] == 60
    assert function_options["region"] == "eu"
    assert function_options["routing_region"] == "eu-west"
    assert module.serve._modal_concurrent_kwargs == {"max_inputs": 1}
    assert module.serve._modal_web_server_kwargs == {
        "args": (module.PORT,),
        "startup_timeout": 1_800,
        "requires_proxy_auth": True,
    }

    command = module.build_vllm_command()
    assert command[command.index("--max-model-len") + 1] == "12288"
    assert command[command.index("--max-num-seqs") + 1] == "1"
    assert command[command.index("--reasoning-parser") + 1] == "qwen3"
    assert command[command.index("--served-model-name") + 1] == module.SERVED_MODEL_NAME
    assert not {"--max-tokens", "--max-new-tokens", "--max-output-tokens"} & set(command)
