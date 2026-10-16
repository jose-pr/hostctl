"""WinRM: native WinRS, pywinrm and PowerShell Remoting legs."""

from ._psrp import (
    PipelineResult as PipelineResult,
    PipelineStreams as PipelineStreams,
    RunspaceSession as RunspaceSession,
)
from ._transport import (
    WinRMConfig as WinRMConfig,
    winrm_providers as winrm_providers,
)

__all__ = [
    "PipelineResult",
    "PipelineStreams",
    "RunspaceSession",
    "WinRMConfig",
    "winrm_providers",
]
