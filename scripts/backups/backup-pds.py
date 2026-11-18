#!/usr/bin/env python3
"""Capture a consistent PDS data directory, upload it with VolSync, and verify it."""

from datetime import datetime, timezone
import hashlib
import importlib.util
import ipaddress
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import signal
import sqlite3
import stat
import subprocess
import sys
import tarfile
import tempfile
import time


module_spec = importlib.util.spec_from_file_location(
    "pds_setup", Path(__file__).with_name("prepare-pds.py"),
)
setup = importlib.util.module_from_spec(module_spec)
module_spec.loader.exec_module(setup)
NS = setup.NAMESPACE
MANIFESTS = setup.MANIFESTS / "data"
STATE = setup.RECOVERY / "initial-backup.json"
QUIESCENCE = setup.RECOVERY / "data-quiescence.json"
CLAIM = setup.CLAIM
COMPONENT = "data"
SOURCE = "data-backup"
SERVICE_DID = "did:web:bluesky.mousses.xyz"
ARTIFACTS = {
    "data.tar", "contents.json", "application.json", "application-config.json",
    "application-secret.json", "sqlite-integrity.json", "metadata.json", "backup-id",
    "SHA256SUMS",
}
APPLICATION_SCRIPT = r"""
const http = require('http');
const get = (path) => new Promise((resolve, reject) => {
  const request = http.get({host: '127.0.0.1', port: 3000, path, timeout: 15000}, (response) => {
    let body = '';
    response.setEncoding('utf8');
    response.on('data', (chunk) => {
      body += chunk;
      if (body.length > 4 * 1024 * 1024) {
        request.destroy(new Error('response too large: ' + path));
      }
    });
    response.on('end', () => {
      if (response.statusCode !== 200) {
        reject(new Error('HTTP ' + response.statusCode + ': ' + path));
        return;
      }
      try {
        resolve(JSON.parse(body));
      } catch (error) {
        reject(new Error('invalid JSON: ' + path));
      }
    });
  });
  request.on('timeout', () => request.destroy(new Error('timeout: ' + path)));
  request.on('error', reject);
});
(async () => {
  const health = await get('/xrpc/_health');
  const describe = await get('/xrpc/com.atproto.server.describeServer');
  const repos = [];
  let cursor = null;
  for (let page = 0; page < 100; page += 1) {
    const suffix = cursor ? '&cursor=' + encodeURIComponent(cursor) : '';
    const listing = await get('/xrpc/com.atproto.sync.listRepos?limit=1000' + suffix);
    if (!Array.isArray(listing.repos)) {
      throw new Error('invalid repository listing');
    }
    repos.push(...listing.repos);
    if (!listing.cursor) {
      break;
    }
    if (listing.cursor === cursor) {
      throw new Error('repository cursor did not advance');
    }
    cursor = listing.cursor;
  }
  console.log('PDS_EVIDENCE_JSON=' + JSON.stringify({health, describe, repos}));
})().catch((error) => {
  console.error(String(error));
  process.exit(1);
});
"""


def now():
    return datetime.now(timezone.utc).isoformat()


def save(path, value):
    setup.private_write(path, json.dumps(value, indent=2) + "\n")


def saved(path):
    return json.loads(setup.private_read(path))


def template_hash(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def staging():
    return setup.BASE / "backup-staging" / CLAIM


def managed(resource):
    if resource["metadata"].get("labels", {}).get("app.kubernetes.io/managed-by") != setup.MANAGER:
        raise setup.SetupError("resource is not owned by this backup setup: " + resource["metadata"]["name"])


def contains_fields(actual, expected):
    if isinstance(expected, dict):
        return isinstance(actual, dict) and all(key in actual and contains_fields(actual[key], value)
                                                for key, value in expected.items())
    if isinstance(expected, list):
        return isinstance(actual, list) and len(actual) == len(expected) and all(
            contains_fields(left, right) for left, right in zip(actual, expected))
    return actual == expected


def read_template(filename):
    return json.loads(setup.kube("create", "--dry-run=client", "-f", str(filename), "-o", "json",
                                 capture=True).stdout)


def sync_directory(path):
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def digest_file(path):
    attributes = path.lstat()
    if not stat.S_ISREG(attributes.st_mode):
        raise setup.SetupError("backup artifact is not a regular file")
    checksum = hashlib.sha256()
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(descriptor, "rb") as source:
        opened = os.fstat(source.fileno())
        if (opened.st_ino, opened.st_dev) != (attributes.st_ino, attributes.st_dev):
            raise setup.SetupError("backup artifact changed while opening it")
        while chunk := source.read(1024 * 1024):
            checksum.update(chunk)
        after = os.fstat(source.fileno())
        if (after.st_size, after.st_mtime_ns, after.st_ctime_ns) != (
                opened.st_size, opened.st_mtime_ns, opened.st_ctime_ns):
            raise setup.SetupError("backup artifact changed while hashing it")
    return checksum.hexdigest()


def artifact_write(path, value):
    # Pending generation directories are private, validated, and never uploaded.
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, "w") as output:
        output.write(value)
        output.flush()
        os.fsync(output.fileno())
        os.fchown(output.fileno(), 999, 999)


