# pyright: reportAttributeAccessIssue=false
"""在 Modal 上部署 SafetyRAISE 专家小模型的 OpenAI 兼容服务。"""

from __future__ import annotations

import os
import subprocess

import modal

APP_NAME = "safetyraise-qwen3-expert"
MODEL_VOLUME_NAME = "safetyraise-qwen3-f16"
MODEL_VOLUME_PATH = "/model-volume"
MODEL_PATH = f"{MODEL_VOLUME_PATH}/models/TS-Qwen3"
SERVED_MODEL_NAME = "suyuan37/SafetyRAISE-TS-Qwen3"
MAX_MODEL_LEN = 12_288
PORT = 8000


def build_vllm_command() -> list[str]:
    """返回已通过 L4 烟测的单并发 vLLM 启动参数。"""
    return [
        "vllm",
        "serve",
        MODEL_PATH,
        "--served-model-name",
        SERVED_MODEL_NAME,
        "--host",
        "0.0.0.0",
        "--port",
        str(PORT),
        "--dtype",
        "float16",
        "--max-model-len",
        str(MAX_MODEL_LEN),
        "--max-num-seqs",
        "1",
        "--gpu-memory-utilization",
        "0.88",
        "--generation-config",
        MODEL_PATH,
        "--reasoning-parser",
        "qwen3",
        "--enforce-eager",
    ]


app = modal.App(APP_NAME)
model_volume = modal.Volume.from_name(MODEL_VOLUME_NAME, create_if_missing=False)

image = (
    modal.Image.from_registry(
        "nvidia/cuda:12.8.1-devel-ubuntu22.04",
        add_python="3.12",
    )
    .entrypoint([])
    .uv_pip_install("vllm==0.21.0")
    .env(
        {
            "HF_HUB_OFFLINE": "1",
            "TRANSFORMERS_OFFLINE": "1",
            "TOKENIZERS_PARALLELISM": "false",
            "VLLM_NO_USAGE_STATS": "1",
        }
    )
)


@app.function(
    image=image,
    gpu="L4",
    volumes={MODEL_VOLUME_PATH: model_volume},
    min_containers=0,
    max_containers=1,
    scaledown_window=60,
    timeout=3_600,
    region="eu",
    routing_region="eu-west",
)
@modal.concurrent(max_inputs=1)
@modal.web_server(PORT, startup_timeout=1_800, requires_proxy_auth=True)
def serve() -> None:
    """启动 vLLM；Modal 会在端口就绪前保持首个请求排队。"""
    print(
        "启动 SafetyRAISE 专家模型服务："
        f"region={os.environ.get('MODAL_REGION', 'unknown')}, "
        f"max_model_len={MAX_MODEL_LEN}, max_num_seqs=1"
    )
    subprocess.Popen(build_vllm_command())
