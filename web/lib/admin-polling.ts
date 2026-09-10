/** One request at a time; stop releases both the timer and in-flight request. */
export function startAdminPolling<T>({
  load, onValue, onError, shouldContinue,
  retryError = () => true,
  intervalMs = 2_500,
  isVisible = () => true,
  schedule = (callback: () => void, ms: number) => setTimeout(callback, ms),
  cancel = (timer: ReturnType<typeof setTimeout>) => clearTimeout(timer),
}: {
  load: (signal: AbortSignal) => Promise<T>;
  onValue: (value: T) => void;
  onError: (error: unknown) => void;
  shouldContinue: (value: T) => boolean;
  retryError?: (error: unknown) => boolean;
  intervalMs?: number;
  isVisible?: () => boolean;
  schedule?: (callback: () => void, ms: number) => ReturnType<typeof setTimeout>;
  cancel?: (timer: ReturnType<typeof setTimeout>) => void;
}): () => void {
  let stopped = false;
  let timer: ReturnType<typeof setTimeout> | undefined;
  let request: AbortController | undefined;
  let keepGoing = true;
  const next = () => {
    if (!stopped && keepGoing) timer = schedule(() => void run(), intervalMs);
  };
  const run = async () => {
    if (stopped) return;
    if (!isVisible()) { next(); return; }
    request = new AbortController();
    try {
      const value = await load(request.signal);
      if (stopped) return;
      keepGoing = shouldContinue(value);
      onValue(value);
    } catch (error) {
      if (!stopped) {
        keepGoing = retryError(error);
        onError(error);
      }
    } finally {
      request = undefined;
      next();
    }
  };
  void run();
  return () => {
    stopped = true;
    if (timer !== undefined) cancel(timer);
    request?.abort();
  };
}
