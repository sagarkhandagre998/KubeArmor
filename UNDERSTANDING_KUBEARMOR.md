# Understanding KubeArmor — A Contributor's Guide

> A from-scratch walkthrough of what KubeArmor is, how it is built, and where to
> look in the code so you can start contributing with confidence.

---

## 1. What is KubeArmor?

**KubeArmor is a cloud-native runtime security enforcement system.** It restricts
what pods, containers, and nodes (VMs/bare-metal) are *allowed to do* at the
operating-system level — which processes can execute, which files can be
read/written, which network operations are permitted, and which Linux
capabilities can be used.

The key idea: instead of only *detecting* bad behavior after it happens,
KubeArmor can **block it inline at the kernel** before it takes effect.

It does this by leveraging two Linux kernel technologies:

| Technology | Used for |
|---|---|
| **LSMs** (AppArmor, SELinux, BPF-LSM) | **Enforcement** — actually blocking disallowed actions |
| **eBPF** | **Observability** — generating rich alerts/telemetry tagged with container/pod/namespace identity |

KubeArmor is a **CNCF Sandbox project** (Apache-2.0 licensed).

### The mental model

```
You write a policy (YAML)  ──►  KubeArmor compiles it into LSM rules  ──►
Kernel enforces it on every syscall  ──►  Violations become alerts (gRPC/log)
```

### Three things KubeArmor protects

1. **Pods/Containers** → `KubeArmorPolicy` (KSP)
2. **Hosts/Nodes (VMs, bare-metal)** → `KubeArmorHostPolicy` (HSP)
3. **Network behavior** → `KubeArmorNetworkPolicy` & cluster-wide variants

---

## 2. The Big Picture Architecture

KubeArmor runs as a **DaemonSet** in Kubernetes (one instance per node), plus
optional control-plane components. Conceptually it has three layers:

```
┌─────────────────────────────────────────────────────────────────────┐
│  CONTROL PLANE (Kubernetes only)                                      │
│  ┌─────────────────────┐         ┌──────────────────────┐            │
│  │ KubeArmorOperator   │ deploys │ KubeArmorController  │            │
│  │ (detects LSM/runtime│────────►│ (CRD webhooks,       │            │
│  │  installs DaemonSet)│         │  pod annotations)    │            │
│  └─────────────────────┘         └──────────────────────┘            │
└─────────────────────────────────────────────────────────────────────┘
                              │ applies policies (CRDs)
                              ▼
┌─────────────────────────────────────────────────────────────────────┐
│  KUBEARMOR DAEMON  (one per node — the heart of the project)         │
│                                                                       │
│   core/  ──────────►  watches K8s API + container runtimes           │
│     │                  (Docker / containerd / CRI-O / NRI)            │
│     │                                                                 │
│     ├──► enforcer/  ─► translates policy → LSM rules                  │
│     │       ├─ AppArmor                                               │
│     │       ├─ SELinux                                                │
│     │       └─ BPF-LSM (modern default)                              │
│     │                                                                 │
│     ├──► monitor/  ─► loads eBPF programs, reads kernel events        │
│     │                  via ring buffer                                │
│     │                                                                 │
│     └──► feeder/  ──► matches events to policies, produces            │
│                        Alerts + Logs, serves them over gRPC           │
└─────────────────────────────────────────────────────────────────────┘
                              │ kernel hooks (LSM) + tracepoints (eBPF)
                              ▼
┌─────────────────────────────────────────────────────────────────────┐
│  LINUX KERNEL  — enforces rules & emits events on every syscall      │
└─────────────────────────────────────────────────────────────────────┘
```

---

## 3. Repository Layout

The repo is a **multi-module Go project**. Each top-level area below has its own
`go.mod`.

