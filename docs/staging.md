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
# The kubeconfig k3s writes is a cluster-admin credential: root-only (0600).
write-kubeconfig-mode: "0600"
EOF
```

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

## Notes

- **Port 6443** (the Kubernetes API) is reachable from the LAN. It needs the client
  certificate in the kubeconfig, but keep the cluster off untrusted networks.
- **Storage:** the Pi boots from an SD card. Before Postgres arrives (Phase 3a), move
  database storage to a USB SSD, because database writes wear SD cards out.
- **Network:** Ethernet is more reliable than Wi-Fi for a cluster node.
- **Uninstall:** `sudo /usr/local/bin/k3s-uninstall.sh` removes k3s and everything
  running in the cluster.
