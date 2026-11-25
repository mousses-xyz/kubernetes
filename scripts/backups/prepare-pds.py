#!/usr/bin/env python3
"""Prepare the pds data Restic repository, staging volume, and recovery credentials."""

import base64
from decimal import Decimal
import fcntl
import ipaddress
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import stat
import subprocess
import sys
import tempfile
import time


NAMESPACE = "pds"
MANAGER = "nas-backup-migration"
LIVE = Path("/filesystem/k3s/data/pds")
BASE = Path("/filesystem/k3s/data/pds-backups")
RECOVERY = Path("/root/k3s-backup-recovery/pds")
COORDINATION_LOCK = Path("/root/k3s-backup-recovery/board-games/data-postgres-0/.setup.lock")
MANIFESTS = Path(__file__).resolve().parents[2] / "apps/pds/backups"
CLAIM = "pds-data"
IMAGE = "ghcr.io/bluesky-social/pds@sha256:20db9d828e9096978a907fe134125cdb26a00417aef6376920204bbfa060ab5c"
TARGET_NODE = "openmediavault"
TARGET_NODE_AFFINITY = {"required": {"nodeSelectorTerms": [{"matchExpressions": [
    {"key": "kubernetes.io/hostname", "operator": "In", "values": [TARGET_NODE]}]}]}}
SERVER_REQUESTS = {"cpu": "25m", "memory": "32Mi"}
DIRECTORIES = [
    (BASE / "backups", 65534, 65534, 0o2770),
    (BASE / "backups" / CLAIM, 65534, 65534, 0o700),
    (BASE / "backup-staging" / CLAIM, 999, 999, 0o2770),
    (BASE / "backup-cache" / CLAIM / "source", 999, 999, 0o2770),
]


class SetupError(Exception):
    pass


