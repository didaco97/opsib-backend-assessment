"""Workflow tests via the HTTP API: valid/invalid transitions, retry safety,
reassignment, audit history, exception resolution gating."""


def _setup(loaded_client):
    r = loaded_client.post("/detection/run")
    assert r.status_code == 200, r.text
    created = r.json()["tasks_created"]
    task_id = created[0]["task_id"]
    return task_id


def test_full_lifecycle_resolves_exception(loaded_client):
    task_id = _setup(loaded_client)
    c = loaded_client
    assert c.post(f"/tasks/{task_id}/acknowledge", params={"actor": "HR-102"}).json()["status"] == "ACKNOWLEDGED"
    assert c.post(f"/tasks/{task_id}/start", params={"actor": "HR-102"}).json()["status"] == "IN_PROGRESS"
    r = c.post(f"/tasks/{task_id}/complete", params={"actor": "HR-102"},
               json={"resolution_code": "RECOUNTED",
                     "resolution_evidence_ref": "GRN-501-A,GRN-501-B"})
    assert r.json()["status"] == "COMPLETED"
    assert c.post(f"/tasks/{task_id}/request-verification").json()["status"] == "PENDING_VERIFICATION"
    r = c.post(f"/tasks/{task_id}/verify", params={"actor": "HR-201"})
    assert r.json()["status"] == "VERIFIED"
    exc_id = r.json()["exception_id"]
    assert c.get(f"/exceptions/{exc_id}").json()["status"] == "RESOLVED"


def test_invalid_transition_rejected(loaded_client):
    task_id = _setup(loaded_client)
    # ASSIGNED -> COMPLETED is not allowed
    r = loaded_client.post(f"/tasks/{task_id}/complete",
                           json={"resolution_code": "X", "resolution_evidence_ref": "Y"})
    assert r.status_code == 409
    assert "invalid transition" in r.json()["detail"]


def test_complete_requires_resolution(loaded_client):
    task_id = _setup(loaded_client)
    loaded_client.post(f"/tasks/{task_id}/acknowledge")
    loaded_client.post(f"/tasks/{task_id}/start")
    r = loaded_client.post(f"/tasks/{task_id}/complete", json={})
    assert r.status_code == 422


def test_repeated_completion_is_safe_replay(loaded_client):
    """Client timeout after commit: the retry must not error or double-record."""
    task_id = _setup(loaded_client)
    c = loaded_client
    c.post(f"/tasks/{task_id}/acknowledge")
    c.post(f"/tasks/{task_id}/start")
    body = {"resolution_code": "RECOUNTED", "resolution_evidence_ref": "DOC-1"}
    first = c.post(f"/tasks/{task_id}/complete", json=body).json()
    second = c.post(f"/tasks/{task_id}/complete", json=body).json()
    assert first["idempotent_replay"] is False
    assert second["idempotent_replay"] is True
    assert second["status"] == "COMPLETED"
    completed_events = [e for e in second["history"] if e["event_type"] == "COMPLETED"]
    assert len(completed_events) == 1


def test_reassign_records_reason_and_validates(loaded_client):
    task_id = _setup(loaded_client)
    c = loaded_client
    # ineligible: inactive employee
    r = c.post(f"/tasks/{task_id}/reassign",
               json={"new_assignee_id": "HR-103", "reason": "coverage", "actor": "HR-201"})
    assert r.status_code == 422
    # reason required
    r = c.post(f"/tasks/{task_id}/reassign",
               json={"new_assignee_id": "HR-201", "reason": "", "actor": "HR-201"})
    assert r.status_code == 422
    # valid reassignment to the on-shift store manager at STORE-014
    r = c.post(f"/tasks/{task_id}/reassign",
               json={"new_assignee_id": "HR-201", "reason": "associate at capacity",
                     "actor": "HR-201"})
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["assignee_id"] == "HR-201"
    re_events = [e for e in data["history"] if e["event_type"] == "REASSIGNED"]
    assert len(re_events) == 1 and "associate at capacity" in re_events[0]["reason"]
    assert re_events[0]["actor"] == "HR-201"


def test_verify_without_completion_blocked(loaded_client):
    task_id = _setup(loaded_client)
    r = loaded_client.post(f"/tasks/{task_id}/verify")
    assert r.status_code == 409  # ASSIGNED -> VERIFIED is not a valid transition


def test_audit_history_order(loaded_client):
    task_id = _setup(loaded_client)
    c = loaded_client
    c.post(f"/tasks/{task_id}/acknowledge", params={"actor": "HR-102"})
    c.post(f"/tasks/{task_id}/start", params={"actor": "HR-102"})
    hist = c.get(f"/tasks/{task_id}").json()["history"]
    types = [e["event_type"] for e in hist]
    assert types == ["CREATED", "ASSIGNED", "ACKNOWLEDGED", "IN_PROGRESS"]


def test_reopen_path(loaded_client):
    task_id = _setup(loaded_client)
    c = loaded_client
    c.post(f"/tasks/{task_id}/acknowledge")
    c.post(f"/tasks/{task_id}/start")
    c.post(f"/tasks/{task_id}/complete",
           json={"resolution_code": "RECOUNTED", "resolution_evidence_ref": "DOC-1"})
    c.post(f"/tasks/{task_id}/request-verification")
    r = c.post(f"/tasks/{task_id}/reopen", params={"actor": "HR-201"})
    assert r.json()["status"] == "REOPENED"
    r = c.post(f"/tasks/{task_id}/start")
    assert r.json()["status"] == "IN_PROGRESS"


def test_reject_path(loaded_client):
    task_id = _setup(loaded_client)
    c = loaded_client
    c.post(f"/tasks/{task_id}/acknowledge")
    c.post(f"/tasks/{task_id}/start")
    c.post(f"/tasks/{task_id}/complete",
           json={"resolution_code": "RECOUNTED", "resolution_evidence_ref": "DOC-1"})
    c.post(f"/tasks/{task_id}/request-verification")
    # reject requires a reason
    r = c.post(f"/tasks/{task_id}/reject", json={})
    assert r.status_code == 422
    r = c.post(f"/tasks/{task_id}/reject", params={"actor": "HR-201"},
               json={"reason": "recount evidence does not match GRN quantities"})
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["status"] == "REJECTED"
    rej = [e for e in data["history"] if e["event_type"] == "REJECTED"]
    assert rej and "recount evidence" in rej[0]["reason"]
    # rejected work can be reopened
    assert c.post(f"/tasks/{task_id}/reopen").json()["status"] == "REOPENED"
