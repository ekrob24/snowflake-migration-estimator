import csv
from pathlib import Path

import pytest

from sfestimate import samples


def write_csv(path: Path, header: list[str], rows: list[list]) -> Path:
    with open(path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(header)
        w.writerows(rows)
    return path


@pytest.fixture(scope="session")
def sample_dir(tmp_path_factory) -> Path:
    d = tmp_path_factory.mktemp("samples")
    samples.generate(d)
    return d