def inspect_artifacts(identifier, *, directory_modes=(0o700,), current=None):
    if current is None:
        root = staging()
        setup.checked_directory(root)
        current = root / "current"
        if not current.exists() and not current.is_symlink():
            return None
        if {path.name for path in root.iterdir()} != {"current"}:
            raise setup.SetupError("unexpected staging data exists beside the published backup")
    setup.checked_directory(current)
    attributes = current.stat()
    mode = stat.S_IMODE(attributes.st_mode)
    if (attributes.st_uid, attributes.st_gid) != (999, 999) or mode not in directory_modes:
        raise setup.SetupError("published backup directory ownership or mode changed; observed uid=" +
                               str(attributes.st_uid) + " gid=" + str(attributes.st_gid) +
                               " mode=" + format(mode, "04o"))
    if {path.name for path in current.iterdir()} != ARTIFACTS:
        raise setup.SetupError("published backup artifact set is incomplete")
    for path in current.iterdir():
        attributes = path.lstat()
        if (not stat.S_ISREG(attributes.st_mode) or attributes.st_nlink != 1 or
                (attributes.st_uid, attributes.st_gid, stat.S_IMODE(attributes.st_mode)) != (999, 999, 0o600)):
            raise setup.SetupError("published artifact ownership, mode, or file type changed: " + path.name +
                                   "; observed uid=" + str(attributes.st_uid) +
                                   " gid=" + str(attributes.st_gid) +
                                   " mode=" + format(stat.S_IMODE(attributes.st_mode), "04o"))
    if (current / "backup-id").read_text().strip() != identifier:
        raise setup.SetupError("published artifacts belong to a different generation")
    metadata = json.loads((current / "metadata.json").read_text())
    if metadata.get("backup_id") != identifier or metadata.get("claim") != CLAIM:
        raise setup.SetupError("published metadata does not match its claim and generation")
    checksums = (current / "SHA256SUMS").read_text()
    lines = checksums.splitlines()
    names = []
    for line in lines:
        match = re.fullmatch(r"([0-9a-f]{64})  ([a-zA-Z0-9.-]+)", line)
        if not match or match[2] not in ARTIFACTS - {"SHA256SUMS"}:
            raise setup.SetupError("published checksum manifest is invalid")
        names.append(match[2])
        if digest_file(current / match[2]) != match[1]:
            raise setup.SetupError("published backup artifact checksum failed")
    if len(names) != len(set(names)) or set(names) != ARTIFACTS - {"SHA256SUMS"}:
        raise setup.SetupError("published checksum manifest is incomplete")
    return checksums


def active_claim_pods(claim):
    pods = json.loads(setup.kube("get", "pods", "-n", NS, "-o", "json", capture=True).stdout)["items"]
    return [pod for pod in pods if pod.get("status", {}).get("phase") not in ("Succeeded", "Failed")
            and any(volume.get("persistentVolumeClaim", {}).get("claimName") == claim
                    for volume in pod["spec"].get("volumes", []))]


def ensure_source_idle(identifier):
    source = setup.get_object("replicationsource", SOURCE, NS)
    if (not source or not source["spec"].get("paused") or
            source["spec"].get("trigger", {}).get("manual") not in ("controlled-by-backup-runner", identifier)):
        raise setup.SetupError("staging may only change while its VolSync source is paused")
    if active_claim_pods("data-backup-staging"):
        raise setup.SetupError("a Pod is still using backup staging")


def worker_budget():
    node = setup.get_object("node", "openmediavault")
    pods = json.loads(setup.kube("get", "pods", "-n", NS, "-o", "json", capture=True).stdout)["items"]
    setup.resource_budget(node, pods)


def new_pending(identifier):
    ensure_source_idle(identifier)
    root = staging()
    setup.checked_directory(root)
    if any(root.iterdir()):
        raise setup.SetupError("unexpected or partial staging data is preserved")
    path = root / (".pending-" + identifier)
    path.mkdir(mode=0o700)
    os.chown(path, 999, 999)
    # Linux mkdir inherits S_ISGID from the staging root despite mode=0700.
    # Apply the exact private mode after ownership, which may also affect bits.
    os.chmod(path, 0o700)
    sync_directory(root)
    return path


