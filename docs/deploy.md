# Deploying Reviewhound

Reviewhound runs as two processes:

- `reviewhound serve`: receives GitHub webhooks, puts one job per PR head on the queue, and
  serves `/healthz` and `/metrics`. It is small and needs no model access.
- `reviewhound worker`: takes jobs off the queue, checks out the PR, asks the model, verifies
  the findings and posts (or stores, in shadow mode) the review. Run one or more.

Both use the same database. With no database URL they use SQLite files in `--home`, which is
fine for one machine. For more than one host, or for containers, use Postgres.

## Environment variables

| Variable | Used by | Meaning |
|---|---|---|
| `REVIEWHOUND_DATABASE_URL` | all | A `postgresql://` URL (user, password, host, port 5432, database). Empty or unset: SQLite files in `--home`. Any other scheme is refused. |
| `REVIEWHOUND_HOME` | all | State directory (default `~/.reviewhound`, `/data` in the image). Holds SQLite files and git checkouts. |
| `REVIEWHOUND_WEBHOOK_SECRET` | serve | The webhook secret configured on GitHub. `serve` refuses to start without it. |
| `GITHUB_TOKEN` | worker | Token mode: a token that can read the repositories and post reviews. Leave unset in App mode. |
| `REVIEWHOUND_APP_ID` | serve, worker | App mode: the GitHub App's id. Set together with the key path. |
| `REVIEWHOUND_APP_PRIVATE_KEY_PATH` | worker | App mode: path to the App's private key file (compose mounts it at `/run/secrets/github_app_key`). |
| `REVIEWHOUND_METRICS_TOKEN` | serve | When set, `/metrics` requires `Authorization: Bearer <token>`. Set it: `/metrics` is served on the published webhook port. |
| `REVIEWHOUND_ALLOWED_OWNERS` | serve | The repository owners whose webhooks become jobs (format: `serve --help`); others are ignored. Unset or empty allows every owner, so set it; only spaces or commas stops `serve` at start. |
| `CLAUDE_CODE_OAUTH_TOKEN` | worker | Lets the `claude` CLI run without an interactive login (create one with `claude setup-token`). The `claude` process gets only `PATH`, `HOME`, `USER`, `LOGNAME`, `LANG`, `TMPDIR`, `SHELL`, this token and `CLAUDE_CONFIG_DIR`; `ANTHROPIC_API_KEY`, `GITHUB_TOKEN`, the database URL and the webhook secret never reach it. |

Useful flags:

- `--backlog-alarm-s` (default 600): a queued job older than this counts as stuck.
- `worker --backlog-alarm-interval-s` (default 300): the worker logs the `backlog_alarm`
  error event at most once per interval while anything is stuck.
- `worker --max-job-seconds` (default 1800): after this long a review stops renewing its
  lease, posts nothing (outcome `lease-lost`) and the job is retried; it ends in `dead` after
  the usual attempts. A wedged review stops renewing too, so its lease expires and another
  worker reclaims the job.
- `--max-proof-seconds` (default 300): total sandbox time for all proofs in one review,
  whatever a repo's `proof.timeout_s` asks for. When it runs out, the remaining proofs are
  inconclusive and no more proof prompts are sent. The Docker runner's own worst case (115 s with
  the default start grace) counts against it too, so keep it above about 116 s.
- `--allow-local-proof-repo OWNER/NAME` (repeatable): the only repos whose
  `proof.runner: local` may run generated tests as plain processes on the worker. A repo's own
  config can never enable it. See "Proof runs in Docker" for the default runner.
- `--proof-image` (default `reviewhound-proof:latest`): the image for `proof.runner: docker`.
- `reviewhound stats` prints review counts, time per stage, cost, failures and the queue;
  `reviewhound stats --prometheus` prints exactly what `/metrics` serves.

Never put secrets in the image, the compose file or the repository. Pass them from the
environment or a secret store. A `.env` file next to `docker-compose.yml` works locally and is
already in `.dockerignore`; keep it out of git.

## Running with Docker Compose

```bash
docker compose build
export REVIEWHOUND_WEBHOOK_SECRET=... GITHUB_TOKEN=... CLAUDE_CODE_OAUTH_TOKEN=...
export POSTGRES_PASSWORD=...
export REVIEWHOUND_DATABASE_URL="postgresql://reviewhound:${POSTGRES_PASSWORD}@postgres:5432/reviewhound"
docker compose --profile postgres up -d
```

Without `--profile postgres` the bundled database is not started; point
`REVIEWHOUND_DATABASE_URL` at your own Postgres instead. The tables are created on first start.

The worker image is built with `INSTALL_CLAUDE_CLI=1`, which adds Node and the `claude` CLI at
the version pinned by `CLAUDE_CODE_VERSION` in the Dockerfile. Bump it deliberately
(`--build-arg CLAUDE_CODE_VERSION=x.y.z`); never use `latest`. The `uv` build image is pinned by
version the same way.

### As a GitHub App

```bash
export REVIEWHOUND_APP_ID=123456
export REVIEWHOUND_APP_PRIVATE_KEY_FILE=/etc/reviewhound/github-app.pem   # on the host
docker compose -f docker-compose.yml -f docker-compose.app.yml --profile postgres up -d
```

`docker-compose.app.yml` passes the App id to both services and mounts the key file read-only
as the secret `/run/secrets/github_app_key`; the key never enters the environment or the
image. The worker runs as uid 10001, so the host file must be readable by it (for example
`chown 10001 github-app.pem && chmod 0400 github-app.pem`). `GITHUB_TOKEN` is not needed.

Only the webhook port is published. The same port serves `/healthz` and `/metrics`, so set
`REVIEWHOUND_METRICS_TOKEN`, and scrape with that bearer token.

