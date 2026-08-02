# Phase 1: Environment & Container Setup

Status: done and verified, 2026-08-02.

## What got built

```
semantic-queue/
├── app/
│   ├── __init__.py
│   └── main.py          # placeholder FastAPI app — see "Deviations" below
├── config/               # empty, .gitkeep only — for Phase 2+ config files
├── workers/
│   └── __init__.py       # empty package — Phase 4 puts inference_worker.py here
├── tests/
│   └── __init__.py       # empty package — Phase 5 puts pytest suite here
├── phases/
│   └── phase-1.md        # this file
├── Dockerfile
├── docker-compose.yml
└── requirements.txt
```

## The pieces, and why each exists

**`requirements.txt`** — pins every direct dependency to an exact version, so
"works on my machine" doesn't rot as the packages get updated upstream. See
"Version decisions" below for why these specific versions.

**`Dockerfile`** — describes how to build the `api` image: start from a slim
Python 3.12 base, install dependencies, copy the source, run uvicorn. It copies
`requirements.txt` and runs `pip install` *before* copying the rest of the
source code on purpose — Docker caches each instruction as a layer, so if you
only change application code (not dependencies), rebuilding skips the slow
`pip install` step entirely and reuses the cached layer. Without that
ordering, every code change would force a multi-minute dependency
reinstall.

**`docker-compose.yml`** — describes how the `api` and `redis` containers run
*together*: what image each uses, what ports are exposed, and that `api`
shouldn't start handling traffic until `redis` reports healthy. A Dockerfile
only knows how to build one image; compose is what wires multiple containers
into one runnable system with one command (`docker compose up`).

**Folder structure (`app/`, `config/`, `workers/`, `tests/`, `phases/`)** —
mirrors the architecture from `plan.md`: `app/` is the FastAPI gateway
(rate limiter, cache, endpoints — Phases 2–3), `workers/` is the background
inference worker (Phase 4), `tests/` is the Phase 5 test suite, `config/` is
reserved for any config files that show up later (e.g. tunable thresholds).
`phases/` is new — a running log of what was actually built each phase, for
future-me to read instead of re-deriving it from git history.

## How `api` and `redis` talk to each other (plain-language analogy)

Think of `docker compose up` as moving into a shared apartment with a
roommate you just met — Docker builds a private hallway (a Docker network,
`semantic-queue_default`) that connects your two rooms (`api` and `redis`)
and *nobody outside the apartment* can walk into that hallway. Inside the
hallway, each room is reachable by its roommate's name, not a random street
address — that's why `app/main.py` (once it needs Redis in Phase 2) will
connect to host `redis`, not `localhost` or an IP: compose's built-in DNS
resolves the service name `redis` to whatever internal IP that container
actually got, which can change every time it restarts.

Separately, the `ports:` section in `docker-compose.yml` is like installing
one doorbell per room that also rings from the street outside the apartment
— `8001:8000` means "traffic hitting port 8001 on this machine gets routed
to port 8000 inside the `api` container," and `6379:6379` does the same for
Redis. That's *only* for you (or a browser, or `redis-cli` on the host) to
reach in from outside; `api` itself doesn't need the doorbell to reach
`redis` — it just walks down the hallway.

The `depends_on: redis: condition: service_healthy` line means the `api`
room doesn't unlock its own door until the roommate confirms (via Redis's
`redis-cli ping` healthcheck) that they're actually awake and answering —
otherwise `api` could start up first and fail the moment Phase 2 code tries
to talk to a Redis that isn't listening yet.

## Version-compatibility decisions

All direct dependencies are pinned to specific versions released within a
~7-week window (mid-May to late-June 2026), not whatever was newest at
build time (which, as of today, includes releases from *yesterday* — too
fresh to trust for a portfolio project). Checked against PyPI's actual
release history rather than guessed:

| Package | Version | Notes |
|---|---|---|
| fastapi | 0.138.2 | |
| uvicorn[standard] | 0.49.0 | `[standard]` pulls in uvloop + watchfiles (needed for `--reload`) |
| redis | 8.0.1 | `redis.asyncio` (needed from Phase 2 on) has been built in since v4.2 |
| torch | 2.12.1+cpu | see below |
| sentence-transformers | 5.6.0 | declares `torch>=1.11.0`, `numpy>=1.20.0` — no upper pins that would fight the versions above |
| numpy | 2.4.6 | |
| locust | 2.44.4 | |