def publish(identifier, pending):
    ensure_source_idle(identifier)
    setup.checked_directory(pending)
    attributes = pending.stat()
    if not stat.S_ISDIR(attributes.st_mode) or (attributes.st_uid, attributes.st_gid) != (999, 999):
        raise setup.SetupError("pending backup directory ownership changed")
    os.chmod(pending, 0o700)
    if {path.name for path in pending.iterdir()} != ARTIFACTS - {"SHA256SUMS"}:
        raise setup.SetupError("pending backup artifacts are incomplete")
    for path in pending.iterdir():
        attributes = path.lstat()
        if not stat.S_ISREG(attributes.st_mode) or attributes.st_nlink != 1:
            raise setup.SetupError("pending backup artifact has an unexpected file type or hard link")
        os.chown(path, 999, 999, follow_symlinks=False)
        os.chmod(path, 0o600)
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    checksums = "".join(digest_file(pending / name) + "  " + name + "\n"
                        for name in sorted(ARTIFACTS - {"SHA256SUMS"}))
    artifact_write(pending / "SHA256SUMS", checksums)
    sync_directory(pending)
    current = staging() / "current"
    if current.exists() or current.is_symlink():
        raise setup.SetupError("refusing to replace a published backup generation")
    pending.rename(current)
    sync_directory(staging())
    inspect_artifacts(identifier)
    print("consistent PDS artifacts published and checksummed", flush=True)


def validate_application(evidence):
    if not isinstance(evidence, dict) or set(evidence) != {"health", "describe", "repos"}:
        raise setup.SetupError("the PDS application response is incomplete")
    health, describe, repos = evidence["health"], evidence["describe"], evidence["repos"]
    if (not isinstance(health, dict) or not isinstance(health.get("version"), str) or
            not re.fullmatch(r"[0-9A-Za-z.+_-]{1,64}", health["version"])):
        raise setup.SetupError("the PDS health response has no runtime version")
    if (not isinstance(describe, dict) or describe.get("did") != SERVICE_DID or
            not isinstance(describe.get("availableUserDomains"), list) or
            not describe["availableUserDomains"] or
            not all(isinstance(value, str) and value for value in describe["availableUserDomains"])):
        raise setup.SetupError("the PDS service identity or handle domains changed")
    if (not isinstance(repos, list) or not repos or
            not all(isinstance(repo, dict) and isinstance(repo.get("did"), str) and repo["did"]
                    for repo in repos)):
        raise setup.SetupError("the PDS repository listing is empty or invalid")
    dids = sorted(repo["did"] for repo in repos)
    if len(dids) != len(set(dids)):
        raise setup.SetupError("the PDS repository listing contains duplicate DIDs")
    return {
        "captured_at": now(), "health": health, "describe": describe,
        "repo_count": len(dids), "repo_dids": dids,
    }


def capture_application_evidence():
    pods = active_claim_pods(CLAIM)
    if len(pods) != 1 or not setup.ready(pods[0]):
        raise setup.SetupError("expected one ready PDS Pod before capturing application evidence")
    result = setup.kube("exec", "-n", NS, "pod/" + pods[0]["metadata"]["name"], "--",
                        "node", "-e", APPLICATION_SCRIPT, capture=True, sensitive=True, timeout=120)
    lines = [line.removeprefix("PDS_EVIDENCE_JSON=") for line in result.stdout.splitlines()
             if line.startswith("PDS_EVIDENCE_JSON=")]
    if len(lines) != 1:
        raise setup.SetupError("the PDS Pod did not report exactly one application snapshot")
    try:
        evidence = json.loads(lines[0])
    except ValueError as error:
        raise setup.SetupError("the PDS application snapshot is not valid JSON") from error
    application = validate_application(evidence)
    save(setup.RECOVERY / "latest-application.json", application)
    print("captured PDS service identity, health, and repository listing", flush=True)
    return application


class HashingReader:
    def __init__(self, source):
        self.source = source
        self.checksum = hashlib.sha256()

    def read(self, count):
        data = self.source.read(count)
        self.checksum.update(data)
        return data


def data_archive(source, destination):
    """Archive regular files and directories in one pass, without following links."""
    contents = []
    device = source.stat().st_dev
    with tarfile.open(destination, "x", format=tarfile.PAX_FORMAT, dereference=False) as archive:
        def visit(path, relative):
            before = path.lstat()
            if before.st_dev != device or (not stat.S_ISDIR(before.st_mode) and not stat.S_ISREG(before.st_mode)):
                raise setup.SetupError("PDS data contains a link, special file, or nested filesystem; preserving it")
            entry = {"path": relative, "uid": before.st_uid, "gid": before.st_gid,
                     "mode": stat.S_IMODE(before.st_mode), "mtime_ns": before.st_mtime_ns}
            if stat.S_ISDIR(before.st_mode):
                names = sorted(item.name for item in path.iterdir())
                information = archive.gettarinfo(str(path), arcname=relative)
                information.uname = information.gname = ""
                archive.addfile(information)
                entry["type"] = "directory"
                contents.append(entry)
                for name in names:
                    visit(path / name, name if relative == "." else relative + "/" + name)
                after = path.lstat()
                if (sorted(item.name for item in path.iterdir()) != names or
                        (after.st_ino, after.st_dev, after.st_mtime_ns, after.st_ctime_ns) != (
                            before.st_ino, before.st_dev, before.st_mtime_ns, before.st_ctime_ns)):
                    raise setup.SetupError("PDS data directory changed while quiesced")
            else:
                if before.st_nlink != 1:
                    raise setup.SetupError("PDS data contains a hard-linked file; preserving it")
                descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
                with os.fdopen(descriptor, "rb") as stream:
                    attributes = os.fstat(stream.fileno())
                    if (attributes.st_ino, attributes.st_dev) != (before.st_ino, before.st_dev):
                        raise setup.SetupError("PDS data file changed while opening it")
                    reader = HashingReader(stream)
                    information = archive.gettarinfo(fileobj=stream, arcname=relative)
                    information.uname = information.gname = ""
                    archive.addfile(information, reader)
                    after = os.fstat(stream.fileno())
                    if (after.st_size, after.st_mtime_ns, after.st_ctime_ns) != (
                            before.st_size, before.st_mtime_ns, before.st_ctime_ns):
                        raise setup.SetupError("PDS data file changed while quiesced")
                after = path.lstat()
                if (after.st_ino, after.st_dev, after.st_size, after.st_mtime_ns, after.st_ctime_ns) != (
                        before.st_ino, before.st_dev, before.st_size, before.st_mtime_ns, before.st_ctime_ns):
                    raise setup.SetupError("PDS data file was replaced while quiesced")
                entry.update({"type": "file", "bytes": before.st_size, "sha256": reader.checksum.hexdigest()})
                contents.append(entry)
        visit(source, ".")
    return contents


