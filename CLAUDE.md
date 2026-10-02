## Agent skills

### Issue tracker

Issues, specs and one-off investigation scripts live under `.scratch/<feature>/`
(gitignored — put scratch there rather than in the repo root).
See `docs/agents/issue-tracker.md`.

### Domain docs

Single-context repo. `CONTEXT.md` + `docs/adr/` are created lazily by the
domain-modeling skill once terms and decisions actually resolve; neither exists yet.
See `docs/agents/domain.md`.

## Ground rules

Invariants that are invisible from the code layout and expensive to rediscover:

- **Packaging layout is load-bearing.** `utils.bundle_root()` walks three parents up
  from `rwmod/utils.py`, so the package must stay at `<root>/src/rwmod`. Installing it
  into site-packages makes `bundle_root()` resolve to `<python>/lib/...` and the static
  mount silently 404s. The Docker image therefore keeps the src layout and sets
  `PYTHONPATH=/app/src`.
- **Version and dependencies have one source: `pyproject.toml`.** `run_tests.py`
  asserts `rwmod.__version__` matches it, and the Dockerfile derives its dependency
  list from it — so neither needs a hand-kept second copy.
- **Shared state is injected through the module-level singletons in `deps.py`**
  (config, queue, autoupdate). New shared state belongs there. `app_state.py` claimed
  that job while never being consumed; it was deleted rather than left as a decoy.
- **Untrusted XML goes through `xmlutil.parse_xml_root`.** Stdlib `xml.etree` is
  reserved for XML this process generated or that RimWorld wrote; the remaining call
  sites are tracked as debt in the `[tool.bandit]` comment in `pyproject.toml`.
- **Failure contract.** An endpoint that fails to do its job raises an `errors.py`
  exception: the status code carries the failure and the body is `{error, detail}`
  with a string detail. A 200 body carries data only — an `{"error": ...}` key inside
  a 200 is a contract regression, not a warning channel. Request-body validation also
  answers in that shape (422) instead of FastAPI's default `{"detail": [...]}`, and a
  *probe* whose job is to report a verdict may use `{ok: false, msg}` as
  `/api/steamcmd/check` does. Raising plain `HTTPException` yields the same shape with
  `error: "HTTPError"` — seeing that generic name marks a site worth migrating.
- **`static/` is generated, never committed.** It used to be half-tracked
  (`static/index.html` in git, `static/assets/**` ignored), so every build dirtied the
  tree and a fresh clone served an index.html whose assets 404'd. The whole directory is
  now ignored: running from source and packaging both require building the frontend
  first (`cd frontend && npm install && npm run build`, or `python tools/build_frontend.py`),
  and `/` answers 503 with that instruction when the build is missing.
- **`python run_tests.py` is the local gate** — import smoke, ruff, ruff format,
  pytest, mypy strict and bandit in one shot.
