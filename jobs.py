"""A small background-job seam, mirroring routing.py's role: generic
infrastructure viewer_server.py's endpoints depend on, with no dependency of
its own on anything app-specific (no QUERIES_DIR, no coding_store).

The problem this solves: two features -- Search & Export's downloads, Model's
classifier training -- each need "kick off a background thread, track its
progress under a job_id, let the frontend poll a status endpoint until it's
done." Before this module, each one hand-rolled its own {job_id: {...}} dict
plus threading.Lock plus GET .../status?job_id= handler, and the two copies
had already drifted (see DEVELOPMENT.md's "Background jobs" convention,
which used to just describe the shape to repeat by hand for the next job
type). JobRegistry is that shape, once.
"""
import threading
import uuid


class JobRegistry:
    """Thread-safe job_id -> status-dict registry. start() spawns the worker
    thread and seeds the initial status dict (status="running" is added
    automatically); get() backs a GET .../status?job_id= poll endpoint;
    mutate() is how the worker thread updates progress under the lock, for
    an update that reads-then-writes a field (an increment, a list append)
    rather than just replacing it -- update() is sugar for the common
    "replace these fields outright" case.

    Any exception the worker function raises is caught and turned into
    status="error" with the exception's message, so a worker function
    doesn't need its own try/except for that -- see start()."""

    def __init__(self):
        self._jobs = {}
        self._lock = threading.Lock()

    def start(self, initial, target, *args, **kwargs):
        """Registers a new job (status="running", plus whatever's in
        `initial`) and runs target(job_id, self, *args, **kwargs) on a
        daemon thread. Returns the new job_id."""
        job_id = uuid.uuid4().hex
        with self._lock:
            self._jobs[job_id] = {"status": "running", **initial}

        def run():
            try:
                target(job_id, self, *args, **kwargs)
            except Exception as exc:
                self.update(job_id, status="error", error=str(exc))

        threading.Thread(target=run, daemon=True).start()
        return job_id

    def mutate(self, job_id, fn):
        """Runs fn(job_dict) under the lock, in place."""
        with self._lock:
            fn(self._jobs[job_id])

    def update(self, job_id, **fields):
        """Sugar for mutate() when every field is just being replaced outright."""
        self.mutate(job_id, lambda job: job.update(fields))

    def get(self, job_id):
        """Returns a shallow copy of the job's status dict, or None if
        job_id is unknown -- a copy so a caller can safely serialize it
        (e.g. to JSON) without racing a concurrent mutate()."""
        with self._lock:
            job = self._jobs.get(job_id)
            return dict(job) if job is not None else None

    def has_running(self):
        """True if any job in this registry currently has status="running" --
        e.g. switch_project() refuses to switch while a download/training job
        is still using the project it would switch away from."""
        with self._lock:
            return any(job.get("status") == "running" for job in self._jobs.values())