def sqlite_integrity(contents):
    files = {entry["path"]: entry for entry in contents if entry["type"] == "file"}
    databases = {}
    with tempfile.TemporaryDirectory(prefix="pds-sqlite-", dir="/tmp") as folder:
        root = Path(folder)
        for path in sorted(setup.LIVE.rglob("*")):
            if path.suffix not in (".sqlite", ".sqlite3"):
                continue
            relative = path.relative_to(setup.LIVE).as_posix()
            before = path.lstat()
            if (not stat.S_ISREG(before.st_mode) or before.st_nlink != 1 or relative not in files):
                raise setup.SetupError("PDS SQLite database has an unexpected file type: " + relative)
            if files[relative]["sha256"] != digest_file(path):
                raise setup.SetupError("PDS SQLite database differs from its archived contents: " + relative)
            copy = root / ("database-" + str(len(databases)) + path.suffix)
            shutil.copyfile(path, copy)
            os.chmod(copy, 0o600)
            for suffix in ("-wal", "-shm"):
                sidecar = path.with_name(path.name + suffix)
                if sidecar.exists() or sidecar.is_symlink():
                    attributes = sidecar.lstat()
                    if not stat.S_ISREG(attributes.st_mode) or attributes.st_nlink != 1:
                        raise setup.SetupError("PDS SQLite sidecar has an unexpected file type: " + relative + suffix)
                    shutil.copyfile(sidecar, copy.with_name(copy.name + suffix))
                    os.chmod(copy.with_name(copy.name + suffix), 0o600)
            connection = sqlite3.connect(str(copy), timeout=30)
            try:
                result = connection.execute("PRAGMA integrity_check").fetchall()
            finally:
                connection.close()
            if result != [("ok",)]:
                raise setup.SetupError("PDS SQLite integrity check failed: " + relative)
            after = path.lstat()
            if (after.st_ino, after.st_dev, after.st_size, after.st_mtime_ns, after.st_ctime_ns) != (
                    before.st_ino, before.st_dev, before.st_size, before.st_mtime_ns, before.st_ctime_ns):
                raise setup.SetupError("PDS SQLite database changed while checking it: " + relative)
            databases[relative] = {
                "bytes": before.st_size, "sha256": files[relative]["sha256"], "integrity_check": "ok",
            }
    if "account.sqlite" not in databases:
        raise setup.SetupError("the PDS account database is missing")
    return {"captured_at": now(), "databases": databases}


def application_artifacts():
    deployment = setup.get_object("deployment", "pds", NS)
    secret = setup.get_object("secret", "pds-env", NS, sensitive=True)
    if (not deployment or not secret or deployment["metadata"].get("uid") is None or
            secret["metadata"].get("name") != "pds-env" or secret["metadata"].get("namespace") != NS):
        raise setup.SetupError("the PDS application resources disappeared before capture")
    config = setup.portable(deployment)
    credentials = setup.portable(secret)
    if (config.get("kind") != "Deployment" or config["metadata"].get("name") != "pds" or
            credentials.get("kind") != "Secret" or credentials["metadata"].get("name") != "pds-env" or
            not credentials.get("data", {}).get("PDS_JWT_SECRET")):
        raise setup.SetupError("the PDS application snapshot is incomplete")
    return config, credentials


def check_quiescence_state(state, identifier=None):
    if (not isinstance(state, dict) or not re.fullmatch(r"(?:initial|daily)-[0-9a-f]{32}", state.get("backup_id", "")) or
            (identifier is not None and state["backup_id"] != identifier) or
            state.get("original_replicas") != 1 or not state.get("deployment_uid") or
            not re.fullmatch(r"[0-9a-f]{64}", state.get("template_hash", "")) or
            state.get("phase") not in ("quiescing", "captured", "resumed")):
        raise setup.SetupError("saved PDS quiescence state is invalid")


