# Deploying the review page on Kubernetes

This runs `python -m farhang review` for a team of reviewers: one pod, sign-in required, reviews on a persistent volume, served over HTTPS through an Ingress.

```
Ingress (TLS) ──> Service :80 ──> Deployment (1 pod, :8766) ──> PVC  /app/validation   reviews
                                                           └──> Secret /etc/farhang/users  accounts
```

The item data is baked into the image. To change items, edit them in git and roll out a new image. Reviews are tied to each item's content hash, so an edited item simply needs reviewing again.

## 1. Image

The `review-image` workflow (`.github/workflows/review-image.yml`) does this on every push to `main` that touches code or data:
- runs the tests;
- pushes `ghcr.io/<owner>/farhangbench-review:latest` and `:sha-<commit>`;
- also tags `v*` releases with their version.

GHCR packages start private. Either make the package public, or give the cluster an image pull secret.

To build the image yourself:

```bash
docker build -t ghcr.io/<owner>/farhangbench-review:dev .
docker push ghcr.io/<owner>/farhangbench-review:dev
```

## 2. Reviewer accounts

Create one account per reviewer. The name becomes their reviewer id (`validation/<name>.jsonl`), so use the same name they would type locally.

```bash
cp deploy/k8s/users.example deploy/k8s/users        # gitignored; delete the example line
python -m farhang review-user "maryam" >> deploy/k8s/users   # prompts for a password (10+ characters)
python -m farhang review-user "ali"    >> deploy/k8s/users
```

The file holds PBKDF2 password hashes, never passwords. Keep it out of git anyway. Item authors can't validate their own items: set `metadata.author` on an item to the author's reviewer name.

## 3. Configure and apply

Edit `deploy/k8s/kustomization.yaml`:
- **Host:** replace `review.example.com` in the Ingress patch.
- **Image:** set `newName` and `newTag`. Pin a `sha-…` tag for reproducible deploys.
- **Ingress annotations** (`deploy/k8s/ingress.yaml`): set to match your cluster. They assume ingress-nginx and a cert-manager `ClusterIssuer` called `letsencrypt`.
- **Storage:** the PVC uses the default StorageClass. Add `storageClassName` in `deploy/k8s/pvc.yaml` if needed.

```bash
kubectl apply -k deploy/k8s
kubectl -n farhangbench rollout status deploy/farhangbench-review
```

Open `https://<host>/` and sign in. The name field shows the signed-in account and can't be changed.

**Adding or removing a reviewer:** edit `deploy/k8s/users` and apply again. The Secret's name includes a hash of its content, so the pod restarts with the new accounts.

## 4. Getting reviews back into git

Reviews live on the volume until you commit them. Regularly, and before deleting the deployment:

```bash
# Option A: in the page, Progress tab → "Download all reviews (zip)"; unzip at the repo root.
# Option B:
kubectl -n farhangbench cp "$(kubectl -n farhangbench get pod -l app.kubernetes.io/name=farhangbench-review -o name | cut -d/ -f2)":/app/validation ./validation
git add validation && git commit -m "Add reviews"
```

Then `python -m farhang review --summary` and `python -m farhang report` use them like any local reviews.

## Design notes

- **One replica, `Recreate` strategy.** Reviews are appended to files under an in-process lock, on a ReadWriteOnce volume. Don't scale it out.
- **Security.**
  - The pod runs as UID 10001 with a read-only root filesystem and all capabilities dropped. The namespace enforces the `restricted` Pod Security standard.
  - Sign-in is HTTP Basic auth, so serve it over HTTPS only (the Ingress redirects HTTP).
  - The answer key of an item is sent only after the signed-in reviewer has submitted a blind answer to it.
- **Health.** `/healthz` needs no sign-in, and both probes use it.
- **Resources.** The pod uses about 50 MiB and handles a request in about 0.3 s, because it reloads all 6,249 items so that a new image's data is always current. The requests and limits leave plenty of room.
