"""Login/logout, the current user, and user management (admin)."""

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, Field
from sqlmodel import Session, select

from ..database import get_session
from ..models import User, UserRole
from ..services import auth, crypto, git_ops

router = APIRouter(prefix="/auth", tags=["auth"])

_ROLES = {r.value for r in UserRole}


class LoginBody(BaseModel):
    username: str
    password: str


class CreateUserBody(BaseModel):
    username: str = Field(min_length=2, max_length=40)
    password: str = Field(min_length=4)
    role: str = UserRole.member.value


class UpdateUserBody(BaseModel):
    role: str | None = None
    password: str | None = Field(default=None, min_length=4)


class ChangePasswordBody(BaseModel):
    current_password: str
    new_password: str = Field(min_length=4)


class GithubConnectBody(BaseModel):
    token: str = Field(min_length=8, max_length=255)


def _public(u: User) -> dict:
    return {"id": u.id, "username": u.username, "role": u.role,
            "created_at": u.created_at.isoformat() if u.created_at else None,
            # GitHub connection status — the token itself is never returned.
            "github_login": u.github_login, "github_name": u.github_name,
            "github_connected": bool(u.github_token)}


@router.post("/login")
def login(body: LoginBody, response: Response, db: Session = Depends(get_session)) -> dict:
    user = db.exec(select(User).where(User.username == body.username.strip())).first()
    if user is None or not auth.verify_password(body.password, user.password_hash):
        raise HTTPException(401, "Invalid username or password")
    token = auth.create_session(db, user)
    response.set_cookie(
        auth.COOKIE_NAME, token, httponly=True, samesite="lax",
        max_age=int(auth.SESSION_TTL.total_seconds()),
    )
    return {"user": _public(user)}


@router.post("/logout")
def logout(request: Request, response: Response, db: Session = Depends(get_session)) -> dict:
    token = request.cookies.get(auth.COOKIE_NAME)
    if token:
        auth.delete_session(db, token)
    response.delete_cookie(auth.COOKIE_NAME)
    return {"ok": True}


@router.get("/me")
def me(user: User = Depends(auth.require_user), db: Session = Depends(get_session)) -> dict:
    return {"user": _public(user), "default_password": auth.using_default_password(db, user)}


@router.post("/change-password")
def change_password(body: ChangePasswordBody, user: User = Depends(auth.require_user),
                    db: Session = Depends(get_session)) -> dict:
    if not auth.verify_password(body.current_password, user.password_hash):
        raise HTTPException(403, "Current password is incorrect")
    user.password_hash = auth.hash_password(body.new_password)
    db.add(user)
    db.commit()
    auth.clear_bootstrap_pending(db, user)
    return {"ok": True}


@router.post("/github")
def connect_github(body: GithubConnectBody, user: User = Depends(auth.require_user),
                   db: Session = Depends(get_session)) -> dict:
    """Connect the current user's own GitHub account via a personal access token.
    The token is validated against the GitHub API, then stored encrypted; PRs the
    user opens from the board are authored by THIS account."""
    try:
        ident = git_ops.github_identity(body.token)
    except ValueError as e:
        raise HTTPException(422, str(e))
    user.github_token = crypto.encrypt(body.token.strip())
    user.github_login = ident["login"]
    user.github_name = ident["name"]
    db.add(user)
    db.commit()
    db.refresh(user)
    return {"user": _public(user)}


@router.delete("/github")
def disconnect_github(user: User = Depends(auth.require_user),
                      db: Session = Depends(get_session)) -> dict:
    """Forget the current user's GitHub token (they can also revoke it on GitHub)."""
    user.github_token = None
    user.github_login = None
    user.github_name = None
    db.add(user)
    db.commit()
    return {"ok": True}


# --- User management (admin) -------------------------------------------------
@router.get("/users", dependencies=[Depends(auth.require_admin)])
def list_users(db: Session = Depends(get_session)) -> list[dict]:
    return [_public(u) for u in db.exec(select(User).order_by(User.created_at)).all()]


@router.post("/users", status_code=201, dependencies=[Depends(auth.require_admin)])
def create_user(body: CreateUserBody, db: Session = Depends(get_session)) -> dict:
    if body.role not in _ROLES:
        raise HTTPException(422, f"Unknown role '{body.role}'")
    username = body.username.strip()
    if db.exec(select(User).where(User.username == username)).first():
        raise HTTPException(409, "Username already exists")
    user = User(username=username, password_hash=auth.hash_password(body.password), role=body.role)
    db.add(user)
    db.commit()
    db.refresh(user)
    return _public(user)


@router.patch("/users/{user_id}")
def update_user(user_id: int, body: UpdateUserBody,
                admin: User = Depends(auth.require_admin),
                db: Session = Depends(get_session)) -> dict:
    user = db.get(User, user_id)
    if user is None:
        raise HTTPException(404, "User not found")
    if body.role is not None:
        if body.role not in _ROLES:
            raise HTTPException(422, f"Unknown role '{body.role}'")
        if user.id == admin.id and body.role != UserRole.admin.value:
            raise HTTPException(409, "You can't demote your own account")
        user.role = body.role
    if body.password:
        user.password_hash = auth.hash_password(body.password)
    db.add(user)
    db.commit()
    if body.password:
        auth.clear_bootstrap_pending(db, user)
    return _public(user)


@router.delete("/users/{user_id}", status_code=204)
def delete_user(user_id: int, admin: User = Depends(auth.require_admin),
                db: Session = Depends(get_session)) -> None:
    user = db.get(User, user_id)
    if user is None:
        raise HTTPException(404, "User not found")
    if user.id == admin.id:
        raise HTTPException(409, "You can't delete your own account")
    db.delete(user)
    db.commit()
