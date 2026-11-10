# Deploying the review page on Coolify

`python -m farhang review` on a self-hosted [Coolify](https://coolify.io) server: one service, sign-in
required, reviews on a named volume, HTTPS through Coolify's Traefik proxy. The Kubernetes manifests
are the other option (see [README.md](README.md)).

```
Coolify Traefik (TLS, X-Forwarded-Proto/For) ──> review :8766 (compose, 1 replica) ──> farhang-state /var/lib/farhang
```

**One replica, always.** Reviews and accounts are plain files appended/rewritten under an in-process
lock on one volume; never scale the service above 1.

## Steps

1. **Create the resource.** In your Coolify project: *Add Resource → Docker Compose → from this GitHub
   repository* (branch `main`, compose path `/docker-compose.yaml`). Coolify checks the compose file
   out verbatim, so no build happens on the server — the image comes from GHCR.
2. **Set the domain.** On the `review` service, set *Domains* to `https://review.<your-domain>`; Coolify
   fills `SERVICE_FQDN_REVIEW_8766` so Traefik routes it to port 8766 and terminates TLS. (The app sets
   cookies `Secure` on its own via `FARHANG_SECURE_COOKIES=1`, and reads `X-Forwarded-For`/`X-Forwarded-Proto`
   as set by the proxy.)
3. **Set the admin.** Generate an entry locally and put it in the resource's environment as
   `FARHANG_ADMINS`:

   ```bash
   python3 -m farhang review-user hadi --env   # prompts for a password, prints hadi:b64:...
   ```

   Paste the whole `name:b64:...` line. Multiple admins: separate entries with commas or newlines
   (each from `review-user <name> --env`). Never remove the last entry without adding another admin
   first — the server refuses to start with none.
4. **Deploy.** Press *Deploy* (or redeploy). Open `https://review.<your-domain>/` and sign in.
   Reviewers are added in the page's **People** tab, exactly as in
   [README.md](README.md#2-accounts).
5. **Auto-redeploy on green builds.** In Coolify, copy the resource's *Deploy Webhook URL*, and under
   *Keys & Tokens* create an API token with the `deploy` permission. Put them in this GitHub repo's
   secrets as `COOLIFY_WEBHOOK` and `COOLIFY_TOKEN`
   (*Settings → Secrets and variables → Actions*). The `deploy` job in
   `.github/workflows/review-image.yml` then triggers a rebuild-pull-redeploy after every green build
   on `main`.
6. **GHCR access.** The image must be pullable: make the `farhangbench-review` package public on
   GitHub, or add the registry credentials under Coolify's *Private Registries* and pick that registry
   for the service.

## State and backups

Everything the server writes lives in the `farhang-state` named volume at `/var/lib/farhang`:
`accounts.json` (reviewer accounts, password hashes only), `sessions.json`, `audit.jsonl`,
`peeks.json` and `validation/<reviewer>.jsonl`. Back up the whole volume, not only the reviews.

Getting reviews back into git:

- **Option A:** in the page, *Progress → Download all reviews (zip)*; unzip at the repo root and commit.
- **Option B:**

  ```bash
  docker cp "$(docker ps -qf ancestor=ghcr.io/hadifarnoud/farhangbench-review:latest)":/var/lib/farhang/validation ./validation
  git add validation && git commit -m "Add reviews"
  ```

Then `python3 -m farhang review --summary` and `python3 -m farhang report` use them like local reviews.
