import os
import subprocess
from pathlib import Path

import pytest


@pytest.mark.parametrize("signature", ["ext4", "xfs", "gpt"])
def test_storage_requires_a_blank_disk(tmp_path, signature):
    storage_directory = tmp_path / "storage"
    storage_directory.mkdir()

    script = Path("cloud-init/storage.sh").read_text()
    script = script.replace('/srv/llm', str(storage_directory))
    # Simulate an attached block device without requiring root or real disks.
    script = script.replace('test -b "$DATA_DEVICE"', 'true')
    binary_directory = tmp_path / "bin"
    binary_directory.mkdir()
    commands = {
        "mountpoint": "exit 1",
        "lsblk": "echo disk",
        "wipefs": 'printf "%s" "$TEST_SIGNATURE"',
        "mkfs.ext4": 'touch "$TEST_MUTATION"',
        "mount": 'touch "$TEST_MUTATION"',
        "systemctl": 'touch "$TEST_MUTATION"',
    }
    for name, body in commands.items():
        executable = binary_directory / name
        executable.write_text(f"#!/bin/bash\n{body}\n")
        executable.chmod(0o755)

    environment = os.environ.copy()
    environment.update({
        "PATH": f"{binary_directory}:{environment['PATH']}",
        "TEST_SIGNATURE": signature,
        "TEST_MUTATION": str(tmp_path / "mutation"),
    })
    result = subprocess.run(["bash", "-c", script], env=environment, capture_output=True, text=True)
    assert result.returncode != 0
    assert not (tmp_path / "mutation").exists()
