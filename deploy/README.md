# Deploying the review page on Kubernetes

This runs `python -m farhang review` for a team of reviewers: one pod, sign-in required, reviews on a persistent volume, served over HTTPS through an Ingress.

```
Ingress (TLS) ──> Service :80 ──> Deployment (1 pod, :8766) ──> PVC    /var/lib/farhang   reviews + reviewer accounts
                                                           └──> Secret /etc/farhang/users admin accounts
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

## 2. Accounts

There are two kinds of account:

- **Admins** (usually just you) come from a Kubernetes Secret built from `deploy/k8s/users`:

  ```bash
  cp deploy/k8s/users.example deploy/k8s/users        # gitignored; delete the example line
  python -m farhang review-user "hadi" >> deploy/k8s/users   # prompts for a password (10+ characters)
  ```

  The file holds PBKDF2 password hashes, never passwords. Keep it out of git anyway.
- **Reviewers** are added by an admin in the page's **People** tab, and stored in `accounts.json` on the volume:
  1. Type their name, choose *Reviewer* (or *Admin*, to let them manage people too) and press *Add*.
  2. The page shows a one-time password once. Send it to them privately, with the sign-in name.
  3. When they first sign in, they must choose their own password before they can do anything else.

  The People tab can also reset a forgotten password (a new one-time password), change someone's role, or remove them. Removing an account keeps the reviews it wrote. Reviewers can change their own password with the *Password* button.

A person's sign-in name is also their reviewer id (`validation/<name>.jsonl`). Item authors can't validate their own items: set `metadata.author` on an item to the author's reviewer name.

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

**Adding or removing an admin from the Secret:** edit `deploy/k8s/users` and apply again. The Secret's name includes a hash of its content, so the pod restarts with the new accounts. Reviewers are managed in the People tab, with no redeploy.

## 4. Getting reviews back into git

Reviews live on the volume until you commit them. Regularly, and before deleting the deployment:

```bash
# Option A: in the page, Progress tab → "Download all reviews (zip)"; unzip at the repo root.
# Option B:
kubectl -n farhangbench cp "$(kubectl -n farhangbench get pod -l app.kubernetes.io/name=farhangbench-review -o name | cut -d/ -f2)":/var/lib/farhang/validation ./validation
git add validation && git commit -m "Add reviews"
```

Then `python -m farhang review --summary` and `python -m farhang report` use them like any local reviews.

## Design notes

- **One replica, `Recreate` strategy.** Reviews are appended to files and accounts rewritten atomically under an in-process lock, on a ReadWriteOnce volume. Don't scale it out.
- **Back up the whole volume**, not only the reviews: `accounts.json` holds the reviewer accounts (password hashes only). The zip download from the Progress tab contains reviews only.
- **Security.**
  - The pod runs as UID 10001 with a read-only root filesystem and all capabilities dropped. The namespace enforces the `restricted` Pod Security standard.
  - Sign-in is HTTP Basic auth, so serve it over HTTPS only (the Ingress redirects HTTP).
  - The answer key of an item is sent only after the signed-in reviewer has submitted a blind answer to it.
- **Health.** `/healthz` needs no sign-in, and both probes use it.
- **Resources.** The pod uses about 50 MiB and handles a request in about 0.3 s, because it reloads all 6,999 items so that a new image's data is always current. The requests and limits leave plenty of room.
