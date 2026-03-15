"""Abstract base class for audio processing pipelines.

Both GpuPipeline and ApiPipeline implement this interface so that
main.py can hold a reference typed as BasePipeline regardless of mode.
"""

from __future__ import annotations

from karaoke_shared.models.job import Job


class BasePipeline:
    """Interface contract for audio processing pipelines.

    Concrete implementations:
      - worker.gpu.gpu_pipeline.GpuPipeline  (local GPU, WORKER_MODE=gpu)
      - worker.api.api_pipeline.ApiPipeline  (cloud APIs, WORKER_MODE=api)
    """

    async def process(self, job: Job) -> None:
        """Process a single pending job end-to-end.

        Args:
            job: The locked job to process.
        """
        raise NotImplementedError

    def cleanup(self) -> None:
        """Release any held resources (models, connections, etc.)."""