| Path | Module | What lives here |
|---|---|---|
| `KubeArmor/` | the daemon | **The core runtime engine — start here.** |
| `pkg/KubeArmorController/` | controller | K8s admission webhooks + CRD handling |
| `pkg/KubeArmorOperator/` | operator | Auto-detects environment, installs/manages KubeArmor |
| `protobuf/` | protobuf | gRPC service & message definitions (`.proto` + generated Go) |
| `deployments/` | deployments | Go code that *generates* environment-specific YAML manifests |
| `tests/` | tests | Ginkgo integration tests (k8s & non-k8s) |
| `examples/` | — | Sample apps + policies (nginx, wordpress, sock-shop…) |
| `getting-started/` | — | User-facing docs (policy specs, deployment guides) |
| `wiki/` | — | Architecture deep-dives |
| `contribution/` | — | **Contribution, development & testing guides** |
| `.github/workflows/` | — | CI pipelines |

### Inside the daemon (`KubeArmor/`)

| Package | Responsibility |
|---|---|
| `main.go` | Entrypoint: checks root, cleans stale BPF maps, loads config, calls `core.KubeArmor()` |
| `core/` | **Orchestrator** — container lifecycle, K8s/runtime watchers, policy routing |
| `enforcer/` | LSM selection + AppArmor / SELinux enforcement |
| `enforcer/bpflsm/` | BPF-LSM enforcement (translates policy → kernel maps) |
| `BPF/` | The actual eBPF **C source** (`*.bpf.c`, `system_monitor.c`, `shared.h`) |
| `monitor/` | Loads eBPF programs, reads the ring buffer, decodes syscalls |
| `feeder/` | Matches events to policies → produces Alerts/Logs, gRPC server |
| `policy/` | gRPC policy service (for non-K8s / unorchestrated environments) |
| `types/` | All data structures (policy specs, alerts, container metadata) |
| `config/` | Daemon configuration loading |
| `common/`, `log/`, `cert/`, `state/`, `presets/` | Shared utilities, logging, TLS, state, built-in policy presets |

---

## 4. How the Daemon Starts (Code Walkthrough)

`KubeArmor/main.go` is tiny and worth reading first:

1. **Requires root** (`os.Geteuid() != 0`) — kernel hooks need privilege.
2. **Cleans stale BPF maps** under `/sys/fs/bpf/kubearmor*` (leftovers from a
   crashed previous run).
3. **`cfg.LoadConfig()`** — reads configuration (flags / env / file).
4. **`core.KubeArmor()`** — hands off to the orchestrator in
   `KubeArmor/core/kubeArmor.go`, which builds the central `KubeArmorDaemon`
   struct and wires together the Feeder, SystemMonitor, RuntimeEnforcer, etc.

> 💡 **Contributor tip:** `core/kubeArmor.go` is your map of the whole daemon —
> every subsystem is a field on the `KubeArmorDaemon` struct. Read it once and
> the rest of the codebase makes sense.

---

## 5. The Core Subsystems in Detail

### 5.1 `core/` — The Orchestrator

The brain that connects Kubernetes/runtimes to enforcement.

- **`kubeArmor.go`** — defines `KubeArmorDaemon`, holds containers, policies, and
  references to every other subsystem.
- **`kubeUpdate.go`** (~3000 lines) — the policy pipeline. Watches the K8s API
  (Nodes, Pods, Policy CRDs) via *informers*, performs **selector matching**
  (`matchLabels` / `matchExpressions`) to find which containers a policy applies
  to, then routes updates to the enforcer and feeder.
- **Runtime handlers** — `dockerHandler.go`, `containerdHandler.go`,
  `crioHandler.go`, `nriHandler.go`, `k8sHandler.go`. These detect container
  start/stop across the supported runtimes so KubeArmor knows the
  `{PID namespace, Mount namespace}` of every container.
- **`unorchestratedUpdates.go`** — handles plain Docker / VM environments (no K8s).

### 5.2 `enforcer/` — Turning Policy into Kernel Rules