def resume_pds(state):
    check_quiescence_state(state)
    deployment = setup.get_object("deployment", "pds", NS)
    if (not deployment or deployment["metadata"]["uid"] != state["deployment_uid"] or
            template_hash(deployment["spec"]["template"]) != state["template_hash"]):
        raise setup.SetupError("PDS was replaced or its Pod template changed; inspect the saved quiescence journal")
    replicas = deployment["spec"].get("replicas")
    if replicas == 0:
        setup.kube("scale", "deployment/pds", "-n", NS, "--replicas=1", "--current-replicas=0",
                   "--resource-version=" + deployment["metadata"]["resourceVersion"])
    elif replicas != 1:
        raise setup.SetupError("PDS replica count changed outside the backup; preserving it")
    setup.kube("rollout", "status", "deployment/pds", "-n", NS, "--timeout=180s")
    pods = active_claim_pods(CLAIM)
    if len(pods) != 1 or not setup.ready(pods[0]):
        raise setup.SetupError("PDS did not become ready after data capture")
    state = {**state, "phase": "resumed", "resumed_at": now()}
    save(QUIESCENCE, state)
    print("PDS restarted and ready; data capture downtime ended", flush=True)


def running_containers():
    return json.loads(setup.run(["k3s", "crictl", "ps", "-o", "json"],
                                capture=True, sensitive=True, timeout=30).stdout)["containers"]


def stopped_writers(old_uids):
    deadline = time.monotonic() + 180
    while time.monotonic() < deadline:
        pods = active_claim_pods(CLAIM)
        if not pods:
            containers = running_containers()
            if not any(container.get("labels", {}).get("io.kubernetes.pod.uid") in old_uids or
                       (container.get("labels", {}).get("io.kubernetes.pod.namespace") == NS and
                        container.get("metadata", {}).get("name") == "pds") for container in containers):
                return
        time.sleep(2)
    raise setup.SetupError("PDS writers did not stop; no data archive was created")


def pending_without_reservation_risk(pod, writer):
    """Recognize only zero-request Pods that cannot exclude the replacement writer."""
    spec = pod["spec"]
    namespace = pod["metadata"]["namespace"]
    if namespace == NS or spec.get("resourceClaims") or spec.get("hostNetwork"):
        return False
    containers = spec.get("containers", []) + spec.get("initContainers", [])
    requests = [(spec.get("resources") or {}).get("requests", {}), spec.get("overhead") or {}]
    requests.extend((container.get("resources") or {}).get("requests", {}) for container in containers)
    if any(setup.quantity(str(value)) != 0 for resources in requests for value in resources.values()):
        return False
    if any(port.get("hostPort", 0) for container in containers for port in container.get("ports", [])):
        return False
    # Required anti-affinity is symmetric: either Pod can exclude the other.
    if ((writer.get("affinity") or {}).get("podAntiAffinity") or {}).get(
            "requiredDuringSchedulingIgnoredDuringExecution"):
        return False
    for term in (((spec.get("affinity") or {}).get("podAntiAffinity") or {}).get(
            "requiredDuringSchedulingIgnoredDuringExecution") or []):
        namespaces = term.get("namespaces") or [namespace]
        if term.get("namespaceSelector") is not None or NS in namespaces:
            return False
    return True


def check_scheduling_backlog(node, deployment):
    pods = json.loads(setup.kube("get", "pods", "-A", "-o", "json", capture=True).stdout)["items"]
    pending = [pod for pod in pods if pod.get("status", {}).get("phase") == "Pending"
               and not pod["spec"].get("nodeName") and not pod["metadata"].get("deletionTimestamp")
               and all(node["metadata"].get("labels", {}).get(key) == value
                       for key, value in pod["spec"].get("nodeSelector", {}).items())]
    writer = deployment["spec"]["template"]["spec"]
    competing = [pod for pod in pending if not pending_without_reservation_risk(pod, writer)]
    if competing:
        names = [pod["metadata"]["namespace"] + "/" + pod["metadata"]["name"] for pod in competing]
        print("pending Pods may compete for PDS's reservations: " + ", ".join(names), flush=True)
        raise setup.SetupError("resolve the scheduling backlog before stopping PDS")
    if pending:
        # Even a zero-request Pod consumes a pod slot. Count assigned terminal Pods
        # conservatively, and leave an extra slot for the replacement writer.
        assigned = sum(pod["spec"].get("nodeName") == node["metadata"]["name"] for pod in pods)
        capacity = setup.quantity(node["status"]["allocatable"]["pods"])
        if assigned + len(pending) + 1 > capacity:
            raise setup.SetupError("insufficient pod slots to safely ignore zero-request pending Pods: " +
                                   "assigned=" + str(assigned) + " pending=" + str(len(pending)) +
                                   " capacity=" + str(capacity))
        names = [pod["metadata"]["namespace"] + "/" + pod["metadata"]["name"] for pod in pending]
        print("pending Pods cannot take CPU/memory reservations or exclude PDS; " +
              "pod-slot capacity verified: " + ", ".join(names), flush=True)


