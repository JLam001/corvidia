"""Scout person detection and confirmation event pipeline (event core)."""

from .config import PipelineConfig, load_config
__all__ = ["EventPipeline", "LatestFrameSlot", "PipelineConfig", "load_config"]


def __getattr__(name):
    # The stand supervisor must not import vision/GPU dependencies. Preserve the
    # public event-core imports for existing callers, but load them on demand.
    if name in {"EventPipeline", "LatestFrameSlot"}:
        from .pipeline import EventPipeline, LatestFrameSlot
        return {"EventPipeline": EventPipeline, "LatestFrameSlot": LatestFrameSlot}[name]
    raise AttributeError(name)
