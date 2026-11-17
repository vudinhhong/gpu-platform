"""What a node can be asked to do, on whichever machine it is.

The master runs these for its own GPUs; an agent runs them for a command it
received.  Arguments and results are plain JSON, so the database objects the
container code reads (a job, its owner, the owner's assignment) travel as
dicts and are rebuilt here as simple objects carrying the same attributes.
"""

import time
from types import SimpleNamespace
from typing import Any, Callable, Dict, Optional


def job_args(job) -> Dict[str, Any]:
    return {k: getattr(job, k, None) for k in (
        "id", "gpu_indices", "gpu_memory_mb", "gpu_memory_enforced",
        "script", "workdir", "output_path", "cpu_cores")}


def user_args(user) -> Dict[str, Any]:
    a = getattr(user, "gpu_assignment", None)
    return {
        **{k: getattr(user, k, None) for k in (
            "id", "username", "preferred_image", "home_path", "disk_quota_mb",
            "gpu_hours_quota", "cpu_hours_quota")},
        "gpu_assignment": None if a is None else {k: getattr(a, k, None) for k in (
            "gpu_indices", "memory_limit_mb", "cpu_cores", "max_processes",
            "gpu_memory_limit_mb")},
    }


def _user(d: Dict[str, Any]) -> SimpleNamespace:
    a = d.get("gpu_assignment")
    return SimpleNamespace(**{**d, "gpu_assignment": SimpleNamespace(**a) if a else None})


def _ping() -> Dict[str, Any]:
    return {"pong": time.time()}


def _start_job(job: Dict[str, Any], user: Dict[str, Any]) -> Dict[str, Any]:
    from services import container_manager

    return container_manager.start_job_container(SimpleNamespace(**job), _user(user))


def _job_state(job_id: int) -> Optional[Dict[str, Any]]:
    from services import container_manager

    return container_manager.job_container_state(job_id)


def _stop_job(job_id: int) -> Any:
    from services import container_manager

    return container_manager.stop_job_container(job_id)


def _remove_job(job_id: int) -> Any:
    from services import container_manager

    return container_manager.remove_job_container(job_id)


def _pause_job(job_id: int) -> Any:
    from services import container_manager

    return container_manager.pause_job_container(job_id)


def _unpause_job(job_id: int) -> Any:
    from services import container_manager

    return container_manager.unpause_job_container(job_id)


def _start_workspace(params: Dict[str, Any], user: Dict[str, Any]) -> Dict[str, Any]:
    from services import container_manager

    out = container_manager.start_user_container(**params, user=_user(user))
    out["port"] = container_manager.CONTAINER_PORT
    return out


def _workspace_state(username: str) -> Optional[Dict[str, Any]]:
    from services import container_manager

    return container_manager.get_container_state(username)


def _workspace_ready(username: str, base_url: str = "/", token: Optional[str] = None,
                     timeout: int = 60) -> bool:
    from services import container_manager

    return container_manager.wait_until_running(
        container_manager.container_name(username), timeout=timeout,
        base_url=base_url, token=token)


def _workspace_logs(username: str, tail: int = 40) -> str:
    from services import container_manager

    return container_manager.get_container_logs(container_manager.container_name(username), tail=tail)


def _stop_workspace(username: str) -> bool:
    from services import container_manager

    return container_manager.stop_user_container(username)


def _image_exists(image: str) -> bool:
    from services import container_manager

    return container_manager.image_exists(image)


OPS: Dict[str, Callable[..., Any]] = {
    "ping": _ping,
    "start_job": _start_job,
    "job_state": _job_state,
    "stop_job": _stop_job,
    "remove_job": _remove_job,
    "pause_job": _pause_job,
    "unpause_job": _unpause_job,
    "start_workspace": _start_workspace,
    "workspace_state": _workspace_state,
    "workspace_ready": _workspace_ready,
    "workspace_logs": _workspace_logs,
    "stop_workspace": _stop_workspace,
    "image_exists": _image_exists,
}


def run(op: str, args: Dict[str, Any]) -> Any:
    fn = OPS.get(op)
    if fn is None:
        raise ValueError(f"unknown node operation {op!r}")
    return fn(**args)
