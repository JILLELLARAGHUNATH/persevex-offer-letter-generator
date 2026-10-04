import threading
import time
import uuid
from typing import Dict, Any, Optional

class BulkJobManager:
    """
    Thread-safe in-memory manager for tracking live bulk operations
    (generation & email sending) across all four workflows:
    - offer_letter (Internship Offer Letter)
    - ca_letter (Campus Ambassador Offer Letter)
    - certificate (Internship Certificate)
    - ca_certificate (Campus Ambassador Certificate)
    """

    def __init__(self):
        self._lock = threading.Lock()
        self._jobs: Dict[str, Dict[str, Any]] = {}

    def create_job(
        self,
        workflow_type: str,
        operation_type: str,
        total: int,
        title: str = "",
        metadata: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        with self._lock:
            job_id = f"job-{uuid.uuid4().hex[:12]}"
            now = time.time()
            job = {
                "job_id": job_id,
                "workflow_type": workflow_type,
                "operation_type": operation_type,  # 'generation' or 'sending'
                "title": title or f"{operation_type.title()} {workflow_type.replace('_', ' ').title()}",
                "status": "running",  # 'running', 'cancelling', 'cancelled', 'completed', 'failed'
                "total": max(0, int(total)),
                "processed": 0,
                "sent": 0,
                "failed": 0,
                "remaining": max(0, int(total)),
                "progress_percent": 0.0,
                "cancel_requested": False,
                "created_at": now,
                "updated_at": now,
                "metadata": metadata or {},
            }
            self._jobs[job_id] = job
            return dict(job)

    def get_job(self, job_id: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            job = self._jobs.get(job_id)
            return dict(job) if job else None

    def get_active_job(self, workflow_type: Optional[str] = None) -> Optional[Dict[str, Any]]:
        with self._lock:
            # Return newest running or cancelling job
            candidates = [
                j for j in self._jobs.values()
                if j["status"] in ("running", "cancelling")
                and (workflow_type is None or j["workflow_type"] == workflow_type)
            ]
            if not candidates:
                return None
            candidates.sort(key=lambda x: x["updated_at"], reverse=True)
            return dict(candidates[0])

    def request_cancel(self, job_id: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            job = self._jobs.get(job_id)
            if not job:
                return None
            job["cancel_requested"] = True
            if job["status"] == "running":
                job["status"] = "cancelling"
            job["updated_at"] = time.time()
            return dict(job)

    def update_progress(
        self,
        job_id: str,
        sent_inc: int = 0,
        failed_inc: int = 0,
        processed_inc: int = 1,
        current_item_name: str = "",
    ) -> Optional[Dict[str, Any]]:
        with self._lock:
            job = self._jobs.get(job_id)
            if not job:
                return None
            job["sent"] += sent_inc
            job["failed"] += failed_inc
            job["processed"] += processed_inc
            if job["processed"] > job["total"]:
                job["total"] = job["processed"]
            job["remaining"] = max(0, job["total"] - job["processed"])
            if job["total"] > 0:
                job["progress_percent"] = round((job["processed"] / job["total"]) * 100, 1)
            else:
                job["progress_percent"] = 100.0

            if current_item_name:
                job["metadata"]["current_item"] = current_item_name

            job["updated_at"] = time.time()
            return dict(job)

    def complete_job(self, job_id: str, status: Optional[str] = None) -> Optional[Dict[str, Any]]:
        with self._lock:
            job = self._jobs.get(job_id)
            if not job:
                return None
            if status:
                job["status"] = status
            elif job.get("cancel_requested"):
                job["status"] = "cancelled"
            else:
                job["status"] = "completed"
            job["remaining"] = 0
            if job["status"] == "completed":
                job["processed"] = job["total"]
                job["progress_percent"] = 100.0
            job["updated_at"] = time.time()
            return dict(job)

    def is_cancelled(self, job_id: Optional[str]) -> bool:
        if not job_id:
            return False
        with self._lock:
            job = self._jobs.get(job_id)
            if not job:
                return False
            return bool(job.get("cancel_requested") or job.get("status") in ("cancelling", "cancelled"))


# Global singleton instance
bulk_job_manager = BulkJobManager()
