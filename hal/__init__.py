"""Hardware abstraction layer.

The only layer that touches hardware, so everything above it stays testable at
a desk. `mount` drives the pan-tilt head; `camera` drives the Canon EOS 2000D
over PTP (gphoto2), with `camera_sim.FakeCamera` as its desk stand-in.
"""

from .camera import (
    BracketError,
    BracketManifest,
    Camera,
    CameraBusy,
    CameraDisconnected,
    CameraError,
    CameraNotFound,
    CameraSetupError,
    FrameRecord,
    Gphoto2Camera,
    create_camera,
)
from .camera_config import CameraConfig, load_camera_config
from .config import AxisConfig, MountConfig, load_config
from .mount import (
    MountError,
    MoveAborted,
    MoveResult,
    NotZeroed,
    PanTiltMount,
    SoftLimit,
    install_signal_handlers,
)

__all__ = [
    "AxisConfig",
    "MountConfig",
    "load_config",
    "PanTiltMount",
    "MountError",
    "NotZeroed",
    "SoftLimit",
    "MoveAborted",
    "MoveResult",
    "install_signal_handlers",
    "Camera",
    "CameraConfig",
    "load_camera_config",
    "create_camera",
    "Gphoto2Camera",
    "BracketManifest",
    "FrameRecord",
    "CameraError",
    "CameraNotFound",
    "CameraBusy",
    "CameraDisconnected",
    "CameraSetupError",
    "BracketError",
]
