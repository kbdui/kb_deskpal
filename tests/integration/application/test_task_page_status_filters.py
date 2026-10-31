"""Real desktop task filtering uses SQL pagination over synthetic local tasks."""

import pytest

from deskpal.application.desktop import DesktopFacade
from deskpal.application.factory import create_application
from deskpal.config import load_config
from deskpal.tasks.models import TaskStatus


def synthetic_config(tmp_path):
    return load_config(environ={"DESKPAL_LLM_PROVIDER": "offline",
        "DESKPAL_WORKSPACE_DIR": str(tmp_path / "workspace"),
        "DESKPAL_DATA_DIR": str(tmp_path / "data"),
        "DESKPAL_MEMORY_EMBEDDING_PROVIDER": "disabled"})


def seed_tasks(app):
    states = [TaskStatus.CANCELLED, TaskStatus.WAITING, TaskStatus.COMPLETED,
              TaskStatus.NEEDS_REVIEW, TaskStatus.CREATED, TaskStatus.PLANNING,
              TaskStatus.CANCELLED, TaskStatus.WAITING, TaskStatus.RUNNING,
              TaskStatus.COMPLETED, TaskStatus.NEEDS_REVIEW, TaskStatus.FAILED]
    for index, state in enumerate(states):
        task = app.tasks.create(f"synthetic task {index}")
        if state in {TaskStatus.WAITING, TaskStatus.RUNNING, TaskStatus.COMPLETED}:
            app.tasks.update_status(task.id, TaskStatus.PLANNING)
        if state == TaskStatus.COMPLETED: app.tasks.update_status(task.id, TaskStatus.RUNNING)
        if state != TaskStatus.CREATED: app.tasks.update_status(task.id, state)
    return app.tasks.page_summaries(limit=100)["items"]


def test_facade_task_filters_page_over_matching_rows_not_unfiltered_pages(tmp_path):
    with create_application(synthetic_config(tmp_path)) as app:
        seeded = seed_tasks(app)
        facade = DesktopFacade(app)
        desired = ["Waiting", "NeedsReview"]
        expected = [item["id"] for item in seeded if item["status"] in desired]
        first = facade.execute("tasks", {"statuses": desired, "limit": 2})
        second = facade.execute("tasks", {"statuses": desired, "limit": 2, "offset": 2})
        empty = facade.execute("tasks", {"statuses": desired, "limit": 2, "offset": 4})
        assert [item["id"] for item in first["items"] + second["items"]] == expected
        assert first["has_more"] and not second["has_more"] and not empty["items"]
        assert {item["status"] for item in first["items"]} == set(desired)
        assert all("request" not in item and "result" not in item for item in first["items"])
        assert [item["id"] for item in facade.execute("tasks", {"limit": 100})["items"]] == [item["id"] for item in seeded]
        single = facade.execute("tasks", {"status": "Waiting"})
        assert len(single["items"]) == 2 and all(item["status"] == "Waiting" for item in single["items"])
        active = app.task_page(statuses=(TaskStatus.CREATED, TaskStatus.PLANNING, TaskStatus.RUNNING))
        assert {item["status"] for item in active["items"]} == {"Created", "Planning", "Running"}


@pytest.mark.parametrize("parameters", [
    {"status": 1}, {"status": "bad"}, {"status": None}, {"statuses": "Waiting"},
    {"statuses": []}, {"statuses": ["Waiting"] * 10}, {"statuses": ["Waiting", "Waiting"]},
    {"statuses": ["Waiting", "not-a-state"]}, {"statuses": [True]},
    {"status": "Waiting", "statuses": ["Waiting"]}, {"unknown": "Waiting"},
])
def test_facade_rejects_invalid_task_filters(tmp_path, parameters):
    with create_application(synthetic_config(tmp_path)) as app:
        with pytest.raises(ValueError): DesktopFacade(app).execute("tasks", parameters)


def test_storage_rejects_untyped_statuses_before_query(tmp_path):
    with create_application(synthetic_config(tmp_path)) as app:
        for statuses in [("Waiting",), (), (TaskStatus.WAITING, TaskStatus.WAITING)]:
            with pytest.raises(ValueError): app.tasks.page_summaries(statuses=statuses)
