"""给旧上传工作区补服务器归属标记；默认只核对，不删除任何资料。"""

import argparse
import json
from collections import defaultdict
from pathlib import Path

from app.core.settings import load_settings
from app.services.database_service import DatabaseService


def backfill(settings, *, apply: bool = False) -> dict[str, int]:
    database = DatabaseService(settings)
    root = Path(settings.input_generation_workspace_dir_path).resolve()
    references: dict[Path, set[str]] = defaultdict(set)
    with database.connection() as conn:
        rows = conn.execute("SELECT id, draft_meta, report_result FROM chat_sessions").fetchall()
    for row in rows:
        meta = row["draft_meta"] or {}
        result = row["report_result"] or {}
        for payload in (meta, result.get("input_generation") or {}):
            raw = payload.get("workspace_dir")
            if not raw:
                continue
            path = Path(str(raw))
            if not path.is_absolute():
                continue
            # 只接管原生成器的一级随机工作区，不跟随符号链接或根目录引用。
            if path.is_symlink() or path.parent.resolve() != root:
                continue
            resolved = path.resolve()
            if resolved.parent != root or not resolved.name.startswith("input-"):
                continue
            references[resolved].add(str(row["id"]))
    counts = {"candidates": 0, "marked": 0, "already_marked": 0, "conflicts": 0, "missing": 0}
    for path, owners in references.items():
        if len(owners) != 1:
            counts["conflicts"] += 1
            continue
        if not path.is_dir():
            counts["missing"] += 1
            continue
        counts["candidates"] += 1
        session_id = next(iter(owners))
        marker = path / ".session-owner.json"
        if marker.exists() or marker.is_symlink():
            try:
                matches = not marker.is_symlink() and json.loads(marker.read_text("utf-8")).get("session_id") == session_id
            except (OSError, ValueError, AttributeError):
                matches = False
            counts["already_marked" if matches else "conflicts"] += 1
            continue
        if apply:
            # exclusive create，绝不覆盖已有归属；本脚本应在应用暂停写入时运行。
            with marker.open("x", encoding="utf-8") as stream:
                json.dump({"version": 1, "session_id": session_id}, stream)
            counts["marked"] += 1
    return counts


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    result = backfill(load_settings(), apply=args.apply)
    print(json.dumps(result))
    if result["conflicts"]:
        raise SystemExit(2)