`runtimeEnforcer.go` **auto-selects** the enforcement backend by reading
`/sys/kernel/security/lsm` and probing BPF-LSM support. Priority is roughly
**BPF-LSM → AppArmor → SELinux** (configurable).

| Backend | File | How it enforces |
|---|---|---|
| **AppArmor** | `appArmorEnforcer.go` | Generates a profile, loads it with `apparmor_parser -r` |
| **SELinux** | `SELinuxEnforcer.go` | Generates a module via `semanage`/`semodule` (host-only) |
| **BPF-LSM** | `bpflsm/enforcer.go` | Inserts rules into eBPF kernel maps (modern default) |

**BPF-LSM is the most important to understand:**
- Loads pre-compiled eBPF objects (`enforcer_bpfel.o` / `_bpfeb.o` for
  little/big-endian) using the `cilium/ebpf` library.
- Attaches to LSM hooks: `bprm_check_security` (exec), `file_open`,
  `socket_create`, `capable`.
- Maintains kernel maps pinned at `/sys/fs/bpf/kubearmor_*`:
  - **`kubearmor_containers`** — a *hash-of-maps*: outer key `{PidNS, MntNS}`
    identifies a container; the inner map holds that container's rules.
  - **`kubearmor_alert_throttle`** — per-container alert rate-limiting state.
  - **`kubearmor_arguments`** — argument-matching rules.
- **`bpflsm/rulesHandling.go`** converts policy rules into an `InnerKey`
  (path + source) plus **bit flags**: `EXEC, WRITE, READ, OWNER, DIR,
  RECURSIVE, DENY, ARGSET, PTS`.

### 5.3 `BPF/` + `monitor/` — eBPF Observability

The eBPF C programs live in `KubeArmor/BPF/`:
- **`system_monitor.c`** — attaches to syscall tracepoints, reads the task
  struct (PID, UID, exe path, cwd), identifies the container by namespace, and
  writes events into a **ring buffer**.
- **`enforcer.bpf.c`** — the enforcement program (LSM hooks → map lookup →
  allow/deny).
- **`shared.h`** — shared structs + CO-RE (Compile Once Run Everywhere) helpers
  for cross-kernel compatibility.

User-space side (`KubeArmor/monitor/`):
- **`systemMonitor.go`** — loads the eBPF objects, creates the ring-buffer
  reader, and manages the `kubearmor_visibility` and `kubearmor_config` maps.
  It also keeps `NsMap`: `{PidNS, MntNS} → container ID`.
- **`syscallParser.go`** — decodes raw ring-buffer samples, extracting syscall
  arguments from CPU registers (architecture-specific: `syscalls_amd64.go`,
  `syscalls_arm64.go`).
- **`logUpdate.go`** — forwards decoded events to the feeder.

> 🔑 **Key concept — namespace identity:** A container is uniquely identified by
> its `{PID namespace inode, Mount namespace inode}`. This pair is the join key
> between the kernel (eBPF maps) and user-space (container metadata). Get
> comfortable with it — it appears everywhere.

### 5.4 `feeder/` — Alerts & Telemetry

- **`feeder.go`** — receives events from the monitor, fans them out to file
  output (stdout/log) and to gRPC streams; caches policies for matching.
- **`policyMatcher.go`** — the matching algorithm. For each event it matches
  `{source, operation, resource, action}` against cached policies and applies
  **default posture** logic. Produces either an **Alert** (violation) or a
  **Log** (visibility/audit).
- **`logServer.go`** — implements the gRPC `LogService`.

The gRPC contract (`protobuf/kubearmor.proto`):
```protobuf
service LogService {
  rpc WatchMessages(RequestMessage) returns (stream Message);  // operational
  rpc WatchAlerts(RequestMessage)   returns (stream Alert);    // policy violations
  rpc WatchLogs(RequestMessage)     returns (stream Log);      // all visibility
  rpc HealthCheck(NonceMessage)     returns (ReplyMessage);
}
```
External consumers (e.g. `kubearmor-relay`, kubearmor CLI `karmor`) subscribe to
these streams.

