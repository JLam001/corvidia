"""Scout person detection and confirmation event pipeline (event core)."""

from .config import PipelineConfig, load_config
from .pipeline import EventPipeline, LatestFrameSlot

__all__ = ["EventPipeline", "LatestFrameSlot", "PipelineConfig", "load_config"]