def capture_data(identifier):
    if inspect_artifacts(identifier):
        return
    if QUIESCENCE.exists() or QUIESCENCE.is_symlink():
        previous = saved(QUIESCENCE)
        check_quiescence_state(previous)
        if previous["backup_id"] != identifier and previous["phase"] != "resumed":
            raise setup.SetupError("an earlier PDS writer has not resumed; recover it before capturing")
        # A prior incomplete capture is preserved for inspection, never overwritten.
        if any(staging().iterdir()):
            raise setup.SetupError("an interrupted PDS capture is preserved; production is running")
    hpas = json.loads(setup.kube("get", "hpa", "-n", NS, "-o", "json", capture=True).stdout)["items"]
    if any(item["spec"].get("scaleTargetRef", {}).get("name") == "pds" for item in hpas):
        raise setup.SetupError("an autoscaler may restart PDS writers; no quiescence attempted")
    # Do not release the app's reservations into a competing scheduling backlog.
    node = setup.get_object("node", "openmediavault")
    deployment = setup.get_object("deployment", "pds", NS)
    if not deployment or deployment["spec"].get("replicas") != 1:
        raise setup.SetupError("PDS replica count changed before quiescence")
    check_scheduling_backlog(node, deployment)
    running_containers()  # Verify runtime inspection works before stopping the app.
    pods = active_claim_pods(CLAIM)
    if len(pods) != 1 or not setup.ready(pods[0]):
        raise setup.SetupError("expected one ready PDS writer before quiescence")
    application = capture_application_evidence()
    config, credentials = application_artifacts()
    state = {"backup_id": identifier, "phase": "quiescing", "original_replicas": 1,
             "deployment_uid": deployment["metadata"]["uid"],
             "template_hash": template_hash(deployment["spec"]["template"]), "started_at": now()}
    pending = new_pending(identifier)
    save(QUIESCENCE, state)
    try:
        print("briefly stopping PDS to capture its SQLite databases and blobs", flush=True)
        setup.kube("scale", "deployment/pds", "-n", NS, "--replicas=0", "--current-replicas=1",
                   "--resource-version=" + deployment["metadata"]["resourceVersion"])
        stopped_writers({pod["metadata"]["uid"] for pod in pods})
        setup.checked_live_directory()
        worker_budget()
        started = now()
        contents = data_archive(setup.LIVE, pending / "data.tar")
        artifact_write(pending / "contents.json", json.dumps(contents, indent=2) + "\n")
        integrity = sqlite_integrity(contents)
        artifact_write(pending / "sqlite-integrity.json", json.dumps(integrity, indent=2) + "\n")
        artifact_write(pending / "application.json", json.dumps(application, indent=2) + "\n")
        artifact_write(pending / "application-config.json", json.dumps(config, indent=2) + "\n")
        artifact_write(pending / "application-secret.json", json.dumps(credentials, indent=2) + "\n")
        artifact_write(pending / "metadata.json", json.dumps({
            "backup_id": identifier, "claim": CLAIM, "captured_at": started,
            "completed_at": now(), "method": "stopped writer; archive, per-file SHA-256, and SQLite integrity check",
            "image": setup.IMAGE, "file_count": sum(entry["type"] == "file" for entry in contents),
            "bytes": sum(entry.get("bytes", 0) for entry in contents),
            "service_did": application["describe"]["did"], "repo_count": application["repo_count"],
            "database_count": len(integrity["databases"]),
        }, indent=2) + "\n")
        artifact_write(pending / "backup-id", identifier + "\n")
        publish(identifier, pending)
        save(QUIESCENCE, {**state, "phase": "captured", "captured_at": now()})
    finally:
        # Repeated disconnect/termination signals must not interrupt recovery.
        previous_handlers = {number: signal.signal(number, signal.SIG_IGN)
                             for number in (signal.SIGHUP, signal.SIGINT, signal.SIGTERM)}
        try:
            resume_pds(state)
        finally:
            for number, handler in previous_handlers.items():
                signal.signal(number, handler)


def upload_complete(source, identifier):
    if not source:
        return False
    status = source.get("status", {})
    return (not source["spec"].get("paused") and source["spec"].get("trigger") == {"manual": identifier} and
            status.get("lastManualSync") == identifier and status.get("lastSyncTime") and
            not status.get("lastSyncStartTime") and status.get("latestMoverStatus", {}).get("result") == "Successful" and
            any(item.get("type") == "Synchronizing" and item.get("status") == "False" and
                item.get("reason") == "WaitingForManual" for item in status.get("conditions", [])))


