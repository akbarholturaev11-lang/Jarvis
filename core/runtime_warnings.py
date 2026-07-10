from __future__ import annotations

import warnings


_SOUNDDEVICE_NUMPY_SHAPE_WARNING = (
    r"^Setting the shape on a NumPy array has been deprecated in NumPy 2\.5\."
)


def install_runtime_warning_filters() -> None:
    """Hide only the known sounddevice/NumPy 2.5 shape deprecation."""
    warnings.filterwarnings(
        "ignore",
        message=_SOUNDDEVICE_NUMPY_SHAPE_WARNING,
        category=DeprecationWarning,
        module=r"^sounddevice$",
    )
