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

from . import answers, llm, parse, rag, store
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


class FieldReq(BaseModel):
    label: str
    field_type: str = "text"
    options: list[str] | None = None
    url: str = ""
    jd_context: str = ""
    allow_llm: bool = True


class BatchReq(BaseModel):
    url: str = ""
    jd_context: str = ""
    fields: list[FieldReq]


class LearnReq(BaseModel):
    url: str = ""
    fields: list[dict]


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


# --------------------------------------------------------------- autofill (apply)
@app.get("/api/profile")
def api_profile() -> dict:
    """Normalized profile the extension uses for instant deterministic fills."""
    return {"profile": answers.load_profile()}


@app.post("/api/answer")
async def api_answer(req: FieldReq) -> dict:
    """Resolve ONE form field to a truthful answer (bank -> profile -> fuzzy -> LLM)."""
    res = await run_in_threadpool(
        answers.resolve,
        label=req.label,
        field_type=req.field_type,
        options=req.options or [],
        url=req.url,
        jd_context=req.jd_context,
        allow_llm=req.allow_llm,
    )
    return res.to_dict()


@app.post("/api/answer/batch")
async def api_answer_batch(req: BatchReq) -> dict:
    """Resolve many fields in one round-trip (the autofill hot path)."""
    def _run() -> list[dict]:
        out = []
        for f in req.fields:
            res = answers.resolve(
                label=f.label,
                field_type=f.field_type,
                options=f.options or [],
                url=f.url or req.url,
                jd_context=f.jd_context or req.jd_context,
                allow_llm=f.allow_llm,
            )
            out.append({"label": f.label, **res.to_dict()})
        return out

    results = await run_in_threadpool(_run)
    return {"results": results}


@app.post("/api/answer/learn")
async def api_answer_learn(req: LearnReq) -> dict:
    """Record what was actually submitted so the bank improves over time."""
    written = await run_in_threadpool(answers.learn, req.fields, url=req.url)
    return {"ok": True, "learned": written, "stats": answers.stats()}


@app.get("/api/answer/stats")
def api_answer_stats() -> dict:
    return answers.stats()


@app.get("/api/resume/match")
def api_resume_match(company: str = "", role: str = "") -> dict:
    """Find the most recent tailored resume PDF matching this company/role, if any.

    The extension calls this to pick which PDF to attach to a given application.
    """
    def norm(s: str) -> str:
        return "".join(ch for ch in (s or "").lower() if ch.isalnum())

    cwant, rwant = norm(company), norm(role)
    best = None
    if PATHS.outputs.exists():
        for d in sorted(PATHS.outputs.iterdir(), reverse=True):
            pdf = d / "resume.pdf"
            if not (d.is_dir() and pdf.exists()):
                continue
            name = norm(d.name)
            score = 0
            if cwant and cwant in name:
                score += 2
            if rwant and rwant in name:
                score += 1
            if score > 0 or best is None:
                cand = {
                    "out_dir": d.name,
                    "pdf_url": f"/outputs/{d.name}/resume.pdf",
                    "score": score,
                }
                if best is None or score > best["score"]:
                    best = cand
            if best and best["score"] == 3:
                break
    if not best:
        return {"match": None}
    return {"match": best}


# ----------------------------------------------------------------- pipeline (auto)
class PipelineRunReq(BaseModel):
    use_llm: bool = True
    do_tailor: bool = True


@app.post("/api/pipeline/run")
async def api_pipeline_run(req: PipelineRunReq) -> dict:
    """Run discover -> score -> tailor -> queue once. Heavy; runs off the event loop."""
    from . import pipeline
    report = await run_in_threadpool(pipeline.run, use_llm=req.use_llm, do_tailor=req.do_tailor)
    return report.to_dict()


@app.get("/api/pipeline/status")
def api_pipeline_status() -> dict:
    from . import jobsdb
    return {"counts": jobsdb.counts(), "applied_today": jobsdb.applied_today()}


@app.get("/api/pipeline/queue")
def api_pipeline_queue() -> dict:
    """Review queue: each tailored/queued job with its pre-resolved answer draft."""
    from . import apply_driver, jobsdb
    jobs = jobsdb.by_state("queued") + jobsdb.by_state("tailored")
    return {"queue": [apply_driver.build_draft(j).to_dict() for j in jobs]}


@app.post("/api/pipeline/applied")
def api_pipeline_applied(req: IngestReq) -> dict:
    """Mark a job applied (the extension calls this after a reviewed submit)."""
    from . import jobsdb
    uid = (req.jd_text or req.jd or "").strip()  # reuse field as the uid carrier
    if not uid:
        raise HTTPException(status_code=422, detail="missing uid")
    jobsdb.set_state(uid, "applied", note="submitted via extension")
    return {"ok": True, "applied_today": jobsdb.applied_today()}