---

## 6. The End-to-End Flow (Follow One Request)

**Scenario:** Block writes to `/etc/passwd` in nginx pods.

```yaml
apiVersion: security.kubearmor.com/v1
kind: KubeArmorPolicy
metadata:
  name: block-passwd
spec:
  selector:
    matchLabels: { app: nginx }
  file:
    matchPaths:
      - path: /etc/passwd
  action: Block
```

1. **Apply** — `kubectl apply` creates the CRD. The controller validates it; an
   informer in `core/kubeUpdate.go` fires.
2. **Match** — `UpdateSecurityPolicies()` runs selector matching → finds all
   `app=nginx` containers.
3. **Compile & enforce** — `RuntimeEnforcer.UpdateSecurityPolicies(endpoint)`:
   - BPF-LSM: build `InnerKey{path:"/etc/passwd"}` with flags `WRITE|DENY`,
     insert into `kubearmor_containers[{pidns,mntns}]`.
4. **Cache for logging** — the feeder caches the policy for event matching.
5. **Container registration** — when an nginx container starts, a runtime handler
   calls `RegisterContainer(id, pidns, mntns)`, creating the outer map key.
6. **Runtime enforcement** — nginx tries `open("/etc/passwd", O_WRONLY)`:
   - Kernel `file_open` LSM hook → eBPF program → looks up `{pidns,mntns}` →
     finds the rule → returns `-EPERM`. **The write fails inline.**
7. **Alert** — the eBPF program also emits a ring-buffer event → monitor decodes
   it → feeder matches it to `block-passwd` → emits an **Alert**
   (`action: Block, result: Denied`).
8. **Delivery** — Alert is written to the log file and streamed to all
   `WatchAlerts()` gRPC clients.

---

## 7. Policy Model Cheat-Sheet

All policy structs are defined in **`KubeArmor/types/types.go`**.

| Rule category | Key fields |
|---|---|
| **Process** | `matchPaths`, `matchDirectories`, `matchPatterns` + `fromSource`, `ownerOnly`, `recursive` |
| **File** | same as Process, plus `readOnly` |
| **Network** | `matchProtocols` (TCP/UDP/ICMP…), `matchDNSQueries` |
| **Capabilities** | `matchCapabilities` (e.g. `CAP_NET_RAW`) |
| **Syscalls** | `matchSyscalls`, `matchPaths` (monitoring-oriented) |
| **Presets** | curated rules: `filelessExec`, `anonymousMapExec`, `protectProc`, `protectEnv` |

**Action** is `Allow` (whitelist) or `Block` (blacklist).

**Default Posture** decides what happens when *no rule matches*:
- `audit` → allow but log
- `block` → deny by default (zero-trust whitelisting)

Three scopes:
- **`KubeArmorPolicy`** — selects pods via `selector.matchLabels`.
- **`KubeArmorHostPolicy`** — selects nodes via `nodeSelector`; can also gate
  devices (USB etc.).
- **`KubeArmorNetworkPolicy`** / cluster variants — ingress/egress, CIDR blocks,
  ports, interfaces.

---

## 8. Control Plane (Kubernetes Components)

| Component | Path | Job |
|---|---|---|
| **KubeArmorOperator** | `pkg/KubeArmorOperator/` | Detects each node's LSM, container runtime, and seccomp support, then installs and configures the right KubeArmor DaemonSet. Manages `KubeArmorConfig`. |
| **KubeArmorController** | `pkg/KubeArmorController/` | Admission webhooks that validate policy CRDs and annotate pods (e.g. for AppArmor). |

If you contribute to deployment/lifecycle logic, these two modules are your area.
The generated install manifests come from `deployments/`.

---

## 9. How to Start Contributing

