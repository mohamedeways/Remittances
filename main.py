"""Remittance tracker: salesmen upload deposit receipts / bank transfers,
finance reviews them. FastAPI + SQLAlchemy (SQLite by default, PostgreSQL via DATABASE_URL)."""
import csv
import hashlib
import io
import os
import secrets
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

import bcrypt
import jwt
from fastapi import Depends, FastAPI, File, Form, HTTPException, Request, Response, UploadFile
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from PIL import Image, ImageOps
from pydantic import BaseModel
import pypdfium2 as pdfium
from sqlalchemy import (Boolean, Column, DateTime, Float, ForeignKey, Integer, String, Text,
                        create_engine, func, inspect, text)
from sqlalchemy.orm import Session, declarative_base, relationship, sessionmaker

# ----------------------------------------------------------------- config
BASE = Path(__file__).parent
DATA_DIR = Path(os.getenv("DATA_DIR", BASE / "data"))
UPLOAD_DIR = DATA_DIR / "uploads"
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)

DATABASE_URL = os.getenv("DATABASE_URL", f"sqlite:///{DATA_DIR / 'app.db'}")
if DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)

COOKIE_SECURE = os.getenv("COOKIE_SECURE", "true").lower() != "false"
EXPORT_KEY = os.getenv("EXPORT_KEY", "")
SESSION_HOURS = 12
MAX_UPLOAD_BYTES = 15 * 1024 * 1024


def _secret() -> str:
    if os.getenv("SECRET_KEY"):
        return os.environ["SECRET_KEY"]
    f = DATA_DIR / ".secret"
    if not f.exists():
        f.write_text(secrets.token_hex(32))
    return f.read_text().strip()


SECRET = _secret()

KINDS = {"deposit", "transfer"}
ROLES = {"salesman", "finance", "admin"}
# status -> statuses finance may move it to
TRANSITIONS = {
    "pending": {"confirmed", "rejected"},
    "confirmed": {"transferred", "rejected", "pending"},
    "rejected": {"pending"},
    "transferred": set(),
}

# --------------------------------------------------------------- database
engine = create_engine(
    DATABASE_URL,
    connect_args={"check_same_thread": False} if DATABASE_URL.startswith("sqlite") else {},
    pool_pre_ping=True,
)
SessionLocal = sessionmaker(bind=engine, autoflush=False)
Base = declarative_base()


def utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def iso(d: Optional[datetime]) -> Optional[str]:
    return d.isoformat() + "Z" if d else None


class User(Base):
    __tablename__ = "users"
    id = Column(Integer, primary_key=True)
    username = Column(String(60), unique=True, nullable=False, index=True)
    full_name = Column(String(120), nullable=False)
    password_hash = Column(String(100), nullable=False)
    role = Column(String(20), nullable=False, default="salesman")
    active = Column(Boolean, nullable=False, default=True)
    created_at = Column(DateTime, default=utcnow)


class Remittance(Base):
    __tablename__ = "remittances"
    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    kind = Column(String(20), nullable=False)
    customer_name = Column(String(160), nullable=False)
    amount = Column(Float, nullable=False)               # typed by the salesman
    confirmed_amount = Column(Float)                      # verified by finance (actual amount received)
    bank_reference = Column(String(80))
    image_path = Column(String(200), nullable=False)   # JPEG shown on screen
    file_kind = Column(String(10), default="image")     # "image" or "pdf"
    file_path = Column(String(200))                     # original PDF (pdf only)
    page_count = Column(Integer)
    image_hash = Column(String(64), nullable=False, index=True)
    status = Column(String(20), nullable=False, default="pending", index=True)
    reject_reason = Column(Text)
    finance_note = Column(Text)
    flags = Column(String(200))
    reviewed_by_id = Column(Integer, ForeignKey("users.id"))
    reviewed_at = Column(DateTime)
    transferred_at = Column(DateTime)
    created_at = Column(DateTime, default=utcnow, index=True)

    user = relationship("User", foreign_keys=[user_id])
    reviewed_by = relationship("User", foreign_keys=[reviewed_by_id])
    history = relationship("History", back_populates="remittance",
                           order_by="History.id", cascade="all, delete-orphan")


