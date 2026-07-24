"""End-to-end API behaviour through the FastAPI app: health, the auth flow, and
role-based access control. The LLM/agent boundary is never exercised here — only
routing, auth, and ACL.
"""

from app.models import User, UserRole
from app.services import auth
from sqlmodel import select


def test_health_is_public(client):
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"


def test_protected_route_requires_auth(client):
    # No cookie → 401.
    r = client.get("/api/settings")
    assert r.status_code == 401


def test_login_rejects_bad_password(client):
    r = client.post("/auth/login", json={"username": "admin", "password": "wrong"})
    assert r.status_code == 401


def test_login_then_access(admin_client):
    r = admin_client.get("/auth/me")
    assert r.status_code == 200
    assert r.json()["user"]["username"] == "admin"


def test_logout_clears_session(admin_client):
    admin_client.post("/auth/logout")
    r = admin_client.get("/api/settings")
    assert r.status_code == 401


def test_settings_readable_by_signed_in_user(admin_client):
    r = admin_client.get("/api/settings")
    assert r.status_code == 200
    assert "groups" in r.json()


def _make_user(db, username, role, password="pw12345"):
    u = User(username=username, password_hash=auth.hash_password(password), role=role)
    db.add(u)
    db.commit()
    return u


def test_viewer_cannot_write_settings(client, db):
    _make_user(db, "viewer1", UserRole.viewer.value)
    client.post("/auth/login", json={"username": "viewer1", "password": "pw12345"})
    r = client.put("/api/settings", json={"values": {"max_revision_rounds": 1}})
    assert r.status_code == 403


def test_member_cannot_write_settings(client, db):
    # Settings are admin-only; a member operates the pipeline but not config.
    _make_user(db, "member1", UserRole.member.value)
    client.post("/auth/login", json={"username": "member1", "password": "pw12345"})
    r = client.put("/api/settings", json={"values": {"max_revision_rounds": 1}})
    assert r.status_code == 403


def test_admin_can_write_settings(admin_client):
    r = admin_client.put("/api/settings", json={"values": {"max_revision_rounds": 2}})
    assert r.status_code == 200
    assert "max_revision_rounds" in r.json()["changed"]


def test_admin_settings_write_validates(admin_client):
    r = admin_client.put("/api/settings", json={"values": {"max_revision_rounds": 999}})
    assert r.status_code == 422


def test_viewer_cannot_manage_users(client, db):
    _make_user(db, "viewer2", UserRole.viewer.value)
    client.post("/auth/login", json={"username": "viewer2", "password": "pw12345"})
    r = client.get("/auth/users")
    assert r.status_code == 403


def test_change_password_clears_default_flag(client, db, monkeypatch):
    # Bootstrap admin on a generated password is flagged; changing it clears that.
    monkeypatch.delenv("ADMIN_PASSWORD", raising=False)
    monkeypatch.setattr(auth.settings, "admin_password", "", raising=False)
    # Remove the fixture's preset admin and re-bootstrap with a generated pw.
    for u in db.exec(select(User)).all():
        db.delete(u)
    db.commit()
    auth.ensure_bootstrap_admin(db)
    # We can't read the generated password, so set a known one for login while
    # leaving the "still-default" pending flag in place.
    admin = db.exec(select(User)).first()
    admin.password_hash = auth.hash_password("known-pw")
    db.add(admin)
    db.commit()

    client.post("/auth/login", json={"username": "admin", "password": "known-pw"})
    assert client.get("/auth/me").json()["default_password"] is True

    r = client.post(
        "/auth/change-password",
        json={"current_password": "known-pw", "new_password": "brand-new-pw"},
    )
    assert r.status_code == 200
    assert client.get("/auth/me").json()["default_password"] is False
