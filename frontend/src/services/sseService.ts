import type { JobStatusEvent } from '../types';

const SSE_BASE_URL = '/api/v1';

type EventHandler = (data: JobStatusEvent) => void;

function parseEvent(event: MessageEvent): JobStatusEvent | null {
  try {
    return JSON.parse(event.data as string) as JobStatusEvent;
  } catch {
    return null;
  }
}

export function subscribeToJobStatus(
  jobId: string,
  onMessage: (data: JobStatusEvent) => void,
  onError?: () => void
): () => void {
  const url = `${SSE_BASE_URL}/jobs/${jobId}/status`;
  const eventSource = new EventSource(url);

  // Default unnamed messages
  eventSource.onmessage = (event: MessageEvent) => {
    const data = parseEvent(event);
    if (data) onMessage(data);
  };

  // Named event: "status" — progress updates
  const handleStatusEvent: EventHandler = (data) => onMessage(data);
  const statusListener = (event: MessageEvent) => {
    const data = parseEvent(event);
    if (data) handleStatusEvent(data);
  };

  // Named event: "completed" — job finished
  const completedListener = (event: MessageEvent) => {
    const data = parseEvent(event);
    if (data) onMessage({ ...data, status: 'completed' });
  };

  // Named event: "error" — job failed
  const errorListener = (event: MessageEvent) => {
    const data = parseEvent(event);
    if (data) onMessage({ ...data, status: 'error' });
  };

  eventSource.addEventListener('status', statusListener);
  eventSource.addEventListener('completed', completedListener);
  eventSource.addEventListener('error', errorListener);

  eventSource.onerror = () => {
    if (onError) {
      onError();
    }
    eventSource.close();
  };

  return () => {
    eventSource.removeEventListener('status', statusListener);
    eventSource.removeEventListener('completed', completedListener);
    eventSource.removeEventListener('error', errorListener);
    eventSource.close();
  };
}
