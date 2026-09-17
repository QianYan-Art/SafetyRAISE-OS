from __future__ import annotations

import argparse
import json
from pathlib import Path


def replay_events(events: list[dict]) -> dict:
    """纯本地重放公开轨迹，不导入服务、数据库或网络执行器。"""
    if not events:
        raise ValueError("重放至少需要一个事件")
    run_id = events[0]["run_id"]
    last_seq, last_version, state = 0, 0, None
    publication_budget = None
    for event in events:
        if event["run_id"] != run_id or event["seq"] != last_seq + 1:
            raise ValueError("事件混入其他运行或序号不连续")
        if event["state_version"] < last_version:
            raise ValueError("状态版本倒退")
        data = event["data"]
        if event["type"] in {"stage", "final"} and data.get("state"):
            state = data["state"]
        if event["type"] == "final" and data.get("state") == "published":
            if publication_budget is not None:
                raise ValueError("重复发布事件")
            publication_budget = data["budget"]
        last_seq, last_version = event["seq"], event["state_version"]
    return {
        "run_id": run_id, "last_event_seq": last_seq, "state_version": last_version,
        "observed_state": state, "publication_budget": publication_budget,
        "mode": "read_only_replay", "quality_gate": "engineering_only",
    }


def main():
    parser = argparse.ArgumentParser(description="仅重放已保存的公开运行事件")
    parser.add_argument("events", type=Path)
    args = parser.parse_args()
    print(json.dumps(replay_events(json.loads(args.events.read_text(encoding="utf-8"))),
                     ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