def upload(identifier, *, evidence_prefix="initial"):
    if not inspect_artifacts(identifier):
        raise setup.SetupError("cannot upload without published artifacts")
    for attempt in range(5):
        source = setup.get_object("replicationsource", SOURCE, NS)
        if not source or source["spec"].get("trigger", {}).get("manual") not in (
                "controlled-by-backup-runner", identifier):
            raise setup.SetupError("VolSync source or generation changed before upload")
        if not source["spec"].get("paused"):
            break
        if active_claim_pods("data-backup-staging"):
            raise setup.SetupError("a producer still holds staging; no upload started")
        worker_budget()
        result = setup.kube("patch", "replicationsource", SOURCE, "-n", NS, "--type=merge", "-p",
                            json.dumps({"metadata": {"resourceVersion": source["metadata"]["resourceVersion"]},
                                        "spec": {"paused": False, "trigger": {"manual": identifier}}}),
                            capture=True, check=False)
        if not result.returncode:
            print(result.stdout, end="", flush=True)
            break
        if "(Conflict)" not in result.stderr:
            raise setup.SetupError("could not trigger the VolSync upload")
        time.sleep(0.2)
    else:
        raise setup.SetupError("VolSync source kept changing; staging is preserved")
    deadline = time.monotonic() + 900
    while time.monotonic() < deadline:
        source = setup.get_object("replicationsource", SOURCE, NS)
        if not source or source["spec"].get("trigger") != {"manual": identifier}:
            raise setup.SetupError("VolSync source or generation changed during upload")
        if upload_complete(source, identifier):
            save(setup.RECOVERY / (evidence_prefix + "-data-volsync-result.json"), source)
            print("PDS VolSync upload, retention, and cleanup completed", flush=True)
            return
        time.sleep(2)
    public = {key: source.get("status", {}).get(key) for key in (
        "conditions", "lastManualSync", "lastSyncTime", "lastSyncStartTime", "latestMoverStatus")}
    print(json.dumps(public, indent=2), flush=True)
    raise setup.SetupError("VolSync did not complete within 900 seconds; published files are preserved")


def verify_backup(identifier, *, evidence_prefix="initial", retention_proof=False):
    checksums = inspect_artifacts(identifier)
    if not checksums:
        raise setup.SetupError("cannot verify without published artifacts")
    worker_budget()
    service = setup.get_object("service", "restic", NS)
    ips = service["spec"].get("clusterIPs") or [service["spec"].get("clusterIP")]
    job = read_template(MANIFESTS / "verify-job.yaml")
    container = job["spec"]["template"]["spec"]["containers"][0]
    container["envFrom"] = [{"secretRef": {"name": "restic-" + CLAIM}}]
    container["env"] = [{"name": key, "value": value} for key, value in {
        "EXPECTED_RESTIC_IPS": ",".join(str(ipaddress.ip_address(value)) for value in ips),
        "EXPECTED_BACKUP_ID": identifier, "EXPECTED_SHA256SUMS": checksums,
        "EXPECTED_ARTIFACTS": json.dumps(sorted(ARTIFACTS)),
        "PROTECTED_CLAIM": CLAIM,
        "VERIFY_RETENTION_PROOF": "true" if retention_proof else "false",
    }.items()]
    created = json.loads(setup.kube("create", "-f", "-", "-o", "json", stdin=json.dumps(job), capture=True).stdout)
    name = created["metadata"]["name"]
    print("job/" + name + " created (PDS snapshot verification)", flush=True)
    try:
        setup.wait_for_job(name, timeout=900)
    finally:
        setup.print_check_logs(name)
    logs = setup.kube("logs", "-n", NS, "job/" + name, capture=True, sensitive=True).stdout
    results = [json.loads(line.removeprefix("BACKUP_VERIFIED_JSON=")) for line in logs.splitlines()
               if line.startswith("BACKUP_VERIFIED_JSON=")]
    if (len(results) != 1 or results[0].get("result") != "passed" or
            results[0].get("backup_id") != identifier or results[0].get("claim") != CLAIM or
            not re.fullmatch(r"[0-9a-f]{64}", results[0].get("snapshot", ""))):
        raise setup.SetupError("completed verification Job did not report the expected backup")
    result = {**results[0], "completed_at": now(), "checksums": checksums, "verification_job": name}
    if retention_proof:
        line = next((line for line in logs.splitlines() if line.startswith("scratch retention proof passed:")), None)
        if not line:
            raise setup.SetupError("the scratch retention proof did not complete")
        result["retention_proof"] = line
    save(setup.RECOVERY / (evidence_prefix + "-data-backup-result.json"), result)
    return result


