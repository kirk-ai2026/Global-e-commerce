import os


def kick():
    target = os.getenv("LULU_CONTENT_RUN_JOB")
    if not target:
        return {"status": "LOCAL_WORKER_REQUIRED"}
    if target != "projects/zenheart/locations/asia-east1/jobs/lulu-merch-content":
        raise ValueError("WORKER_TARGET_NOT_ALLOWED")
    import google.auth
    from google.auth.transport.requests import AuthorizedSession

    credentials, _ = google.auth.default(
        scopes=["https://www.googleapis.com/auth/cloud-platform"]
    )
    try:
        response = AuthorizedSession(credentials).post(
            "https://run.googleapis.com/v2/" + target + ":run", json={}, timeout=15
        )
        return {
            "status": "DISPATCHED" if response.ok else "PENDING_RETRY",
            "operation": response.json().get("name") if response.ok else None,
        }
    except Exception:
        return {"status": "PENDING_RETRY"}