class History(Base):
    __tablename__ = "history"
    id = Column(Integer, primary_key=True)
    remittance_id = Column(Integer, ForeignKey("remittances.id"), nullable=False, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    action = Column(String(60), nullable=False)
    note = Column(Text)
    at = Column(DateTime, default=utcnow)
    remittance = relationship("Remittance", back_populates="history")
    user = relationship("User")


Base.metadata.create_all(engine)


def ensure_columns():
    """Add the PDF columns to a database created by an earlier version (keeps all data)."""
    have = {c["name"] for c in inspect(engine).get_columns("remittances")}
    wanted = {"file_kind": "VARCHAR(10) DEFAULT 'image'", "file_path": "VARCHAR(200)",
              "page_count": "INTEGER", "confirmed_amount": "FLOAT"}
    with engine.begin() as conn:
        for name, ddl in wanted.items():
            if name not in have:
                conn.execute(text(f"ALTER TABLE remittances ADD COLUMN {name} {ddl}"))


ensure_columns()


def hash_pw(pw: str) -> str:
    return bcrypt.hashpw(pw.encode(), bcrypt.gensalt()).decode()


def check_pw(pw: str, h: str) -> bool:
    try:
        return bcrypt.checkpw(pw.encode()[:72], h.encode())
    except ValueError:
        return False


def seed_admin():
    with SessionLocal() as db:
        if db.query(User).count():
            return
        username = os.getenv("ADMIN_USERNAME", "admin")
        pw = os.getenv("ADMIN_PASSWORD")
        generated = not pw
        pw = pw or secrets.token_urlsafe(9)
        db.add(User(username=username, full_name="Administrator",
                    password_hash=hash_pw(pw), role="admin"))
        db.commit()
        if generated:
            print(f"\n*** First start: admin user '{username}' created with password: {pw}\n"
                  f"*** Log in and change it.\n", flush=True)


seed_admin()

# -------------------------------------------------------------------- app
app = FastAPI(title="Remittance Tracker", docs_url=None, redoc_url=None, openapi_url=None)


@app.middleware("http")
async def security_headers(request: Request, call_next):
    resp = await call_next(request)
    resp.headers["X-Content-Type-Options"] = "nosniff"
    resp.headers["X-Frame-Options"] = "DENY"
    resp.headers["Referrer-Policy"] = "no-referrer"
    resp.headers["Permissions-Policy"] = "camera=(self), geolocation=(), microphone=()"
    resp.headers["Content-Security-Policy"] = (
        "default-src 'self'; script-src 'self'; "
        "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
        "font-src https://fonts.gstatic.com; img-src 'self' blob: data:; "
        "connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; "
        "form-action 'self'; object-src 'none'")
    if COOKIE_SECURE:
        resp.headers["Strict-Transport-Security"] = "max-age=31536000"
    if request.url.path.startswith("/api/"):
        resp.headers["Cache-Control"] = "no-store"
    return resp


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def current_user(request: Request, db: Session = Depends(get_db)) -> User:
    token = request.cookies.get("session")
    if not token:
        raise HTTPException(401, "Not logged in")
    try:
        data = jwt.decode(token, SECRET, algorithms=["HS256"])
    except jwt.PyJWTError:
        raise HTTPException(401, "Session expired")
    user = db.get(User, int(data["sub"]))
    if not user or not user.active:
        raise HTTPException(401, "Account disabled")
    return user


def finance_user(user: User = Depends(current_user)) -> User:
    if user.role not in ("finance", "admin"):
        raise HTTPException(403, "Finance only")
    return user


def admin_user(user: User = Depends(current_user)) -> User:
    if user.role != "admin":
        raise HTTPException(403, "Admin only")
    return user


def user_out(u: User):
    return {"id": u.id, "username": u.username, "full_name": u.full_name,
            "role": u.role, "active": u.active}


# ------------------------------------------------------------------- auth
_attempts: dict[str, list[float]] = {}


class LoginIn(BaseModel):
    username: str
    password: str


@app.post("/api/login")
def login(body: LoginIn, request: Request, response: Response, db: Session = Depends(get_db)):
    key = f"{request.client.host if request.client else '?'}|{body.username.lower()}"
    now = time.time()
    recent = [t for t in _attempts.get(key, []) if now - t < 600]
    if len(recent) >= 8:
        raise HTTPException(429, "Too many attempts. Try again in 10 minutes.")
    user = db.query(User).filter(func.lower(User.username) == body.username.strip().lower()).first()
    if not user or not user.active or not check_pw(body.password, user.password_hash):
        recent.append(now)
        _attempts[key] = recent
        raise HTTPException(401, "Wrong username or password")
    _attempts.pop(key, None)
    token = jwt.encode({"sub": str(user.id), "exp": utcnow() + timedelta(hours=SESSION_HOURS)},
                       SECRET, algorithm="HS256")
    response.set_cookie("session", token, httponly=True, samesite="strict",
                        secure=COOKIE_SECURE, max_age=SESSION_HOURS * 3600)
    return user_out(user)


@app.post("/api/logout")
def logout(response: Response):
    response.delete_cookie("session")
    return {"ok": True}


@app.get("/api/me")
def me(user: User = Depends(current_user)):
    return user_out(user)


class PasswordIn(BaseModel):
    current_password: str
    new_password: str


def check_new_password(pw: str):
    if len(pw) < 8 or len(pw.encode()) > 72:
        raise HTTPException(400, "Password must be 8 to 72 characters")


@app.post("/api/me/password")
def change_password(body: PasswordIn, user: User = Depends(current_user),
                    db: Session = Depends(get_db)):
    if not check_pw(body.current_password, user.password_hash):
        raise HTTPException(400, "Current password is wrong")
    check_new_password(body.new_password)
    user.password_hash = hash_pw(body.new_password)
    db.commit()
    return {"ok": True}


# ------------------------------------------------------------------ users
class UserIn(BaseModel):
    username: str
    full_name: str
    password: str
    role: str = "salesman"


class UserPatch(BaseModel):
    full_name: Optional[str] = None
    role: Optional[str] = None
    active: Optional[bool] = None
    password: Optional[str] = None


@app.get("/api/users")
def list_users(_: User = Depends(admin_user), db: Session = Depends(get_db)):
    return [user_out(u) for u in db.query(User).order_by(User.role, User.full_name)]


@app.post("/api/users")
def create_user(body: UserIn, _: User = Depends(admin_user), db: Session = Depends(get_db)):
    username = body.username.strip()
    if not username or not body.full_name.strip():
        raise HTTPException(400, "Username and name are required")
    if body.role not in ROLES:
        raise HTTPException(400, "Invalid role")
    check_new_password(body.password)
    if db.query(User).filter(func.lower(User.username) == username.lower()).first():
        raise HTTPException(409, "Username already exists")
    u = User(username=username, full_name=body.full_name.strip(),
             password_hash=hash_pw(body.password), role=body.role)
    db.add(u)
    db.commit()
    return user_out(u)


@app.patch("/api/users/{uid}")
def patch_user(uid: int, body: UserPatch, admin: User = Depends(admin_user),
               db: Session = Depends(get_db)):
    u = db.get(User, uid)
    if not u:
        raise HTTPException(404, "User not found")
    if u.id == admin.id and (body.active is False or (body.role and body.role != "admin")):
        raise HTTPException(400, "You cannot disable or demote your own account")
    if body.full_name is not None:
        u.full_name = body.full_name.strip() or u.full_name
    if body.role is not None:
        if body.role not in ROLES:
            raise HTTPException(400, "Invalid role")
        u.role = body.role
    if body.active is not None:
        u.active = body.active
    if body.password:
        check_new_password(body.password)
        u.password_hash = hash_pw(body.password)
    db.commit()
    return user_out(u)


@app.get("/api/salesmen")
def salesmen(_: User = Depends(finance_user), db: Session = Depends(get_db)):
    rows = db.query(User).filter(User.role == "salesman").order_by(User.full_name)
    return [{"id": u.id, "full_name": u.full_name, "active": u.active} for u in rows]


# ------------------------------------------------------------ remittances
def serial(r: Remittance) -> str:
    return f"REM-{r.id:06d}"


def remit_out(r: Remittance, detail: bool = False):
    d = {
        "id": r.id, "serial": serial(r), "salesman_id": r.user_id, "salesman": r.user.full_name,
        "kind": r.kind, "customer_name": r.customer_name, "amount": r.amount,
        "confirmed_amount": r.confirmed_amount,
        "amount_diff": (round(r.confirmed_amount - r.amount, 2)
                        if r.confirmed_amount is not None else None),
        "bank_reference": r.bank_reference, "status": r.status,
        "reject_reason": r.reject_reason, "finance_note": r.finance_note,
        "flags": r.flags.split(",") if r.flags else [],
        "reviewed_by": r.reviewed_by.full_name if r.reviewed_by else None,
        "reviewed_at": iso(r.reviewed_at), "transferred_at": iso(r.transferred_at),
        "created_at": iso(r.created_at), "image_url": f"/api/remittances/{r.id}/image",
        "file_kind": r.file_kind or "image", "page_count": r.page_count,
        "file_url": f"/api/remittances/{r.id}/file",
    }
    if detail:
        d["history"] = [{"action": h.action, "note": h.note, "by": h.user.full_name,
                         "at": iso(h.at)} for h in r.history]
    return d


def filtered(db: Session, user: User, status=None, salesman_id=None, q=None,
             date_from=None, date_to=None):
    qs = db.query(Remittance)
    if user.role == "salesman":
        qs = qs.filter(Remittance.user_id == user.id)
    elif salesman_id:
        qs = qs.filter(Remittance.user_id == salesman_id)
    if status:
        qs = qs.filter(Remittance.status == status)
    if q:
        like = f"%{q.strip().lower()}%"
        qs = qs.filter(func.lower(Remittance.customer_name).like(like)
                       | func.lower(func.coalesce(Remittance.bank_reference, "")).like(like))
    try:
        if date_from:
            qs = qs.filter(Remittance.created_at >= datetime.fromisoformat(date_from))
        if date_to:
            qs = qs.filter(Remittance.created_at < datetime.fromisoformat(date_to) + timedelta(days=1))
    except ValueError:
        raise HTTPException(400, "Invalid date")
    return qs


@app.get("/api/remittances")
def list_remittances(status: Optional[str] = None, salesman_id: Optional[int] = None,
                     q: Optional[str] = None, date_from: Optional[str] = None,
                     date_to: Optional[str] = None, limit: int = 200, offset: int = 0,
                     user: User = Depends(current_user), db: Session = Depends(get_db)):
    qs = filtered(db, user, status, salesman_id, q, date_from, date_to)
    rows = qs.order_by(Remittance.id.desc()).offset(offset).limit(min(limit, 500)).all()
    return [remit_out(r) for r in rows]


@app.get("/api/summary")
def summary(salesman_id: Optional[int] = None, q: Optional[str] = None,
            date_from: Optional[str] = None, date_to: Optional[str] = None,
            user: User = Depends(current_user), db: Session = Depends(get_db)):
    """Counts and amounts per status (uses the finance-confirmed amount when set;
    ignores the status filter so tabs always show totals)."""
    qs = filtered(db, user, None, salesman_id, q, date_from, date_to)
    out = {s: {"count": 0, "amount": 0.0} for s in TRANSITIONS}
    for s, c, a in qs.with_entities(Remittance.status, func.count(),
                                  func.sum(func.coalesce(Remittance.confirmed_amount, Remittance.amount))) \
            .group_by(Remittance.status):
        out[s] = {"count": c, "amount": round(a or 0, 2)}
    return out


def to_jpeg(img: Image.Image) -> bytes:
    img.thumbnail((1800, 5400))
    out = io.BytesIO()
    img.convert("RGB").save(out, "JPEG", quality=82, optimize=True)
    return out.getvalue()


def process_image(data: bytes) -> bytes:
    try:
        img = ImageOps.exif_transpose(Image.open(io.BytesIO(data)))
    except Exception:
        raise HTTPException(400, "The file is not a valid image or PDF")
    return to_jpeg(img)


MAX_PDF_PAGES = 30
PREVIEW_PAGES = 3


def process_pdf(data: bytes):
    """Render the first pages to one tall JPEG for viewing. Returns (jpeg_bytes, page_count)."""
    try:
        pdf = pdfium.PdfDocument(data)
    except Exception:
        raise HTTPException(400, "The PDF is damaged or password-protected")
    try:
        n = len(pdf)
        if n == 0 or n > MAX_PDF_PAGES:
            raise HTTPException(400, f"The PDF must have between 1 and {MAX_PDF_PAGES} pages")
        pages = []
        for i in range(min(n, PREVIEW_PAGES)):
            page = pdf[i]
            w, h = page.get_size()
            if w <= 0 or h <= 0:
                raise HTTPException(400, "The PDF page size is invalid")
            scale = min(1.6, 1800 / max(w, h))
            pages.append(page.render(scale=scale).to_pil().convert("RGB"))
    except HTTPException:
        raise
    except Exception:
        raise HTTPException(400, "The PDF could not be read")
    finally:
        pdf.close()
    width = max(p.width for p in pages)
    sheet = Image.new("RGB", (width, sum(p.height for p in pages) + 12 * (len(pages) - 1)), "white")
    y = 0
    for p in pages:
        sheet.paste(p, ((width - p.width) // 2, y))
        y += p.height + 12
    return to_jpeg(sheet), n


@app.post("/api/remittances")
async def create_remittance(
    kind: str = Form(...), customer_name: str = Form(...), amount: float = Form(...),
    bank_reference: str = Form(""), image: UploadFile = File(...),
    user: User = Depends(current_user), db: Session = Depends(get_db),
):
    if user.role != "salesman":
        raise HTTPException(403, "Only salesmen upload receipts")
    customer_name = customer_name.strip()
    bank_reference = bank_reference.strip() or None
    if kind not in KINDS:
        raise HTTPException(400, "Invalid type")
    if not customer_name:
        raise HTTPException(400, "Customer name is required")
    if not (0 < amount < 1_000_000_000):
        raise HTTPException(400, "Amount must be greater than zero")
    raw = await image.read(MAX_UPLOAD_BYTES + 1)
    if len(raw) > MAX_UPLOAD_BYTES:
        raise HTTPException(400, "File is larger than 15 MB")
    digest = hashlib.sha256(raw).hexdigest()
    dup = db.query(Remittance).filter(Remittance.image_hash == digest).first()
    if dup:
        raise HTTPException(409, f"This exact image was already uploaded ({serial(dup)})")

    flags = []
    if bank_reference and db.query(Remittance).filter(
            func.lower(Remittance.bank_reference) == bank_reference.lower()).first():
        flags.append("same_reference")
    day_start = utcnow().replace(hour=0, minute=0, second=0, microsecond=0)
    if db.query(Remittance).filter(
            func.lower(Remittance.customer_name) == customer_name.lower(),
            Remittance.amount == round(amount, 2),
            Remittance.created_at >= day_start - timedelta(days=1)).first():
        flags.append("same_customer_amount")

    is_pdf = b"%PDF-" in raw[:1024]
    base = secrets.token_hex(12)
    fname, pdf_name, pages = f"{base}.jpg", None, None
    if is_pdf:
        jpeg, pages = await run_in_threadpool(process_pdf, raw)
        pdf_name = f"{base}.pdf"
        (UPLOAD_DIR / pdf_name).write_bytes(raw)
    else:
        jpeg = await run_in_threadpool(process_image, raw)
    (UPLOAD_DIR / fname).write_bytes(jpeg)
    r = Remittance(user_id=user.id, kind=kind, customer_name=customer_name,
                   amount=round(amount, 2), bank_reference=bank_reference,
                   image_path=fname, file_kind="pdf" if is_pdf else "image",
                   file_path=pdf_name, page_count=pages,
                   image_hash=digest, flags=",".join(flags) or None)
    db.add(r)
    db.flush()
    db.add(History(remittance_id=r.id, user_id=user.id, action="uploaded"))
    db.commit()
    db.refresh(r)
    return remit_out(r)


def get_allowed(db: Session, user: User, rid: int) -> Remittance:
    r = db.get(Remittance, rid)
    if not r or (user.role == "salesman" and r.user_id != user.id):
        raise HTTPException(404, "Not found")
    return r


@app.get("/api/remittances/{rid}")
def get_remittance(rid: int, user: User = Depends(current_user), db: Session = Depends(get_db)):
    return remit_out(get_allowed(db, user, rid), detail=True)


@app.get("/api/remittances/{rid}/image")
def get_image(rid: int, user: User = Depends(current_user), db: Session = Depends(get_db)):
    r = get_allowed(db, user, rid)
    path = UPLOAD_DIR / r.image_path
    if not path.exists():
        raise HTTPException(404, "Image missing")
    return FileResponse(path, media_type="image/jpeg",
                        headers={"Cache-Control": "private, max-age=3600"})


@app.get("/api/remittances/{rid}/file")
def get_file(rid: int, user: User = Depends(current_user), db: Session = Depends(get_db)):
    """Original upload: the PDF as a download, or the stored image."""
    r = get_allowed(db, user, rid)
    if r.file_kind == "pdf" and r.file_path:
        path = UPLOAD_DIR / r.file_path
        if not path.exists():
            raise HTTPException(404, "File missing")
        return FileResponse(path, media_type="application/pdf", filename=f"{serial(r)}.pdf",
                            headers={"Cache-Control": "private, max-age=3600"})
    return get_image(rid, user, db)


class StatusIn(BaseModel):
    status: str
    reason: Optional[str] = None
    note: Optional[str] = None
    confirmed_amount: Optional[float] = None


@app.post("/api/remittances/{rid}/status")
def set_status(rid: int, body: StatusIn, user: User = Depends(finance_user),
               db: Session = Depends(get_db)):
    r = get_allowed(db, user, rid)
    if body.status not in TRANSITIONS.get(r.status, set()):
        raise HTTPException(400, f"Cannot move from {r.status} to {body.status}")
    if body.status == "rejected" and not (body.reason or "").strip():
        raise HTTPException(400, "A rejection reason is required")
    if body.confirmed_amount is not None and not (0 < body.confirmed_amount < 1_000_000_000):
        raise HTTPException(400, "Confirmed amount must be greater than zero")
    if body.status == "transferred" and body.confirmed_amount is None and r.confirmed_amount is None:
        raise HTTPException(400, "Enter the amount actually received before marking as transferred")
    old = r.status
    r.status = body.status
    amount_note = None
    if body.status in ("confirmed", "transferred"):
        if body.confirmed_amount is not None:
            r.confirmed_amount = round(body.confirmed_amount, 2)
        if r.confirmed_amount is not None and abs(r.confirmed_amount - r.amount) > 0.004:
            amount_note = f"amount {r.amount:g} -> {r.confirmed_amount:g}"
    else:
        r.confirmed_amount = None
    r.reviewed_by_id = user.id
    r.reviewed_at = utcnow()
    r.reject_reason = body.reason.strip() if body.status == "rejected" else None
    if body.note is not None:
        r.finance_note = body.note.strip() or None
    r.transferred_at = utcnow() if body.status == "transferred" else None
    db.add(History(remittance_id=r.id, user_id=user.id, action=f"{old} to {body.status}",
                   note=" | ".join(x for x in [
                       body.reason if body.status == "rejected" else body.note, amount_note] if x) or None))
    db.commit()
    db.refresh(r)
    return remit_out(r, detail=True)


@app.delete("/api/remittances/{rid}")
def delete_remittance(rid: int, user: User = Depends(current_user), db: Session = Depends(get_db)):
    r = get_allowed(db, user, rid)
    if user.role == "salesman" and (r.user_id != user.id or r.status != "pending"):
        raise HTTPException(400, "Only your own pending uploads can be deleted")
    if user.role == "finance":
        raise HTTPException(403, "Finance cannot delete uploads")
    paths = [UPLOAD_DIR / r.image_path] + ([UPLOAD_DIR / r.file_path] if r.file_path else [])
    db.delete(r)
    db.commit()
    for p in paths:
        p.unlink(missing_ok=True)
    return {"ok": True}


@app.get("/api/export.csv")
def export_csv(request: Request, key: str = "", status: Optional[str] = None,
               salesman_id: Optional[int] = None, date_from: Optional[str] = None,
               date_to: Optional[str] = None, db: Session = Depends(get_db)):
    """CSV for Excel / Power BI. Logged-in finance, or ?key=EXPORT_KEY for scheduled refresh."""
    if EXPORT_KEY and secrets.compare_digest(key, EXPORT_KEY):
        qs = filtered(db, User(role="admin"), status, salesman_id, None, date_from, date_to)
    else:
        qs = filtered(db, finance_user(current_user(request, db)), status, salesman_id,
                      None, date_from, date_to)
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["serial", "created_at_utc", "salesman", "type", "file_type", "customer", "amount",
                "confirmed_amount", "amount_difference", "bank_reference", "status", "reject_reason", "finance_note", "flags",
                "reviewed_by", "reviewed_at_utc", "transferred_at_utc"])
    for r in qs.order_by(Remittance.id):
        w.writerow([serial(r), iso(r.created_at), r.user.full_name, r.kind, r.file_kind or "image", r.customer_name,
                    r.amount, "" if r.confirmed_amount is None else r.confirmed_amount,
                    "" if r.confirmed_amount is None else round(r.confirmed_amount - r.amount, 2),
                    r.bank_reference or "", r.status, r.reject_reason or "",
                    r.finance_note or "", r.flags or "",
                    r.reviewed_by.full_name if r.reviewed_by else "",
                    iso(r.reviewed_at) or "", iso(r.transferred_at) or ""])
    data = ("\ufeff" + buf.getvalue()).encode("utf-8")
    return StreamingResponse(io.BytesIO(data), media_type="text/csv; charset=utf-8",
                             headers={"Content-Disposition": 'attachment; filename="remittances.csv"'})


@app.get("/healthz")
def healthz():
    return {"ok": True}


app.mount("/", StaticFiles(directory=BASE / "static", html=True), name="static")
