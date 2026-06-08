"""FastAPI backend: serves the browser UI and the Chrome-extension /ingest endpoint.

Routes
    GET  /                  -> the single-page UI
    GET  /api/health        -> Ollama version + Tectonic availability
    POST /api/parse         -> JD text -> structured brief (confirm company/role)
    POST /api/generate      -> JD text -> tailored, compiled, ATS-checked PDF
    POST /api/correct       -> append a correction to corrections.md
    POST /ingest            -> Chrome extension hands off a captured JD
    GET  /api/pending       -> the last ingested JD (for the UI to load)
    /static/*               -> UI assets;  /outputs/*  -> generated PDFs
"""
from __future__ import annotations

import shutil

from fastapi import FastAPI, HTTPException
from fastapi.concurrency import run_in_threadpool
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import llm, parse, rag, store
from .cli import generate_resume
from .compile import _resolve_tectonic
from .config import PATHS, load_config

app = FastAPI(title="Resume Tailor")

# The Chrome extension posts from a chrome-extension:// origin; allow that plus
# localhost. This is a local single-user tool, so the policy is permissive.
app.add_middleware(
    CORSMiddleware,
    allow_origin_regex=r"^(chrome-extension://.*|https?://localhost(:\d+)?|https?://127\.0\.0\.1(:\d+)?)$",
    allow_methods=["*"],
    allow_headers=["*"],
)

PATHS.outputs.mkdir(parents=True, exist_ok=True)
app.mount("/outputs", StaticFiles(directory=str(PATHS.outputs)), name="outputs")
_STATIC = PATHS.root / "app" / "static"
app.mount("/static", StaticFiles(directory=str(_STATIC)), name="static")

# In-memory hand-off slot for the Chrome extension (single user).
_pending: dict | None = None


class ParseReq(BaseModel):
    jd_text: str


class GenerateReq(BaseModel):
    jd_text: str
    company: str | None = None
    role: str | None = None


class CorrectReq(BaseModel):
    text: str
    company: str | None = ""
    role: str | None = ""
    jd_context: str | None = ""


class IngestReq(BaseModel):
    jd_text: str | None = None
    jd: str | None = None
    company: str | None = None
    role: str | None = None


@app.get("/")
def index() -> FileResponse:
    return FileResponse(str(_STATIC / "index.html"))


@app.get("/api/health")
def health() -> dict:
    cfg = load_config()
    out: dict = {}
    try:
        h = llm.health()  # {provider, model, ok, detail}
        out.update(h)
    except Exception as exc:  # noqa: BLE001
        out["provider"] = None
        out["ok"] = False
        out["detail"] = str(exc)
    try:
        out["tectonic"] = bool(_resolve_tectonic(cfg))
    except Exception:  # noqa: BLE001
        out["tectonic"] = False
    return out


@app.post("/api/parse")
def api_parse(req: ParseReq) -> dict:
    try:
        jd = parse.parse_jd(req.jd_text)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    date, company, role = parse.slugs(jd)
    return {"jd": jd.model_dump(), "date": date, "company_slug": company, "role_slug": role}


@app.post("/api/generate")
async def api_generate(req: GenerateReq) -> dict:
    try:
        res = await run_in_threadpool(
            generate_resume, req.jd_text, company=req.company, role=req.role
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except llm.LLMError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except Exception as exc:  # compile or other failure
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    rel = res.out_dir.relative_to(PATHS.root).as_posix()
    return {
        "out_dir": rel,
        "pdf_url": f"/outputs/{res.out_dir.name}/resume.pdf",
        "tex_url": f"/outputs/{res.out_dir.name}/tailored.tex",
        "changelog": res.changelog,
        "missing_requirements": res.missing_requirements,
        "keywords_used": res.keywords_used,
        "ats_ok": res.ats_ok,
        "ats_issues": res.ats_issues,
        "grounding_ok": res.grounding_ok,
        "grounding_violations": res.grounding_violations,
        "pages": res.pages,
        "one_page": res.one_page,
        "trims": res.trims,
        "tells": res.tells,
    }


@app.post("/api/correct")
def api_correct(req: CorrectReq) -> dict:
    try:
        line = store.append_correction(req.text, company=req.company or "", role=req.role or "")
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    # Also index it for RAG (best-effort). Context = the JD it was made against,
    # falling back to "role @ company".
    ctx = (req.jd_context or "").strip() or f"{req.role or ''} @ {req.company or ''}".strip(" @")
    indexed = rag.add(req.text, jd_context=ctx, kind="correction", source="ui")
    return {"ok": True, "line": line, "rag_indexed": indexed}


@app.post("/ingest")
def ingest(req: IngestReq) -> dict:
    global _pending
    text = (req.jd_text or req.jd or "").strip()
    if len(text) < 30:
        raise HTTPException(status_code=422, detail="Ingested JD looks empty.")
    _pending = {"jd_text": text, "company": req.company, "role": req.role}
    return {"ok": True, "chars": len(text)}


@app.get("/api/pending")
def pending() -> dict:
    return {"pending": _pending}
