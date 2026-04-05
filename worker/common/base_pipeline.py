"""Abstract base class for audio processing pipelines."""

from __future__ import annotations

from karaoke_shared.models.job import Job


class BasePipeline:
    """Interface contract for audio processing pipelines.

    Concrete implementation:
      - worker.gpu.gpu_pipeline.GpuPipeline  (local GPU)
    """

    async def process(self, job: Job) -> None:
        """Process a single pending job end-to-end.

        Args:
            job: The locked job to process.
        """
        raise NotImplementedError

    def cleanup(self) -> None:
        """Release any held resources (models, connections, etc.)."""
