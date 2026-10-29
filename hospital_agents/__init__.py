"""Multi-agent operations layer for the AI Hospital Digital Twin."""
__version__ = "0.1.0"

from .contracts import SharedInputs
from .config import Settings
from .orchestrator import Orchestrator, default_agents

__all__ = ["SharedInputs", "Settings", "Orchestrator", "default_agents", "__version__"]
