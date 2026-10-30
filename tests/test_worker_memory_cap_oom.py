"""OOM fixture for the worker memcg cap (P3).

The portal TS build needed ~3.85 GB anon-rss and was OOM-killed at the old 4 GiB worker
cap (t_da1b5dbf, t_3c1f894f, t_3e6e7e19, t_6a578e77), so the cap is 8 GiB. This file
proves the direction that matters: a 4.5 GiB worker allocation survives the cap
``process_registry`` declares, and the same allocation under a 4 GiB cap is killed.
"""

import subprocess
import sys
from pathlib import Path

import pytest

ALLOCATION_BYTES = 4608 * 1024 * 1024  # 4.5 GiB: above the old 4 GiB cap, below the 8 GiB one
_CHUNK_BYTES = 64 * 1024 * 1024
_OLD_CAP_BYTES = 4 * 1024 * 1024 * 1024
_MEM_NEEDED_BYTES = 6 * 1024 * 1024 * 1024  # 4.5 GiB allocation + parent/margin


def _available_bytes() -> int:
    for line in Path("/proc/meminfo").read_text().splitlines():
        if line.startswith("MemAvailable:"):
            return int(line.split()[1]) * 1024
    return 0


def _declared_cap() -> int:
    from tools import process_registry

    # A live install tree on sys.path also carries an 8 GiB cap; only the checkout counts.
    assert Path(process_registry.__file__).resolve() == (
        Path(__file__).resolve().parents[1] / "tools" / "process_registry.py"
    ), process_registry.__file__
    return process_registry._WORKER_MEMORY_MAX_CAP_BYTES


def _run_allocator(cap_bytes: int) -> subprocess.CompletedProcess:
    """Allocate ALLOCATION_BYTES in a child whose address space is capped at ``cap_bytes``."""
    return subprocess.run(
        [sys.executable, str(Path(__file__).resolve()), str(cap_bytes)],
        capture_output=True,
        text=True,
        timeout=180,
    )


@pytest.mark.skipif(
    _available_bytes() < _MEM_NEEDED_BYTES,
    reason="needs >=6 GiB MemAvailable to allocate 4.5 GiB safely on this host",
)
def test_cap_is_eight_gib():
    assert _declared_cap() == 8589934592


@pytest.mark.platforms("posix")
@pytest.mark.skipif(
    _available_bytes() < _MEM_NEEDED_BYTES,
    reason="needs >=6 GiB MemAvailable to allocate 4.5 GiB safely on this host",
)
def test_four_point_five_gib_worker_survives_the_cap():
    cap = _declared_cap()
    done = _run_allocator(cap)
    assert done.returncode == 0, (
        f"4.5 GiB worker allocation was killed under the declared {cap}-byte cap: "
        f"{done.stderr[-500:]}"
    )
    assert "allocated 4608 MiB" in done.stdout


@pytest.mark.platforms("posix")
@pytest.mark.skipif(
    _available_bytes() < _MEM_NEEDED_BYTES,
    reason="needs >=6 GiB MemAvailable to allocate 4.5 GiB safely on this host",
)
def test_same_allocation_is_killed_under_four_gib():
    done = _run_allocator(_OLD_CAP_BYTES)
    assert done.returncode != 0, (
        "the 4.5 GiB allocation survived a 4 GiB cap, so this fixture cannot fail"
    )


def _allocate_under(cap_bytes: int) -> None:
    import resource

    # shortcut: RLIMIT_AS stands in for the cgroup memory.max the worker cap feeds;
    # upgrade to a real systemd scope if RSS-vs-AS divergence ever matters here.
    resource.setrlimit(resource.RLIMIT_AS, (cap_bytes, cap_bytes))
    chunks = [bytearray(_CHUNK_BYTES) for _ in range(ALLOCATION_BYTES // _CHUNK_BYTES)]
    print(f"allocated {len(chunks) * _CHUNK_BYTES // 1024**2} MiB")


if __name__ == "__main__":
    _allocate_under(int(sys.argv[1]))
