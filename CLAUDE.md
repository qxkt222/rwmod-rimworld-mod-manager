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
  exception: the status code carries the failure and the body is `{error, detail}`.
  A 200 body carries data only — an `{"error": ...}` key inside a 200 is a contract
  regression, not a warning channel. The exception is a *probe*, whose job is to
  report a verdict: `/api/steamcmd/check` legitimately returns `{ok: false, msg}`.
- **`python run_tests.py` is the local gate** — import smoke, ruff, ruff format,
  pytest, mypy strict and bandit in one shot.
