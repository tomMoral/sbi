import pytest
import torch

from sbi.utils.sbiutils import seed_all_backends

# Seed for `set_seed` fixture. Change to random state of all seeded tests.
seed = 1


# Use seed automatically for every test function.
@pytest.fixture(autouse=True)
def set_seed():
    seed_all_backends(seed)


@pytest.fixture(scope="session", autouse=True)
def set_default_tensor_type():
    torch.set_default_tensor_type("torch.FloatTensor")


# Pytest hook to skip GPU tests if no devices are available.
def pytest_collection_modifyitems(config, items):
    """Skip GPU tests if no devices are available."""
    gpu_device_available = (
        torch.cuda.is_available() or torch.backends.mps.is_available()
    )
    if not gpu_device_available:
        skip_gpu = pytest.mark.skip(reason="No devices available")
        for item in items:
            if "gpu" in item.keywords:
                item.add_marker(skip_gpu)
