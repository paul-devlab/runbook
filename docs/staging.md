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

## 6. Deploy the app (by hand, until Argo CD takes over)

The manifests in `deploy/staging/` are numbered so the Namespace is created first:

```bash
kubectl apply -f deploy/staging/
kubectl -n runbook rollout status deploy/runbook    # first pull on the Pi takes a few minutes
curl http://<staging-host>.local/health             # {"status":"ok"}
```

To roll out a new version, change the image tag in `deploy/staging/10-deployment.yaml`
to the new `main` commit's `sha-<full SHA>` (in a PR), then apply again.

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
