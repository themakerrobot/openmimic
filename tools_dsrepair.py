#!/usr/bin/env python
# Copyright 2025 The HuggingFace Inc. team. All rights reserved.
# (Adapted from huggingface/leLab lelab/dataset_repair.py — Apache License 2.0.
#  변경: 진입점을 데이터셋 폴더 경로로 받고, 고치기 전에 meta/·data/ 를 백업하며, CLI 를 붙였습니다.)
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""마무리(finalize) 안 된 수집 데이터셋 복구 — 강제 종료·정전·크래시로 끊긴 녹화.

  python tools_dsrepair.py <데이터셋 폴더>            # 필요하면 복구
  python tools_dsrepair.py <데이터셋 폴더> --check    # 상태만 (0=정상, 3=복구 필요)

LeRobotDataset.finalize() 가 meta/episodes/ 와 parquet 꼬리(footer)를 씁니다. 그 전에 끊기면 프레임·영상·info.json 은
있는데 에피소드 색인이 없어 데이터셋이 안 열립니다. 디스크에 남은 읽을 수 있는 parquet·영상으로 색인을 다시 만듭니다.
쓰던 중이던 마지막 parquet 파일(꼬리 없음)과 그 뒤의 프레임은 살릴 수 없습니다.

고치기 전에 <상위 폴더>/.repair_backup/<이름>_<시각>/ 에 meta/ 와 data/(parquet) 를 복사해 둡니다 (영상은 건드리지 않음).
"""

import json
import logging
import re
import shutil
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_CHUNK_FILE_RE = re.compile(r"chunk-(\d+)/file-(\d+)\.")


class DatasetRepairError(Exception):
    """The dataset is damaged beyond what can be rebuilt from disk."""


def _chunk_file(path: Path) -> tuple[int, int]:
    match = _CHUNK_FILE_RE.search(path.as_posix())
    if match is None:
        raise DatasetRepairError(f"Unexpected dataset file layout: {path}")
    return int(match.group(1)), int(match.group(2))


def _video_files(root: Path, video_key: str) -> list[tuple[int, int, float]]:
    """Return (chunk_index, file_index, duration_s) per encoded video file."""
    import av

    files = []
    for path in sorted((root / "videos" / video_key).rglob("*.mp4"), key=_chunk_file):
        try:
            with av.open(str(path)) as container:
                duration = float(container.duration) / av.time_base if container.duration else 0.0
        except Exception as e:
            # An unclosed mp4 has no moov atom; treat it as carrying no episodes.
            logger.warning("Could not read %s (%s) — episodes in it are unrecoverable", path, e)
            duration = 0.0
        chunk_index, file_index = _chunk_file(path)
        files.append((chunk_index, file_index, duration))
    return files


def _episodes_from_data(root: Path) -> tuple[list[dict[str, Any]], list[Path]]:
    """Reconstruct one row per episode from the readable data files.

    Also returns the trailing files that have no parquet footer — the reader
    globs every file under ``data/``, so they have to be moved out of the way.
    """
    import pandas as pd
    import pyarrow.parquet as pq

    # Written with the first saved episode, so its absence means the session
    # died before one completed.
    if not (root / "meta" / "tasks.parquet").exists():
        return [], []

    tasks = pd.read_parquet(root / "meta" / "tasks.parquet")
    task_by_index = {int(index): task for task, index in tasks["task_index"].items()}

    data_files = sorted((root / "data").rglob("*.parquet"), key=_chunk_file)
    rows: list[dict[str, Any]] = []
    next_index = 0
    for position, path in enumerate(data_files):
        try:
            frames = pq.read_table(path, columns=["episode_index", "task_index"]).to_pandas()
        except Exception as e:
            # No footer: the writer died mid-file. Nothing from here on is
            # readable, since only the newest file is ever left open.
            logger.warning("Data file %s is truncated (%s) — recovering the episodes before it", path, e)
            return rows, data_files[position:]

        chunk_index, file_index = _chunk_file(path)
        for episode_index, group in frames.groupby("episode_index", sort=True):
            length = len(group)
            rows.append(
                {
                    "episode_index": int(episode_index),
                    "tasks": [task_by_index[int(i)] for i in sorted(group["task_index"].unique())],
                    "length": length,
                    "data/chunk_index": chunk_index,
                    "data/file_index": file_index,
                    "dataset_from_index": next_index,
                    "dataset_to_index": next_index + length,
                    "meta/episodes/chunk_index": 0,
                    "meta/episodes/file_index": 0,
                }
            )
            next_index += length
    return rows, []


def _assign_videos(rows: list[dict[str, Any]], root: Path, video_key: str, fps: int) -> int:
    """Fill in the video columns for `video_key`, in place.

    Episodes are concatenated into a video file until it hits the size limit,
    so each episode's window is its predecessors' durations within that file.
    Returns how many leading rows are actually covered by encoded video.
    """
    files = _video_files(root, video_key)
    cursor = 0
    offset = 0.0
    tolerance = 1.0 / fps

    for covered, row in enumerate(rows):
        duration = row["length"] / fps
        while cursor < len(files) and offset + duration > files[cursor][2] + tolerance:
            cursor += 1
            offset = 0.0
        if cursor >= len(files):
            return covered

        chunk_index, file_index, _ = files[cursor]
        row[f"videos/{video_key}/chunk_index"] = chunk_index
        row[f"videos/{video_key}/file_index"] = file_index
        row[f"videos/{video_key}/from_timestamp"] = offset
        row[f"videos/{video_key}/to_timestamp"] = offset + duration
        offset += duration

    return len(rows)


def _trim_data(root: Path, keep_through: int) -> int:
    """Drop data rows belonging to episodes past `keep_through`.

    The reader loads every row under data/ and push_to_hub uploads the whole
    directory, so rows the rebuilt index no longer references would otherwise
    travel with the dataset. Returns the number of frames dropped.
    """
    import pyarrow.compute as pc
    import pyarrow.parquet as pq

    dropped = 0
    for path in sorted((root / "data").rglob("*.parquet"), key=_chunk_file):
        table = pq.read_table(path)
        kept = table.filter(pc.less_equal(table["episode_index"], keep_through))
        if kept.num_rows == table.num_rows:
            continue
        dropped += table.num_rows - kept.num_rows
        if kept.num_rows == 0:
            path.unlink()
        else:
            pq.write_table(kept, path, compression="snappy", use_dictionary=True)
    return dropped


def _episode_video_frames(root: Path, video_key: str, row: dict[str, Any]) -> "np.ndarray":  # noqa: F821
    """Decode a sample of the frames one episode references, as (N, C, H, W) in [0, 1]."""
    import av
    import numpy as np

    from lerobot.datasets.compute_stats import sample_indices

    file_index = row[f"videos/{video_key}/file_index"]
    start = row[f"videos/{video_key}/from_timestamp"]
    end = row[f"videos/{video_key}/to_timestamp"]

    path = next((root / "videos" / video_key).rglob(f"*file-{file_index:03d}.mp4"))
    with av.open(str(path)) as container:
        frames = [
            frame
            for frame in container.decode(container.streams.video[0])
            if start <= float(frame.time) < end
        ]

    if not frames:
        raise DatasetRepairError(f"No decodable frames for {video_key} in episode {row['episode_index']}")

    sampled = [frames[i] for i in sample_indices(len(frames))]
    return np.stack([frame.to_ndarray(format="rgb24").transpose(2, 0, 1) for frame in sampled]) / 255.0


def _recompute_stats(root: Path, info: dict[str, Any], rows: list[dict[str, Any]]) -> None:
    """Rewrite meta/stats.json over the retained frames only.

    stats.json is aggregated as each episode is saved, so it still describes
    episodes that were lost with the interruption, and training normalizes with
    it. Rebuilt the way recording builds it, per episode then aggregated, so the
    numbers match what the same episodes would have produced on their own.
    """
    import numpy as np
    import pyarrow.parquet as pq

    from lerobot.datasets.compute_stats import aggregate_stats, get_feature_stats
    from lerobot.datasets.io_utils import write_stats

    columns: dict[str, np.ndarray] = {}  # noqa: F821
    for path in sorted((root / "data").rglob("*.parquet"), key=_chunk_file):
        table = pq.read_table(path)
        for key in table.schema.names:
            values = np.stack(table[key].to_numpy(zero_copy_only=False).tolist())
            columns[key] = np.concatenate([columns[key], values]) if key in columns else values

    per_episode = []
    for row in rows:
        episode_stats = {}
        for key, feature in info["features"].items():
            if feature["dtype"] in {"string", "language"}:
                continue

            if feature["dtype"] in {"image", "video"}:
                values = _episode_video_frames(root, key, row)
                episode_stats[key] = {
                    name: value if name == "count" else np.squeeze(value, axis=0)
                    for name, value in get_feature_stats(values, axis=(0, 2, 3), keepdims=True).items()
                }
            elif key in columns:
                values = columns[key][row["dataset_from_index"] : row["dataset_to_index"]]
                episode_stats[key] = get_feature_stats(values, axis=0, keepdims=values.ndim == 1)
        per_episode.append(episode_stats)

    write_stats(aggregate_stats(per_episode), root)


def _write_episodes(root: Path, rows: list[dict[str, Any]]) -> None:
    import pyarrow as pa
    import pyarrow.parquet as pq

    episodes_dir = root / "meta" / "episodes"
    shutil.rmtree(episodes_dir, ignore_errors=True)
    path = episodes_dir / "chunk-000" / "file-000.parquet"
    path.parent.mkdir(parents=True, exist_ok=True)
    pq.write_table(pa.Table.from_pylist(rows), path, compression="snappy", use_dictionary=True)



def needs_repair(root: Path) -> bool:
    from lerobot.datasets.io_utils import load_episodes
    if not (root / "meta" / "info.json").exists():
        return False
    try:
        load_episodes(root)
        return False
    except Exception:
        return True


def repair_dataset(root) -> str | None:
    """root 의 에피소드 색인을 다시 만듭니다. 이미 정상이면 None, 살릴 게 없으면 DatasetRepairError."""
    import time

    root = Path(root).resolve()
    if not needs_repair(root):
        return None

    # 데이터셋 폴더 밖에 둡니다 — 안에 두면 다운로드(tar)·Hub 업로드에 같이 실려 갑니다
    backup = root.parent / ".repair_backup" / f"{root.name}_{time.strftime('%Y%m%d_%H%M%S')}"
    shutil.copytree(root / "meta", backup / "meta")
    if (root / "data").is_dir():
        shutil.copytree(root / "data", backup / "data")
    print(f"백업: {backup}", flush=True)

    info = json.loads((root / "meta" / "info.json").read_text())
    fps = info["fps"]
    recorded_episodes = info.get("total_episodes", 0)

    rows, truncated = _episodes_from_data(root)
    for video_key, feature in info["features"].items():
        if feature["dtype"] == "video":
            del rows[_assign_videos(rows, root, video_key, fps) :]

    if not rows:
        raise DatasetRepairError(
            "온전히 저장된 에피소드가 하나도 없습니다 — 남은 파일을 읽을 수 없어 복구할 수 없습니다. 다시 수집하세요.")

    for path in truncated:
        path.rename(path.with_suffix(".parquet.unreadable"))

    lost = recorded_episodes - len(rows)
    if lost > 0:
        _trim_data(root, rows[-1]["episode_index"])
        _recompute_stats(root, info, rows)

    _write_episodes(root, rows)

    info["total_episodes"] = len(rows)
    info["total_frames"] = sum(row["length"] for row in rows)
    info["splits"] = {"train": f"0:{len(rows)}"}
    (root / "meta" / "info.json").write_text(json.dumps(info, indent=4))

    msg = f"에피소드 {len(rows)}개 복구"
    if lost > 0:
        msg += f" · 끊긴 녹화로 {lost}개는 잃었습니다"
    if truncated:
        msg += f" · 읽을 수 없는 파일 {len(truncated)}개는 .unreadable 로 옮겼습니다"
    return msg


def main(argv=None):
    import sys
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv:
        print(__doc__)
        return 2
    root = Path(argv[0])
    if "--check" in argv:
        bad = needs_repair(root)
        print("복구 필요" if bad else "정상")
        return 3 if bad else 0
    try:
        msg = repair_dataset(root)
    except DatasetRepairError as e:
        print(f"복구 실패: {e}", flush=True)
        return 1
    print(msg or "정상 — 고칠 것이 없습니다", flush=True)
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(main())