### Recommended reading order
1. `README.md` (project framing)
2. `wiki/kubearmor_overview.md` (architecture)
3. `contribution/contribution_guide.md` + `contribution/development_guide.md`
4. `contribution/testing_guide.md`
5. Then trace code: `main.go` → `core/kubeArmor.go` → `core/kubeUpdate.go`

### Set up a dev environment
- Use a local cluster: `contribution/{k3s,minikube,microk8s,self-managed-k8s}/`
  each has setup notes.
- Prefer a **BPF-LSM-capable kernel** (5.7+) so you exercise the default path.
- Build the daemon via `KubeArmor/Makefile`; build images with the root
  `Dockerfile`.

### Run the tests
- Integration tests: `tests/` (Ginkgo). Mirrors CI in
  `.github/workflows/ci-test-ginkgo.yml`.
- Unit tests: `go test ./...` within a module (see `ci-test-go.yml`).
- Controller/operator tests: `ci-test-controllers.yml`, `ci-test-operator.yaml`.

### Good first areas
- **Docs & examples** — low-risk, high-value (`examples/`, `getting-started/`).
- **Policy presets** — add curated rules in `KubeArmor/presets/`.
- **Runtime handlers** — improve container-runtime integration in `core/`.
- **Tests** — extend `tests/` coverage.
- Look for GitHub issues labeled **good first issue** / **help wanted**.

### Workflow
1. Fork & branch.
2. Make changes; **sign off your commits** (DCO is required: `git commit -s`).
3. Add/adjust tests.
4. Open a PR; ensure CI passes.
- Community: the KubeArmor **Slack** (CNCF) and **biweekly Zoom** call are linked
  in `README.md`.

---

## 10. Glossary

| Term | Meaning |
|---|---|
| **LSM** | Linux Security Module — kernel framework for mandatory access control (AppArmor/SELinux/BPF-LSM) |
| **eBPF** | Safe in-kernel programs; used here for syscall observability |
| **BPF-LSM** | Using eBPF programs attached to LSM hooks for enforcement |
| **CRD** | Kubernetes Custom Resource Definition — how policies are expressed |
| **Ring buffer** | Kernel→user-space channel carrying eBPF events |
| **PidNS / MntNS** | PID & Mount namespace inodes — together identify a container |
| **Default posture** | Behavior when no rule matches (audit vs block) |
| **Feeder** | The subsystem that turns kernel events into Alerts/Logs over gRPC |
| **KSP / HSP** | KubeArmorPolicy (pod) / KubeArmorHostPolicy (node) |

---

## 11. Quick File Index

| I want to understand… | Read |
|---|---|
| Daemon startup | `KubeArmor/main.go`, `KubeArmor/core/kubeArmor.go` |
| Policy → container matching | `KubeArmor/core/kubeUpdate.go` |
| Runtime integration | `KubeArmor/core/{docker,containerd,crio,nri,k8s}Handler.go` |
| AppArmor enforcement | `KubeArmor/enforcer/appArmorEnforcer.go` |
| SELinux enforcement | `KubeArmor/enforcer/SELinuxEnforcer.go` |
| BPF-LSM enforcement | `KubeArmor/enforcer/bpflsm/{enforcer,rulesHandling}.go` |
| eBPF kernel code | `KubeArmor/BPF/{system_monitor.c,enforcer.bpf.c,shared.h}` |
| Syscall monitoring | `KubeArmor/monitor/{systemMonitor,syscallParser}.go` |
| Alerts & gRPC | `KubeArmor/feeder/{feeder,policyMatcher,logServer}.go` |
| Data structures | `KubeArmor/types/types.go` |
| gRPC API | `protobuf/kubearmor.proto` |
| K8s control plane | `pkg/KubeArmorController/`, `pkg/KubeArmorOperator/` |

---

*Happy hacking! Start by reading `core/kubeArmor.go`, then pick a small issue and
trace it end-to-end using the file index above.*
