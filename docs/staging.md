# Staging cluster (k3s)

How the **staging** cluster is built, so it can be rebuilt from scratch.

Staging is a single-node [k3s](https://docs.k3s.io/) cluster on an arm64 Raspberry Pi.
k3s is a small, certified Kubernetes distribution packaged as one binary. It includes
what a small cluster needs: containerd (runs containers), Flannel (pod networking),
CoreDNS, Traefik (ingress: routes HTTP traffic to apps), a local-path storage provisioner
and a metrics server.

Machine-specific values (hostname, IP) are in `CLAUDE.local.md`, which isn't committed.
Below, `<staging-host>` means the Pi's hostname and `<staging-user>` your login on it.

## Versions

| Component | Version | Why this one |
|---|---|---|
| k3s | `v1.37.1+k3s1` | Current stable minor (1.37); the newer v1.37.2 was a day old and had no security fixes |
| kubectl (on the dev machine) | `v1.37.1` | Same minor as the cluster. kubectl supports one minor version either side |
| Prometheus Helm chart | `29.39.0` (Prometheus v3.15.1) | Newest chart at least 7 days old (new ones ship every day or two) |
| Grafana Helm chart | `14.2.0` (Grafana 13.3.0), grafana-community repo | Newest chart at least 7 days old. The old `grafana/helm-charts` copy is deprecated |

To upgrade, change the version here first (in a PR), then rerun the install with the new
version. It upgrades in place.

## 1. Prerequisites (on the Pi)

Already true on a default 64-bit Raspberry Pi OS (Debian 13 "trixie") install. Check:

```bash
uname -m                                   # aarch64 (64-bit)
cat /boot/firmware/cmdline.txt             # must contain: cgroup_memory=1 cgroup_enable=memory
grep -w memory /sys/fs/cgroup/cgroup.controllers   # prints a line = memory cgroup available
```

If the `cgroup_*` options are missing, add them to the **end of the single line** in
`/boot/firmware/cmdline.txt` and reboot. Kubernetes uses memory cgroups to enforce pod
memory limits; without them k3s won't start.

Swap can stay on: k3s runs the kubelet with `failSwapOn: false`, and pods don't use swap.

## 2. Configure k3s (on the Pi)

k3s reads its settings from `/etc/rancher/k3s/config.yaml`. A file is easier to review
and keep than a long list of command-line flags.

```bash
sudo mkdir -p /etc/rancher/k3s
sudo tee /etc/rancher/k3s/config.yaml >/dev/null <<'EOF'
# Extra names the API server's TLS certificate is valid for, so kubectl on the
# dev machine can connect by hostname. Add every name you'll use to reach the Pi.
tls-san:
  - <staging-host>
  - <staging-host>.lan
  - <staging-host>.local
# The kubeconfig k3s writes is a cluster-admin credential: root-only (0600).
write-kubeconfig-mode: "0600"
# Pod network (Flannel) uses the wired card. k3s picks the node IP from the
# interface with the default route when it starts, which is also eth0.
flannel-iface: eth0
EOF
```

Use **Ethernet** (`eth0`), with Wi-Fi off: `sudo nmcli radio wifi off`. Two connections
on the same network can send traffic out of the wrong one, and k3s records the node IP
from whichever card had the default route when it started.

No fixed IP is configured. If the Pi's address changes, run `sudo systemctl restart k3s`
(or reboot) so k3s picks up the new one. Changes to `tls-san` also need a restart, which
reissues the API certificate.

## 3. Install k3s (on the Pi)

We use the official install script, **pinned to the release tag** rather than the moving
`get.k3s.io` copy. We also download it and read it before running it. The script fetches
the k3s binary for this version, checks it against the published SHA-256 checksum,
and installs a `k3s` systemd service.

```bash
K3S_VERSION='v1.37.1+k3s1'
curl -sfL -o /tmp/k3s-install.sh \
  "https://raw.githubusercontent.com/k3s-io/k3s/${K3S_VERSION}/install.sh"
less /tmp/k3s-install.sh                   # read it before running it
sudo INSTALL_K3S_VERSION="$K3S_VERSION" sh /tmp/k3s-install.sh
rm /tmp/k3s-install.sh
```

Check it (allow a minute or two on a Pi):

```bash
sudo systemctl status k3s --no-pager       # active (running)
sudo k3s kubectl get nodes                 # STATUS: Ready
sudo k3s kubectl get pods -A               # all Running or Completed
```

## 4. kubectl on the dev machine

Install `kubectl` (pinned) and verify its checksum. `~/.local/bin` is on `PATH` on Ubuntu.

```bash
KUBECTL_VERSION='v1.37.1'
cd /tmp
curl -fLO "https://dl.k8s.io/release/${KUBECTL_VERSION}/bin/linux/amd64/kubectl"
curl -fLO "https://dl.k8s.io/release/${KUBECTL_VERSION}/bin/linux/amd64/kubectl.sha256"
echo "$(cat kubectl.sha256)  kubectl" | sha256sum --check    # must say: kubectl: OK
install -m 0755 kubectl ~/.local/bin/kubectl
rm kubectl kubectl.sha256
kubectl version --client
```

## 5. Cluster access from the dev machine (kubeconfig)

The kubeconfig contains a **cluster-admin client certificate and key**. Treat it like
a root password:

- Never commit it, paste it into chat or tickets, or print it to the terminal.
- Keep it at mode `0600` (only you can read it).

Copy it from the Pi without displaying it, then point it at the Pi instead of `127.0.0.1`.
The file is root-only, so first make a private copy you can read (`sudo` asks for your
password; `-t` gives it a terminal to ask on), copy that across with `scp`, then delete it:

```bash
PI='<staging-user>@<staging-host>'
mkdir -p ~/.kube && chmod 700 ~/.kube
ssh -t "$PI" 'sudo install -m 0600 -o "$USER" /etc/rancher/k3s/k3s.yaml ~/k3s.yaml'
scp "$PI:k3s.yaml" ~/.kube/config && ssh "$PI" 'rm ~/k3s.yaml'
chmod 600 ~/.kube/config
sed -i 's#https://127.0.0.1:6443#https://<staging-host>.lan:6443#' ~/.kube/config
kubectl get nodes                          # the Pi, STATUS: Ready
```

## 6. Deploying the app

**Argo CD deploys the app** (section 7): the `runbook-staging` Application keeps the
`runbook` namespace matching `deploy/staging/` on `main`. To change staging, change those
files **in a PR and merge it**. Argo CD notices within about 3 minutes and applies it.
Don't `kubectl apply` or `kubectl edit` the app by hand: Argo CD's self-heal puts it back.

To roll out a new version of the app, change `newTag` in `deploy/staging/kustomization.yaml`
to the new `main` commit's `sha-<full SHA>`. That one tag is used by both the app and the
migration Job.

**Each sync runs in order** (Argo CD *sync waves*, waiting for each to be healthy):

| Wave | What |
|---|---|
| -2 | Namespace |
| -1 | Postgres password (SealedSecret), Postgres |
| 0 | Migration Job: `alembic upgrade head` with the new image (`08-migrate-job.yaml`) |
| 1 | The app |

If the migration fails, the sync stops there and the **old app version keeps running**.
The last migration Job is kept, so you can read its output:

```bash
kubectl -n argocd get application runbook-staging   # SYNC STATUS: Synced, HEALTH: Healthy
kubectl -n runbook logs job/runbook-migrate          # the last migration run
kubectl -n runbook rollout status deploy/runbook
curl http://<staging-host>.local/ready              # {"status":"ready"}
```

Bootstrap only (a brand-new cluster before Argo CD is installed): `kubectl apply -k
deploy/staging`. Applied by hand, the waves and hook are ignored, so if the migration Job
fails because Postgres isn't up yet, delete the Job and apply again.

## 7. Argo CD

[Argo CD](https://argo-cd.readthedocs.io/) watches this Git repository and keeps the
cluster matching what's in it (GitOps). Its install lives in `deploy/argocd/`: the
official manifest pinned by commit SHA, minus Dex (SSO), notifications and
ApplicationSets, which we don't use. That leaves four workloads: `argocd-server` (UI and
API), `argocd-repo-server` (reads Git and renders manifests), `argocd-application-controller`
(compares and syncs) and `argocd-redis` (cache).

Install or upgrade, from the dev machine:

```bash
kubectl apply --server-side -k deploy/argocd
kubectl -n argocd rollout status deploy/argocd-server --timeout=600s
kubectl -n argocd get pods                          # 4 pods, all Running
```

`--server-side` is required: Argo CD's CRDs are too big for the annotation plain
`kubectl apply` stores.

The same command also creates the **`runbook-staging` Application**
(`deploy/argocd/application-runbook-staging.yaml`), which tells Argo CD what to deploy.
Changes to `deploy/argocd/` itself are still applied by hand with that command. Argo CD
doesn't manage its own install (yet).

**Opening the UI.** It's not exposed on the LAN; reach it through a port-forward, a
temporary tunnel from your machine into the cluster:

```bash
kubectl -n argocd port-forward svc/argocd-server 8080:443
# then browse to https://localhost:8080 (self-signed certificate warning: expected)
```

**First login.** User `admin`. The initial password is generated at install time and
stored in a Secret. Show it in **your own terminal only** (not in chat, tickets or logs):

```bash
kubectl -n argocd get secret argocd-initial-admin-secret -o jsonpath='{.data.password}' | base64 -d; echo
```

Then change it in the UI (**User Info → Update Password**), store it in your password
manager, and delete the initial Secret so the old password isn't left lying around:

```bash
kubectl -n argocd delete secret argocd-initial-admin-secret
```

## 8. Sealed Secrets

[Sealed Secrets](https://github.com/bitnami-labs/sealed-secrets) lets Kubernetes Secrets
live in Git, **encrypted**. You encrypt ("seal") a Secret on your machine with `kubeseal`,
commit the resulting `SealedSecret`, and the controller in the cluster decrypts it into a
normal Secret. Only this cluster's controller holds the private key, so a SealedSecret in
a public repo is safe to publish.

The controller (`v0.40.0`, vendored in `deploy/sealed-secrets/`) is deployed by Argo CD
(the `sealed-secrets` Application, created by the `kubectl apply -k deploy/argocd`
command in section 7).

### kubeseal on the dev machine

```bash
KUBESEAL_VERSION='0.40.0'
cd /tmp
base="https://github.com/bitnami-labs/sealed-secrets/releases/download/v${KUBESEAL_VERSION}"
curl -fLO "$base/kubeseal-${KUBESEAL_VERSION}-linux-amd64.tar.gz"
curl -fLO "$base/sealed-secrets_${KUBESEAL_VERSION}_checksums.txt"
sha256sum --check --ignore-missing "sealed-secrets_${KUBESEAL_VERSION}_checksums.txt"   # must say: OK
tar -xzf "kubeseal-${KUBESEAL_VERSION}-linux-amd64.tar.gz" kubeseal
install -m 0755 kubeseal ~/.local/bin/kubeseal
rm kubeseal "kubeseal-${KUBESEAL_VERSION}-linux-amd64.tar.gz" "sealed-secrets_${KUBESEAL_VERSION}_checksums.txt"
kubeseal --version
```

### Sealing a secret

Build the Secret locally with `--dry-run=client` (nothing is sent to the cluster), pipe it
straight into `kubeseal`, and only the encrypted output touches the disk:

```bash
kubectl create secret generic <name> -n <namespace> \
  --from-literal=<key>="$(openssl rand -base64 32)" \
  --dry-run=client -o yaml \
  | kubeseal --format yaml > deploy/staging/<file>.yaml
```

A SealedSecret is tied to its **name and namespace**: it can't be renamed or moved to
another namespace without sealing it again.

### Backing up the sealing key (do this!)

If the cluster is rebuilt, a new controller makes a new key and **can't decrypt anything
already in Git**. Keep a copy of the private key **outside the cluster and outside Git**,
for example as an attachment in your password manager:

```bash
kubectl -n kube-system get secret -l sealedsecrets.bitnami.com/sealed-secrets-key -o yaml \
  > sealed-secrets-key.yaml
# store sealed-secrets-key.yaml in your password manager, then:
shred -u sealed-secrets-key.yaml
```

The controller adds a new key every 30 days (older keys are kept, so old secrets still
decrypt). New SealedSecrets use the newest key, so **repeat the backup after a renewal**.

**Restoring** onto a rebuilt cluster: before Argo CD syncs the SealedSecrets, apply the
backup, then restart the controller so it loads the key:

```bash
kubectl apply -f sealed-secrets-key.yaml
kubectl -n kube-system rollout restart deploy/sealed-secrets-controller
```

## 9. PostgreSQL

Postgres runs in the `runbook` namespace as a StatefulSet (`deploy/staging/06-postgres.yaml`),
with the same `pgvector` image as Compose and CI. It runs as user 999 with a read-only
filesystem, to meet the namespace's "restricted" Pod Security policy.

- **Data:** the PersistentVolumeClaim `data-postgres-0`, a `local-path` folder on the Pi's
  disk (the **SD card**, for now). It survives pod restarts and deleting the StatefulSet.
  Deleting the PVC deletes the data.
- **Password:** the SealedSecret `runbook-db` (`deploy/staging/05-db-secret.sealed.yaml`).
  The app and Postgres both read it from the Secret it creates.
- **The app** connects to the Service `postgres`, and its readiness probe is `/ready`, so it
  only gets traffic while the database is reachable.

Open a `psql` shell (it runs inside the pod, so no password or port-forward is needed):

```bash
kubectl -n runbook exec -it postgres-0 -- psql -U runbook -d runbook
```

**Creating a user** (runs inside the app's pod, so it uses the app's database settings;
`-it` gives it a terminal for the password prompt):

```bash
kubectl -n runbook exec -it deploy/runbook -- \
  python -m app.cli create-user --email you@example.com --name "Your Name"
```

**Rotating the password:** seal a new value over `05-db-secret.sealed.yaml` (section 8) and
merge it. Postgres only reads `POSTGRES_PASSWORD` when it first creates the database, so
also change it inside Postgres (`ALTER USER runbook PASSWORD '...'`) before restarting
the app.

## 10. Monitoring (Prometheus)

Prometheus stores metrics over time. It runs in the `monitoring` namespace, deployed by the
`prometheus` Argo CD Application (`deploy/argocd/application-prometheus.yaml`) from the
official Helm chart, with our settings in `deploy/monitoring/prometheus-values.yaml`.

- **Scraped every 30 s:** the app's pods (annotated `prometheus.io/scrape`, port 9000),
  node-exporter (the Pi's CPU, memory, disk, temperature), per-container CPU/memory, and
  Prometheus itself. Kept for 7 days or 1.5 GB, whichever comes first.
- **Not exposed** outside the cluster. To look at it, forward a port and open
  http://127.0.0.1:9090 (Status → Targets shows what's scraped and whether it's up):

  ```bash
  kubectl -n monitoring port-forward svc/prometheus-server 9090:80
  ```

- **Change a setting:** edit the values file in a PR; Argo CD applies it after merge.
- **Upgrade the chart:** change `targetRevision` in the Application, then
  `kubectl apply --server-side -k deploy/argocd` (that folder isn't synced by Argo CD itself).

## 11. Dashboards (Grafana)

Grafana draws graphs from Prometheus. It runs in the `monitoring` namespace, deployed by the
`grafana` Argo CD Application (`deploy/argocd/application-grafana.yaml`) from the
grafana-community Helm chart. Everything is defined in Git, nothing by clicking:

- Settings: `deploy/monitoring/grafana-values.yaml` (including the Prometheus data source).
- Dashboards: JSON files in `deploy/monitoring/grafana/dashboards/` (**Runbook**: the app and
  Ask; **Staging Pi**: CPU, memory, temperature, disk, per-pod use).
- Admin login: user `admin`, password in the SealedSecret
  `deploy/monitoring/grafana/admin-secret.sealed.yaml`. To change it, seal a new one the same
  way as §8 (`kubectl create secret generic grafana-admin -n monitoring
  --from-literal=admin-user=admin --from-file=admin-password=/dev/stdin --dry-run=client -o yaml
  | kubeseal --format yaml`, typing the password on stdin), keep the file's
  `argocd.argoproj.io/sync-wave: "-1"` annotation, commit, and restart Grafana.

Grafana keeps no data of its own (no volume), so a restart brings it back exactly as Git
describes. **Not exposed** outside the cluster. To open it, forward a port and browse to
http://127.0.0.1:3000 (from a laptop: `ssh -L 3000:127.0.0.1:3000 <user>@<dev-machine>` first):

```bash
kubectl -n monitoring port-forward svc/grafana 3000:80
```

**Changing a dashboard:** edit it in Grafana (it won't let you save over the file), then
Share → Export → Save to file, replace the JSON file in a PR. Argo CD applies it after merge.

## Names: `.local` vs `.lan`

- **`<staging-host>.local`** is announced by the Pi itself over **mDNS** (Avahi, on by
  default in Raspberry Pi OS), so it always matches the Pi's current address. Use it in
  the **browser**: `http://<staging-host>.local/`. On an Ubuntu server install, resolving
  `.local` names needs `sudo apt install libnss-mdns`; desktops and Macs have it built in.
- **`<staging-host>.lan`** comes from the router's DNS, which can lag behind address
  changes (it kept the old Wi-Fi address after the move to Ethernet). The **kubeconfig**
  uses this name because **kubectl can't resolve `.local`**: it's a Go program built
  without the system's name lookup, so it asks DNS directly and never reaches mDNS.
  The router's IPv6 record for the Pi stayed correct, which kept kubectl working.

## Notes

- **Port 6443** (the Kubernetes API) is reachable from the LAN. It needs the client
  certificate in the kubeconfig, but keep the cluster off untrusted networks.
- **Storage:** the Pi boots from an SD card. Before Postgres arrives (Phase 3a), move
  database storage to a USB SSD, because database writes wear SD cards out.
- **Uninstall:** `sudo /usr/local/bin/k3s-uninstall.sh` removes k3s and everything
  running in the cluster.