def run(arguments, *, stdin=None, capture=False, sensitive=False, check=True, timeout=240):
    try:
        result = subprocess.run(arguments, input=stdin, text=True, capture_output=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        raise SetupError("command timed out: " + arguments[0])
    if result.returncode:
        private_write(RECOVERY / "last-command-error.json", json.dumps({
            "command": arguments[0], "returncode": result.returncode,
            "stdout": result.stdout[-1024**2:], "stderr": result.stderr[-1024**2:],
        }, indent=2) + "\n")
    if not result.returncode and not capture and not sensitive and result.stdout:
        print(result.stdout, end="", flush=True)
    if check and result.returncode:
        raise SetupError("command failed: " + arguments[0] + "; diagnostics saved privately")
    return result


def kube(*arguments, **options):
    return run(["k3s", "kubectl", "--request-timeout=20s", *arguments], **options)


def get_object(resource, name, namespace=None, *, sensitive=False):
    arguments = ["get", resource, name, "--ignore-not-found=true", "-o", "json"]
    if namespace:
        arguments.extend(["-n", namespace])
    output = kube(*arguments, capture=True, sensitive=sensitive).stdout
    return json.loads(output) if output.strip() else None


def mounted_on_array(path):
    existing = path
    while not existing.exists():
        existing = existing.parent
    source = run(["findmnt", "-n", "-o", "SOURCE", "-T", str(existing)], capture=True).stdout.strip()
    filesystem = run(["findmnt", "-n", "-o", "FSTYPE", "-T", str(existing)], capture=True).stdout.strip()
    if source != "/dev/md0" or filesystem != "btrfs":
        raise SetupError(f"{path} is on {source}/{filesystem}, expected /dev/md0/btrfs")


def checked_directory(path):
    """Allow the established backup base mapping, but no new symlinks beneath it."""
    if not path.is_relative_to(BASE):
        raise SetupError("directory is outside the approved backup path")
    base_real = BASE.resolve(strict=True)
    if not path.resolve().is_relative_to(base_real):
        raise SetupError(f"directory escapes the approved backup path: {path}")
    current = path
    while current != BASE:
        if current.is_symlink():
            raise SetupError(f"unexpected symlink: {current}")
        if current.exists() and not current.is_dir():
            raise SetupError(f"expected a directory: {current}")
        current = current.parent
    mounted_on_array(path)


def checked_live_directory():
    if LIVE.is_symlink() or not LIVE.is_dir():
        raise SetupError("live PDS data path is missing or is a symlink")
    real = LIVE.resolve(strict=True)
    expected = Path("/srv/dev-disk-by-uuid-f33e3507-3ce3-4d3c-8423-1398e972f9e4/k3s/data/pds")
    if real != expected:
        raise SetupError("the approved live PDS array mapping changed")
    mounted_on_array(LIVE)
    attributes = LIVE.stat()
    if (attributes.st_uid, attributes.st_gid) != (1000, 1000):
        raise SetupError("live PDS directory ownership changed")


def recovery_directory():
    current = Path("/root")
    for component in RECOVERY.relative_to(current).parts:
        current /= component
        if current.is_symlink():
            raise SetupError(f"unexpected recovery-directory symlink: {current}")
        if current.exists():
            attributes = current.stat()
            if not current.is_dir() or attributes.st_uid != 0:
                raise SetupError(f"recovery directory must be owned by root: {current}")
            if stat.S_IMODE(attributes.st_mode) != 0o700:
                raise SetupError(f"recovery directory must have mode 0700: {current}")
        else:
            current.mkdir(mode=0o700)


def operation_lock(path, *, create):
    """Serialize with the established namespace backup workers."""
    current = Path("/root")
    for part in path.parent.relative_to(current).parts:
        current /= part
        attributes = current.lstat()
        if (not stat.S_ISDIR(attributes.st_mode) or attributes.st_uid != 0 or
                stat.S_IMODE(attributes.st_mode) != 0o700):
            raise SetupError("operation-lock directory is not private and root-owned")
    descriptor = os.open(path, os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK | (os.O_CREAT if create else 0),
                         0o600)
    stream = os.fdopen(descriptor, "r+")
    try:
        attributes = os.fstat(stream.fileno())
        if (not stat.S_ISREG(attributes.st_mode) or attributes.st_nlink != 1 or
                attributes.st_uid != 0 or stat.S_IMODE(attributes.st_mode) != 0o600):
            raise SetupError("operation lock is not a private regular root-owned file")
        try:
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise SetupError("another operation holds the backup lock: " + str(path)) from error
    except BaseException:
        stream.close()
        raise
    return stream


def private_read(path):
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(descriptor, "r") as stream:
        attributes = os.fstat(stream.fileno())
        if (not stat.S_ISREG(attributes.st_mode) or attributes.st_uid != 0 or attributes.st_nlink != 1 or
                stat.S_IMODE(attributes.st_mode) != 0o600 or attributes.st_size > 16 * 1024**2):
            raise SetupError(f"expected a private regular root-owned recovery file: {path}")
        return stream.read()


def private_write(path, content):
    if path.exists() or path.is_symlink():
        private_read(path)
    descriptor, temporary = tempfile.mkstemp(prefix=".pending-", dir=RECOVERY)
    try:
        with os.fdopen(descriptor, "w") as output:
            output.write(content)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
        directory = os.open(RECOVERY, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def ensure_secret(name, fields):
    data = {key: base64.b64encode(value.encode()).decode() for key, value in fields.items()}
    existing = get_object("secret", name, NAMESPACE, sensitive=True)
    if existing:
        if existing.get("data") != data:
            raise SetupError(f"existing Secret differs from saved recovery credentials: {name}")
        print(f"secret/{name} matches saved credentials", flush=True)
        return
    secret = {
        "apiVersion": "v1", "kind": "Secret",
        "metadata": {"name": name, "namespace": NAMESPACE,
                     "labels": {"app.kubernetes.io/managed-by": MANAGER}},
        "type": "Opaque", "data": data,
    }
    kube("create", "-f", "-", stdin=json.dumps(secret), sensitive=True)
    print(f"secret/{name} created", flush=True)


def portable(resource):
    return {"apiVersion": resource["apiVersion"], "kind": resource["kind"],
            "metadata": {"name": resource["metadata"]["name"],
                         **({"labels": resource["metadata"]["labels"]} if resource["metadata"].get("labels") else {}),
                         **({"namespace": resource["metadata"]["namespace"]} if resource["metadata"].get("namespace") else {})},
            **{key: resource[key] for key in ("spec", "data", "binaryData", "type") if key in resource}}


def save_secret(name, *, refresh=False, resource=None):
    secret = resource if resource is not None else get_object("secret", name, NAMESPACE, sensitive=True)
    if not secret:
        raise SetupError("required Secret is missing: " + name)
    if secret.get("kind") != "Secret" or secret["metadata"].get("name") != name or secret["metadata"].get("namespace") != NAMESPACE:
        raise SetupError("unexpected Secret identity: " + name)
    output = portable(secret)
    path = RECOVERY / (name + ".secret.json")
    if (path.exists() or path.is_symlink()) and not refresh:
        if json.loads(private_read(path)) != output:
            raise SetupError("Secret differs from the saved recovery copy: " + name)
    private_write(path, json.dumps(output, indent=2) + "\n")
    return secret


def ready(pod):
    return (not pod["metadata"].get("deletionTimestamp") and
            any(condition.get("type") == "Ready" and condition.get("status") == "True"
                for condition in pod.get("status", {}).get("conditions", [])))


def quantity(value):
    match = re.fullmatch(r"([0-9]+(?:\.[0-9]+)?)(n|u|m|Ki|Mi|Gi|Ti|Pi|Ei|k|K|M|G|T|P|E)?", value)
    if not match:
        raise SetupError("unrecognized Kubernetes resource quantity")
    suffix = match[2] or ""
    factors = {"": 1, "n": Decimal("1e-9"), "u": Decimal("1e-6"), "m": Decimal("1e-3"),
               **{unit + "i": 1024**power for power, unit in enumerate("KMGTPE", 1)},
               **{unit: 1000**power for power, unit in enumerate("KMGTPE", 1)}, "k": 1000}
    return Decimal(match[1]) * factors[suffix]


def resource_budget(node, pods, *, worker=True, extra=None):
    description = kube("describe", "node", "openmediavault", capture=True).stdout
    allocation = description.partition("Allocated resources:")[2].split("Events:", 1)[0]
    allocated = dict(re.findall(r"^\s+(cpu|memory)\s+(\S+)", allocation, re.MULTILINE))
    if set(allocated) != {"cpu", "memory"}:
        raise SetupError("could not determine the node's resource reservations")
    servers = [pod for pod in pods
               if pod["metadata"].get("labels", {}).get("app.kubernetes.io/name") == "restic-server"
               and pod["spec"].get("nodeName") == "openmediavault"
               and pod.get("status", {}).get("phase") not in ("Succeeded", "Failed")]
    if len(servers) > 1:
        raise SetupError("more than one pds Restic server Pod is reserving node resources")
    if servers and servers[0]["spec"]["containers"][0].get("resources", {}).get("requests") != SERVER_REQUESTS:
        raise SetupError("existing Restic server reservations differ from this setup")
    needed = {"cpu": Decimal("0.025") if not servers else Decimal("0"),
              "memory": (0 if servers else 32) * 1024**2}
    if worker:
        needed["cpu"] += Decimal("0.05")
        needed["memory"] += 128 * 1024**2
    for key, value in (extra or {}).items():
        if key not in needed:
            raise SetupError("unexpected resource budget extension: " + key)
        needed[key] += quantity(str(value))
    available = {key: quantity(node["status"]["allocatable"][key]) - quantity(allocated[key])
                 for key in needed}
    print("available reservations: " + str(round(available["cpu"] * 1000, 0)) + "m CPU, " +
          str(round(available["memory"] / 1024**2, 1)) + "Mi memory", flush=True)
    if any(available[key] < needed[key] for key in needed):
        raise SetupError("insufficient reservations for the pds backup endpoint or worker")


def live_preflight(*, check_resources=True):
    nodes = json.loads(kube("get", "nodes", "-o", "json", capture=True).stdout)["items"]
    matches = [item for item in nodes if item["metadata"]["name"] == TARGET_NODE]
    if len(matches) != 1:
        raise SetupError("the openmediavault node is missing or duplicated")
    node = matches[0]
    if (node["spec"].get("unschedulable") or any(item.get("effect") in ("NoSchedule", "NoExecute") for item in node["spec"].get("taints", [])) or
            not any(item.get("type") == "Ready" and item.get("status") == "True" for item in node["status"].get("conditions", [])) or
            any(item.get("status") == "True" for item in node["status"].get("conditions", [])
                if item.get("type") in ("MemoryPressure", "DiskPressure", "PIDPressure"))):
        raise SetupError("OMV is not ready/schedulable or reports node pressure")
    claim = get_object("pvc", CLAIM, NAMESPACE)
    volume = get_object("pv", CLAIM)
    if (not claim or not volume or claim.get("status", {}).get("phase") != "Bound" or
            volume.get("status", {}).get("phase") != "Bound" or
            claim["spec"].get("volumeName") != CLAIM or
            volume["spec"].get("claimRef", {}).get("uid") != claim["metadata"]["uid"] or
            volume["spec"].get("local", {}).get("path") != str(LIVE) or
            volume["spec"].get("persistentVolumeReclaimPolicy") != "Retain" or
            volume["spec"].get("nodeAffinity") != TARGET_NODE_AFFINITY or
            any(item["spec"].get("accessModes") != ["ReadWriteOncePod"] for item in (claim, volume))):
        raise SetupError("live binding differs from the verified inventory: " + CLAIM)
    checked_live_directory()
    deployment = get_object("deployment", "pds", NAMESPACE)
    if (not deployment or deployment["spec"].get("replicas") != 1 or
            deployment["spec"].get("strategy") != {"type": "Recreate"}):
        raise SetupError("the production PDS Deployment changed")
    spec = deployment["spec"]["template"]["spec"]
    containers = [item for item in spec.get("containers", []) if item.get("name") == "pds"]
    mounts = {item["name"]: item for item in spec.get("volumes", [])}
    if (len(containers) != 1 or containers[0].get("image") != IMAGE or
            spec.get("initContainers") or spec.get("ephemeralContainers") or
            spec.get("nodeSelector") != {"kubernetes.io/hostname": TARGET_NODE} or
            not any(mount.get("mountPath") == "/pds" and not mount.get("readOnly") and
                    not mount.get("subPath") and not mount.get("subPathExpr") and
                    mounts.get(mount["name"], {}).get("persistentVolumeClaim", {}).get("claimName") == CLAIM
                    for mount in containers[0].get("volumeMounts", [])) or
            containers[0].get("envFrom") != [{"secretRef": {"name": "pds-env"}}]):
        raise SetupError("PDS image, Secret, placement, or writable data mount changed")
    secret = get_object("secret", "pds-env", NAMESPACE, sensitive=True)
    if not secret:
        raise SetupError("required PDS environment Secret is missing")
    try:
        values = {key: base64.b64decode(value, validate=True).decode() for key, value in secret.get("data", {}).items()}
    except (ValueError, UnicodeDecodeError) as error:
        raise SetupError("PDS environment Secret is not valid UTF-8") from error
    for key, expected in (("PDS_HOSTNAME", "bluesky.mousses.xyz"), ("PDS_DATA_DIRECTORY", "/pds"),
                          ("PDS_BLOBSTORE_DISK_LOCATION", "/pds/blocks"), ("PDS_PORT", "3000")):
        if values.get(key) != expected:
            raise SetupError("PDS environment differs from the verified identity: " + key)
    for key in ("PDS_JWT_SECRET", "PDS_ADMIN_PASSWORD", "PDS_PLC_ROTATION_KEY_K256_PRIVATE_KEY_HEX"):
        if not values.get(key):
            raise SetupError("PDS environment is missing a required secret: " + key)
    pods = json.loads(kube("get", "pods", "-n", NAMESPACE, "-o", "json", capture=True).stdout)["items"]
    users = [pod for pod in pods if pod.get("status", {}).get("phase") not in ("Succeeded", "Failed") and
             any(volume.get("persistentVolumeClaim", {}).get("claimName") == CLAIM
                 for volume in pod["spec"].get("volumes", []))]
    if len(users) != 1 or not ready(users[0]) or users[0]["spec"].get("nodeName") != "openmediavault":
        raise SetupError("expected exactly one ready PDS Pod using the live claim")
    if json.loads(kube("get", "hpa", "-n", NAMESPACE, "-o", "json", capture=True).stdout)["items"]:
        raise SetupError("PDS autoscaling requires inspection before backup")
    if check_resources:
        resource_budget(node, pods, worker=False)
    for filename, resource in (
            (CLAIM + ".pvc.json", claim), (CLAIM + ".pv.json", volume),
            ("pds.deployment.json", deployment)):
        path = RECOVERY / filename
        if (path.exists() or path.is_symlink()) and json.loads(private_read(path))["metadata"]["uid"] != resource["metadata"]["uid"]:
            raise SetupError("saved resource identity changed: " + filename)
        private_write(path, json.dumps(portable(resource), indent=2) + "\n")
    save_secret("pds-env", resource=secret)
    return deployment


def render_resources():
    rendered = kube("create", "--dry-run=client", "-k", str(MANIFESTS), "-o", "json", capture=True).stdout.lstrip()
    decoder = json.JSONDecoder()
    resources = []
    while rendered:
        resource, end = decoder.raw_decode(rendered)
        resources.extend(resource.get("items", [resource]))
        rendered = rendered[end:].lstrip()
    if not resources:
        raise SetupError("backup manifest bundle is empty")
    for resource in resources:
        metadata = resource["metadata"]
        group = resource["apiVersion"].split("/")[0] if "/" in resource["apiVersion"] else ""
        kind = resource["kind"].lower() + ("." + group if group else "")
        existing = get_object(kind, metadata["name"], metadata.get("namespace"))
        if not existing:
            continue
        if existing["metadata"].get("labels", {}).get("app.kubernetes.io/managed-by") != MANAGER:
            raise SetupError("resource name is already used outside this migration: " + metadata["name"])
        if resource["kind"] in ("PersistentVolume", "PersistentVolumeClaim", "StorageClass"):
            actual = existing.get("spec", existing)
            expected = resource.get("spec", resource)
            for key in ("local", "accessModes", "volumeMode", "storageClassName", "volumeName", "capacity",
                        "persistentVolumeReclaimPolicy", "nodeAffinity", "provisioner", "reclaimPolicy",
                        "volumeBindingMode", "resources"):
                if key in expected and actual.get(key) != expected[key]:
                    raise SetupError("existing backup storage differs: " + metadata["name"] + ": " + key)
            if resource["kind"] == "PersistentVolume":
                reference = actual.get("claimRef", {})
                if any(reference.get(key) != value for key, value in expected["claimRef"].items()):
                    raise SetupError("backup PV is reserved for a different claim: " + metadata["name"])
                if existing.get("status", {}).get("phase") in ("Released", "Failed"):
                    raise SetupError("backup PV requires manual binding inspection: " + metadata["name"])
    return resources


def credential_fields(claim, entry):
    repository = "rest:https://restic.pds.svc:8000/" + claim + "/"
    fields = ("RESTIC_REPOSITORY", "RESTIC_PASSWORD", "RESTIC_REST_USERNAME", "RESTIC_REST_PASSWORD", "htpasswd")
    if (not isinstance(entry, dict) or set(entry) != set(fields) or
            entry.get("RESTIC_REPOSITORY") != repository or entry.get("RESTIC_REST_USERNAME") != claim or
            not all(isinstance(entry[key], str) and entry[key] for key in fields)):
        raise SetupError("saved repository credentials are invalid: " + claim)
    return {key: value for key, value in entry.items() if key != "htpasswd"}


def prepare_credentials():
    path = RECOVERY / "credentials.json"
    have_credentials = path.exists() or path.is_symlink()
    credentials = json.loads(private_read(path)) if have_credentials else None
    for directory, uid, gid, mode in DIRECTORIES:
        checked_directory(directory)
        if directory.exists() and any(directory.iterdir()):
            if not have_credentials:
                raise SetupError("nonempty backup directory has no saved credentials: " + str(directory))
            attributes = directory.stat()
            if (attributes.st_uid, attributes.st_gid, stat.S_IMODE(attributes.st_mode)) != (uid, gid, mode):
                raise SetupError("existing backup directory ownership or mode differs: " + str(directory))
    if not have_credentials:
        for name in ("restic-root-ca", "restic-server-tls", "restic-server-auth", "restic-" + CLAIM):
            if get_object("secret", name, NAMESPACE, sensitive=True):
                raise SetupError("existing Secret has no saved recovery credentials: " + name)
        password = secrets.token_hex(32)
        htpasswd = run(["htpasswd", "-n", "-i", "-B", "-C", "12", CLAIM],
                       stdin=password + "\n", capture=True, sensitive=True).stdout.strip() + "\n"
        credentials = {"namespace": NAMESPACE, "repositories": {CLAIM: {
            "RESTIC_REPOSITORY": "rest:https://restic.pds.svc:8000/" + CLAIM + "/",
            "RESTIC_PASSWORD": secrets.token_hex(32), "RESTIC_REST_USERNAME": CLAIM,
            "RESTIC_REST_PASSWORD": password, "htpasswd": htpasswd,
        }}}
        private_write(path, json.dumps(credentials, indent=2) + "\n")
    if (credentials.get("namespace") != NAMESPACE or set(credentials.get("repositories", {})) != {CLAIM}):
        raise SetupError("saved credentials do not describe the approved repository")
    entry = credentials["repositories"][CLAIM]
    credential_fields(CLAIM, entry)
    secret_fields = {
        "restic-server-auth": {".htpasswd": entry["htpasswd"]},
        "restic-" + CLAIM: credential_fields(CLAIM, entry),
    }
    for name, fields in secret_fields.items():
        existing = get_object("secret", name, NAMESPACE, sensitive=True)
        expected = {key: base64.b64encode(value.encode()).decode() for key, value in fields.items()}
        if existing and existing.get("data") != expected:
            raise SetupError("existing Secret differs from the saved credentials: " + name)
    return secret_fields


def wait_for_job(name, *, timeout=330):
    started = time.monotonic()
    while time.monotonic() - started < timeout:
        job = get_object("job", name, NAMESPACE)
        if not job:
            raise SetupError("repository check Job disappeared: " + name)
        conditions = job.get("status", {}).get("conditions", [])
        if any(item.get("type") == "Complete" and item.get("status") == "True" for item in conditions):
            print("job/" + name + " complete", flush=True)
            return
        for item in conditions:
            if item.get("type") in ("Failed", "FailureTarget") and item.get("status") == "True":
                raise SetupError("repository check failed: " + name + ": " + item.get("reason", "unknown"))
        if time.monotonic() - started >= 60:
            pods = json.loads(kube("get", "pods", "-n", NAMESPACE, "-l", "batch.kubernetes.io/job-name=" + name,
                                   "-o", "json", capture=True).stdout)["items"]
            for pod in pods:
                for item in pod.get("status", {}).get("conditions", []):
                    if item.get("type") == "PodScheduled" and item.get("status") == "False":
                        raise SetupError("repository check cannot schedule: " + item.get("message", "unknown reason"))
        time.sleep(2)
    raise SetupError("repository check timed out: " + name)


def print_check_logs(name):
    result = kube("logs", "-n", NAMESPACE, "job/" + name, "--pod-running-timeout=5s",
                  capture=True, sensitive=True, check=False, timeout=15)
    if result.returncode:
        print("repository check logs are not available yet: " + name, flush=True)
        return
    output = result.stdout
    entries = json.loads(private_read(RECOVERY / "credentials.json"))["repositories"]
    for entry in entries.values():
        for field in ("RESTIC_PASSWORD", "RESTIC_REST_PASSWORD", "htpasswd"):
            output = output.replace(entry[field], "[redacted]")
        basic_auth = base64.b64encode((entry["RESTIC_REST_USERNAME"] + ":" + entry["RESTIC_REST_PASSWORD"]).encode()).decode()
        output = output.replace(basic_auth, "[redacted]")
    print(output, end="", flush=True)


def repository_checks():
    active = json.loads(kube("get", "pods", "-n", NAMESPACE, "-l", "backups.mousses.xyz/restic-client=true",
                             "-o", "json", capture=True).stdout)["items"]
    if any(pod.get("status", {}).get("phase") not in ("Succeeded", "Failed") for pod in active):
        raise SetupError("another repository client is still active; wait for it to stop before rerunning setup")
    service = get_object("service", "restic", NAMESPACE)
    ips = service["spec"].get("clusterIPs") or [service["spec"].get("clusterIP")]
    expected = ",".join(str(ipaddress.ip_address(value)) for value in ips)
    job = json.loads(kube("create", "--dry-run=client", "-f", str(MANIFESTS / "repository-check.yaml"),
                          "-o", "json", capture=True).stdout)
    container = job["spec"]["template"]["spec"]["containers"][0]
    container["envFrom"] = [{"secretRef": {"name": "restic-" + CLAIM}}]
    container["env"] = [{"name": "EXPECTED_RESTIC_IPS", "value": expected},
                        {"name": "OTHER_REPOSITORY", "value": ""}]
    created = json.loads(kube("create", "-f", "-", "-o", "json", stdin=json.dumps(job), capture=True).stdout)
    name = created["metadata"]["name"]
    print("job/" + name + " created (pds data DNS/TLS/authentication check)", flush=True)
    try:
        wait_for_job(name)
    finally:
        print_check_logs(name)


def prepare():
    live_preflight()
    render_resources()
    fields = prepare_credentials()
    for directory, uid, gid, mode in DIRECTORIES:
        directory.mkdir(parents=True, exist_ok=True)
        os.chown(directory, uid, gid)
        os.chmod(directory, mode)
        mounted_on_array(directory)
        print("array directory prepared: " + str(directory), flush=True)
    for name, values in fields.items():
        ensure_secret(name, values)
    kube("apply", "-k", str(MANIFESTS))
    kube("wait", "-n", NAMESPACE, "--for=jsonpath={.status.phase}=Bound", "--timeout=60s",
         "pvc/restic-repositories", "pvc/data-backup-staging")
    for name in ("restic-repositories", "data-backup-staging"):
        claim = get_object("pvc", name, NAMESPACE)
        volume = get_object("pv", claim["spec"]["volumeName"])
        if volume["spec"].get("claimRef", {}).get("uid") != claim["metadata"]["uid"]:
            raise SetupError("new backup PV claim UID does not match: " + name)
    kube("wait", "-n", NAMESPACE, "--for=condition=Ready", "--timeout=180s",
         "certificate/restic-root-ca", "certificate/restic-server-tls")
    kube("rollout", "status", "-n", NAMESPACE, "deployment/restic-server", "--timeout=180s")
    for name in fields:
        save_secret(name)
    save_secret("restic-server-tls", refresh=True)
    ca = save_secret("restic-root-ca")
    private_write(RECOVERY / "ca.crt", base64.b64decode(ca["data"]["tls.crt"], validate=True).decode())
    print("recovery credentials saved privately under " + str(RECOVERY) + "; do not paste them", flush=True)
    repository_checks()
    result = {"result": "passed", "namespace": NAMESPACE, "repositories": [CLAIM],
              "completed_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
    private_write(RECOVERY / "endpoint-verification.json", json.dumps(result, indent=2) + "\n")
    kube("get", "pods", "-n", NAMESPACE, "-o", "wide")
    kube("get", "pvc", "-n", NAMESPACE)
    print("pds data repository and array-backed staging verified; initial backup and restore remain pending", flush=True)


def main():
    if os.geteuid() != 0:
        raise SetupError("run with sudo python3 on OMV")
    os.umask(0o077)
    os.environ["PATH"] = "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
    for command in ("k3s", "findmnt", "htpasswd"):
        if not shutil.which(command):
            raise SetupError("required command is missing: " + command)
    if not LIVE.is_dir() or not MANIFESTS.is_dir():
        raise SetupError("approved live data path or downloaded manifests are missing")
    if not BASE.exists():
        BASE.mkdir(parents=True, mode=0o700)
        os.chmod(BASE, 0o700)
    mounted_on_array(BASE)
    if shutil.disk_usage(BASE).free < 20 * 1024**3:
        raise SetupError("less than 20 GiB remains on the approved array")
    recovery_directory()
    with operation_lock(RECOVERY / ".setup.lock", create=True):
        with operation_lock(COORDINATION_LOCK, create=False):
            print("pds endpoint setup serialized with the existing namespace backup workers", flush=True)
            prepare()


if __name__ == "__main__":
    try:
        main()
    except (SetupError, OSError, ValueError, KeyError, TypeError, subprocess.TimeoutExpired) as error:
        print("setup stopped: " + str(error), file=sys.stderr)
        sys.exit(1)