## GitHub webhook setup

1. In the repository (or GitHub App) settings, add a webhook with payload URL
   `https://<your host>/webhook` and content type `application/json`.
2. Set the secret to the same value as `REVIEWHOUND_WEBHOOK_SECRET`.
3. Choose "Let me select individual events" and tick **Pull requests**. Other events are
   accepted and ignored.
4. GitHub sends a `ping`; the server answers `pong`. Check "Recent Deliveries" for a 200.

Put TLS in front of `serve` (a reverse proxy or load balancer). Set `REVIEWHOUND_METRICS_TOKEN`
so `/metrics` needs `Authorization: Bearer <token>`; without it `/metrics` is open to anyone
who can reach the port. Prefer also keeping `/metrics` off the public route at the proxy.

A new repository runs in shadow mode (reviews are stored, nothing is posted) until its
`.reviewhound.yml` on the base branch says `posting: true`.

## Scaling workers

Add workers freely: `docker compose up -d --scale worker=3`, or more hosts with the same
`REVIEWHOUND_DATABASE_URL`.

- Each job is claimed by exactly one worker (`FOR UPDATE SKIP LOCKED` on Postgres).
- While a review runs, the worker renews its lease every `lease_s / 3` (lease default 30
  minutes). If a worker dies, its lease expires and another worker retries the job.
- A review that loses its lease, or runs past `--max-job-seconds`, logs a `lease_lost` error,
  posts nothing (outcome `lease-lost`) and the job is retried. At most one review is posted per
  PR head even then.
- Two workers checking out the same commit each fetch into a private temp directory and the
  first one to finish is used; the other's copy is discarded.
- SQLite works for several workers on one machine, not across machines.

Watch `queue_backlog{state="queued"}`, `oldest_queued_age_seconds` and `backlog_alarm`. If the
alarm stays at 1, add workers or look at `jobs_failed_total` for the cause. Jobs that keep
failing end in `dead`; after fixing the cause, requeue them with the queue's `requeue(job_id)`
(there is no CLI command for this yet).

## Metrics

`GET /metrics` (Prometheus text format):

| Metric | Type | Meaning |
|---|---|---|
| `reviews_total{outcome}` | counter | Finished reviews: posted, shadow, skipped, superseded, head-moved, already-posted, post-uncertain, lease-lost |
| `review_stage_seconds_sum/_count{stage}` | summary | Time in each pipeline stage (fetch, diff, index, context, model, verify, scan, post) |
| `review_cost_usd_total` | counter | Model cost reported by the provider |
| `jobs_failed_total{error}` | counter | Failed attempts, by exception type |
| `queue_backlog{state}` | gauge | Jobs per state |
| `oldest_queued_age_seconds` | gauge | Age of the oldest queued job |
| `queue_stuck_jobs`, `backlog_alarm` | gauge | Queued jobs older than `--backlog-alarm-s`, and 1 if any |

Counters are stored in the database, so `serve` reports what every worker recorded.

## Proof runs in Docker

`proof.runner: docker` (see "Proof by failing test" in the README) needs, on every worker:

1. The proof image, built locally. Reviewhound runs it with `--pull never`, so it is never
   fetched from a registry:

   ```bash
   docker build -f Dockerfile.proof -t reviewhound-proof:latest .
   ```

2. The `docker` CLI and access to a Docker daemon. Access to the Docker socket is root on that
   host: give it only to the worker, never to `serve`, and prefer a dedicated host or a
   rootless daemon. The shipped worker image has no Docker CLI and no socket; when proofs are
   off nothing is needed.

3. When the worker itself runs in a container, the daemon resolves bind-mount paths on the
   host. Reviewhound writes each tree under the worker's temp dir and mounts it read-only, so
   that directory must exist at the same path on the host and in the worker: for example mount
   `/var/lib/reviewhound-proof:/var/lib/reviewhound-proof` and set
   `TMPDIR=/var/lib/reviewhound-proof` for the worker. Otherwise the container sees an empty
   `/src` and every proof is inconclusive.

Each run: `docker run -d` as user 65534 with `--network none --read-only --cap-drop ALL
--security-opt no-new-privileges`, memory, CPU and pids limits, the tree as a read-only bind
mount, and `/tmp` and `/out` as size-limited tmpfs inside the container. The test runs via
`docker exec`; the report and output are read back through `docker exec` (regular files only,
size-capped), then `docker rm -f`. Nothing is written to a host path by the container. A
container that cannot be removed logs `could not remove proof container` and stops itself when
its `sleep` ends.

## Backups

- Postgres: `pg_dump --format=custom "$REVIEWHOUND_DATABASE_URL" > reviewhound.dump` on a
  schedule; restore with `pg_restore --clean --dbname ...`. The data that matters is the
  `findings` and `feedback` tables (developer reactions); `jobs` and `metric_counters` can be
  lost without harm beyond history.
- SQLite: `sqlite3 ~/.reviewhound/reviewhound.db ".backup reviewhound.bak"` (safe while running;
  do not copy the file directly while WAL is active). Same for `queue.db`.
- The `checkouts/` directory is a cache. Do not back it up.

## Upgrading

1. Read the release notes for schema changes.
2. Back up the database.
3. Build the new image, then restart `serve` and the workers. Tables and indexes are created
   with `IF NOT EXISTS`, so starting a new version on an old database is safe; a column added
   by a later version will be listed with its migration in the release notes.
4. Running jobs are not lost: a worker stopped mid-review leaves its lease to expire and the
   job is retried. To avoid the wait, stop workers between jobs.
5. Checkouts left half-written by versions before M6 (a directory without
   `.reviewhound-complete`) are replaced automatically.
6. Check `/healthz`, then `/metrics` for a rising `jobs_failed_total`.