def apply_sources(identifier):
    rendered = setup.kube("create", "--dry-run=client", "-k", str(MANIFESTS), "-o", "json",
                          capture=True).stdout.lstrip()
    decoder = json.JSONDecoder()
    resources = []
    while rendered:
        resource, end = decoder.raw_decode(rendered)
        resources.extend(resource.get("items", [resource]))
        rendered = rendered[end:].lstrip()
    if len(resources) != 1 or resources[0].get("kind") != "ReplicationSource" or \
            resources[0]["metadata"].get("name") != SOURCE:
        raise setup.SetupError("unexpected PDS source/NetworkPolicy bundle")
    resource = resources[0]
    existing = setup.get_object("replicationsource", SOURCE, NS)
    if existing:
        managed(existing)
        expected = {key: value for key, value in resource["spec"].items() if key not in ("paused", "trigger")}
        if (not contains_fields(existing["spec"], expected) or
                existing["spec"].get("trigger") not in ({"manual": "controlled-by-backup-runner"},
                                                        {"manual": identifier})):
            raise setup.SetupError("existing VolSync policy or generation differs")
        if (not existing["spec"].get("paused") and
                existing["spec"]["trigger"]["manual"] == "controlled-by-backup-runner"):
            raise setup.SetupError("an initial source was unexpectedly unpaused")
        return  # Preserve an upload already started by this generation.
    setup.kube("apply", "-f", "-", stdin=json.dumps(resource))


def check_binding(claim_name, volume_name, path, *, required=True):
    claim = setup.get_object("pvc", claim_name, NS)
    if not claim:
        if required:
            raise setup.SetupError("backup storage claim is missing: " + claim_name)
        return
    volume = setup.get_object("pv", volume_name)
    if (not volume or claim.get("status", {}).get("phase") != "Bound" or
            claim["spec"].get("volumeName") != volume_name or
            volume["spec"].get("claimRef", {}).get("uid") != claim["metadata"]["uid"] or
            volume["spec"].get("local", {}).get("path") != str(path) or
            volume["spec"].get("persistentVolumeReclaimPolicy") != "Retain"):
        raise setup.SetupError("backup storage binding differs: " + claim_name)


def preflight():
    proof = saved(setup.RECOVERY / "endpoint-verification.json")
    if proof.get("result") != "passed" or proof.get("repositories") != [CLAIM]:
        raise setup.SetupError("the backup endpoint has not been verified")
    setup.live_preflight(check_resources=False)
    server = setup.get_object("deployment", "restic-server", NS)
    if not server or server.get("status", {}).get("availableReplicas", 0) != 1:
        raise setup.SetupError("the backup endpoint is not available")
    credentials = saved(setup.RECOVERY / "credentials.json")
    setup.ensure_secret("restic-" + CLAIM, setup.credential_fields(CLAIM, credentials["repositories"][CLAIM]))
    setup.save_secret("restic-" + CLAIM)
    setup.save_secret("restic-root-ca")
    check_binding("data-backup-staging", "pds-data-backup-staging", staging())
    check_binding("volsync-src-data-backup-cache", "pds-data-backup-cache",
                  setup.BASE / "backup-cache" / CLAIM / "source", required=False)
    for path in (staging(), setup.BASE / "backups" / CLAIM,
                 setup.BASE / "backup-cache" / CLAIM / "source"):
        setup.checked_directory(path)
        if not path.is_dir():
            raise setup.SetupError("an approved backup directory is missing")
    if shutil.disk_usage(staging()).free < 20 * 1024**3:
        raise setup.SetupError("less than 20 GiB remains on the backup array")


def backup():
    # A durable journal permits restoring the writer after SIGKILL or host loss.
    if QUIESCENCE.exists() or QUIESCENCE.is_symlink():
        state = saved(QUIESCENCE)
        check_quiescence_state(state)
        if state["phase"] != "resumed":
            resume_pds(state)
    preflight()
    if not STATE.exists() and not STATE.is_symlink():
        save(STATE, {"backup_id": "initial-" + secrets.token_hex(16), "started_at": now()})
    identifier = saved(STATE).get("backup_id")
    if not isinstance(identifier, str) or not re.fullmatch(r"initial-[0-9a-f]{32}", identifier):
        raise setup.SetupError("saved initial backup generation is invalid")
    apply_sources(identifier)
    capture_data(identifier)
    upload(identifier)
    verify_backup(identifier)
    setup.live_preflight(check_resources=False)
    print("PDS initial backup verified; isolated restore and daily scheduling remain pending", flush=True)


def interrupted(number, frame):
    raise setup.SetupError("backup interrupted by signal " + str(number))


def main():
    if os.geteuid() != 0:
        raise setup.SetupError("run with sudo python3 on OMV")
    os.umask(0o077)
    os.environ["PATH"] = "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
    setup.recovery_directory()
    for number in (signal.SIGHUP, signal.SIGINT, signal.SIGTERM):
        signal.signal(number, interrupted)
    with setup.operation_lock(setup.RECOVERY / ".setup.lock", create=True):
        with setup.operation_lock(setup.COORDINATION_LOCK, create=False):
            print("pds backup worker serialized with the existing namespace backup workers", flush=True)
            backup()


if __name__ == "__main__":
    try:
        main()
    except (setup.SetupError, OSError, ValueError, KeyError, TypeError, subprocess.TimeoutExpired) as error:
        print("backup stopped: " + str(error), file=sys.stderr)
        sys.exit(1)
