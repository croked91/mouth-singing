const SSE_BASE_URL = '/api/v1';

export function subscribeToJobStatus(
  jobId: string,
  onMessage: (data: unknown) => void,
  onError?: () => void
): () => void {
  const url = `${SSE_BASE_URL}/jobs/${jobId}/status`;
  const eventSource = new EventSource(url);

  eventSource.onmessage = (event: MessageEvent) => {
    try {
      const data = JSON.parse(event.data as string) as unknown;
      onMessage(data);
    } catch {
      onMessage(event.data);
    }
  };

  eventSource.onerror = () => {
    if (onError) {
      onError();
    }
    eventSource.close();
  };

  return () => {
    eventSource.close();
  };
}