**CPU-only PyTorch build.** `requirements.txt` adds
`--extra-index-url https://download.pytorch.org/whl/cpu` and pins
`torch==2.12.1+cpu` (not plain `2.12.1`). The default PyPI build of torch
bundles CUDA and is multiple GB larger; this container has no GPU
passthrough (plain WSL2 Docker, no `--gpus` flag), so that size buys
nothing. The `+cpu` version suffix only exists on PyTorch's own index, not
on PyPI, so pip can't accidentally resolve the CUDA build instead — verified
the exact `cp312` (Python 3.12) wheel exists on that index before pinning it,
and confirmed via the actual `docker compose build` output that pip pulled
`torch-2.12.1+cpu` from `download.pytorch.org`, not PyPI.

**Compatibility was verified by actually building**, not just by reading
declared dependency ranges: `docker compose build` ran pip's real dependency
resolver against all seven pins together, and it resolved and installed
cleanly with no conflicts (full install log has 60+ resolved packages,
`Successfully installed ...` with no errors).

## Deviations from `plan.md`

1. **Placeholder `app/main.py`.** Phase 1's "done when" criterion requires
   `docker-compose up` to boot the `api` container *cleanly* — but the real
   FastAPI gateway (rate limiter, `/health`, `/v1/predict`) is explicit
   Phase 2 scope. Added a minimal placeholder (`FastAPI()` with a single
   `GET /` returning `{"status": "ok"}`) purely so uvicorn has something to
   serve. This gets replaced, not extended, in Phase 2.

2. **`api` published on host port 8001, not 8000.** Host port 8000 was
   already bound by an unrelated `k3d-storm-local` Kubernetes cluster
   running on this machine (a different project's infrastructure — left
   untouched). `docker-compose.yml` maps `8001:8000`, so the container's
   internal port is still 8000, only the host-side mapping changed. Nothing
   in `plan.md` depends on the literal number 8000, so this doesn't affect
   Phase 2+. Re-checked in a follow-up pass: `ports:` under `api` in
   `docker-compose.yml` contains only `"8001:8000"` — no `8000:8000` ever
   got reintroduced.

3. **Live-reload dev setup added.** `docker-compose.yml` bind-mounts the
   project directory into `/code` and the Dockerfile's `CMD` runs uvicorn
   with `--reload`, so Phase 2+ code edits take effect without rebuilding
   the image. Not called out explicitly in `plan.md`, but implied by
   needing to iterate on `app/` and `workers/` over the next several days.

4. **Redis healthcheck + `depends_on: condition: service_healthy`.** Not
   specified in `plan.md`, added so `api` can't start before `redis` is
   actually accepting connections — otherwise Phase 2's rate limiter would
   intermittently fail to connect on a cold `docker compose up`.

5. **Non-root `USER` in the Dockerfile (added after initial Phase 1 pass).**
   The first version of the container ran as root, which meant every
   `--reload` recompile wrote `__pycache__/*.pyc` into the bind-mounted
   project directory owned by `root` — those files couldn't be deleted or
   modified from the host without `sudo`/container access, a real friction
   point over 4 more days of iterating on `app/` and `workers/`. Fixed by
   adding `appuser` with UID/GID `1000:1000` (the default first-user WSL2
   UID, confirmed against the host via `id`) and switching to it with
   `USER appuser` before `CMD`. Any files the container writes into the
   bind mount now land owned by the host user. Verified by touching
   `app/main.py` to force a `--reload` recompile and checking the
   resulting `app/__pycache__/*.pyc` ownership — see verification steps
   below.

## How to verify this still works later

```bash
cd ~/Projects/semantic-queue

# Docker Desktop must be running first (WSL2 backend) — if `docker info`
# fails, start Docker Desktop from Windows and wait ~30-60s.
docker info

# Build and start both containers
docker compose up -d --build

# Both should show "Up", redis should show "(healthy)"
docker compose ps

# API is reachable on the host at 8001 (not 8000 — see Deviations #2)
curl http://localhost:8001/
# expect: {"status":"ok"}

# Redis reachable from inside the api container (redis-tools isn't
# installed in the slim image, so this uses the redis Python client
# that's already a dependency — equivalent to `redis-cli ping`)
docker compose exec api python -c "
import redis
r = redis.Redis(host='redis', port=6379, decode_responses=True)
print('PING ->', r.ping())
"
# expect: PING -> True

# Container should run as appuser (1000:1000), not root
docker compose exec api id
# expect: uid=1000(appuser) gid=1000(appuser) groups=1000(appuser)

# No file in the project dir should be root-owned (run from the WSL host,
# project root)
find . -user root -not -path "./.git/*"
# expect: no output

# Tear down when done
docker compose down
```
