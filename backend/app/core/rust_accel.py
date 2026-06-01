import ctypes
import os
from pathlib import Path


_RUST_TOKEN_ACCEL = None
_RUST_TOKEN_ACCEL_LOAD_ERROR = ""


def load_rust_token_accel():  # noqa: ANN202
    global _RUST_TOKEN_ACCEL, _RUST_TOKEN_ACCEL_LOAD_ERROR
    if _RUST_TOKEN_ACCEL is not None:
        return _RUST_TOKEN_ACCEL
    if _RUST_TOKEN_ACCEL_LOAD_ERROR:
        return None

    candidates: list[Path] = []
    env_path = os.getenv("SAFETYRAISE_TOKEN_ACCEL_LIB", "").strip()
    if env_path:
        candidates.append(Path(env_path))

    native_dir = Path(__file__).resolve().parents[2] / "native" / "query_token_accel" / "target" / "release"
    candidates.extend(
        [
            native_dir / "query_token_accel.dll",
            native_dir / "libquery_token_accel.so",
            native_dir / "libquery_token_accel.dylib",
        ]
    )

    for candidate in candidates:
        if not candidate.exists():
            continue
        try:
            library = ctypes.CDLL(str(candidate))
            library.accel_tokenize_text.argtypes = [ctypes.c_char_p]
            library.accel_tokenize_text.restype = ctypes.c_void_p
            library.accel_tokenize_batch.argtypes = [ctypes.c_char_p]
            library.accel_tokenize_batch.restype = ctypes.c_void_p
            library.accel_score_records.argtypes = [ctypes.c_char_p]
            library.accel_score_records.restype = ctypes.c_void_p
            if hasattr(library, "accel_extract_json_candidates"):
                library.accel_extract_json_candidates.argtypes = [ctypes.c_char_p]
                library.accel_extract_json_candidates.restype = ctypes.c_void_p
            library.accel_free_string.argtypes = [ctypes.c_void_p]
            library.accel_free_string.restype = None
            _RUST_TOKEN_ACCEL = library
            return _RUST_TOKEN_ACCEL
        except OSError as exc:
            _RUST_TOKEN_ACCEL_LOAD_ERROR = f"{type(exc).__name__}: {exc}"
            return None

    _RUST_TOKEN_ACCEL_LOAD_ERROR = "not_built"
    return None
