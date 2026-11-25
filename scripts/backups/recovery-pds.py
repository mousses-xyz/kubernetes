#!/usr/bin/env python3
"""Export the verified PDS recovery bundle on OMV; verify an encrypted copy off OMV."""

import argparse
import base64
from datetime import datetime, timezone
import fcntl
import hashlib
import io
import json
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import sys
import tarfile


NAMESPACE = "pds"
CLAIM = "pds-data"
RECOVERY = Path("/root/k3s-backup-recovery") / NAMESPACE
BASE = Path("/filesystem/k3s/data/pds-backups")
STAGING = BASE / "backup-staging" / CLAIM
SERVICE_DID = "did:web:bluesky.mousses.xyz"
IMAGE = "ghcr.io/bluesky-social/pds@sha256:20db9d828e9096978a907fe134125cdb26a00417aef6376920204bbfa060ab5c"
ARTIFACTS = {
    "data.tar", "contents.json", "application.json", "application-config.json",
    "application-secret.json", "sqlite-integrity.json", "metadata.json", "backup-id",
    "SHA256SUMS",
}
SMALL_ARTIFACTS = ARTIFACTS - {"data.tar"}
FILES = (
    "credentials.json", "ca.crt", "endpoint-verification.json",
    "restic-pds-data.secret.json", "restic-server-auth.secret.json",
    "restic-server-tls.secret.json", "restic-root-ca.secret.json",
    "pds-env.secret.json", "pds.deployment.json", "pds-data.pvc.json", "pds-data.pv.json",
    "initial-backup.json", "initial-data-volsync-result.json", "initial-data-backup-result.json",
    "initial-restore.json", "initial-data-volsync-restore-result.json",
    "initial-data-restore-result.json", "initial-data-smoke-result.json",
    "latest-application.json", "latest-daily-backup.json", "data-quiescence.json",
    "daily-schedule.json", "retention-proof.json",
)
MANIFEST = "recovery-manifest.json"
MAX_RECOVERY_FILE = 32 * 1024**2
MAX_SMALL_ARTIFACT = 32 * 1024**2
MAX_DATA_ARTIFACT = 64 * 1024**3
MAX_RECOVERY_CONTENT = 64 * 1024**2


class RecoveryError(Exception):
    pass


def require(condition, message):
    if not condition:
        raise RecoveryError(message)


def unique_object(pairs):
    result = {}
    for key, value in pairs:
        require(key not in result, "recovery JSON contains duplicate keys")
        result[key] = value
    return result


def document(files, name):
    value = json.loads(files[name], object_pairs_hook=unique_object)
    require(isinstance(value, dict), "expected a recovery JSON object")
    return value


def artifact_document(artifacts, name):
    value = json.loads(artifacts[name], object_pairs_hook=unique_object)
    require(isinstance(value, dict), "expected an artifact JSON object")
    return value


def identifier(value, prefix):
    require(isinstance(value, str) and re.fullmatch(prefix + r"-[0-9a-f]{32}", value),
            "saved backup generation is invalid")
    return value


def timestamp(value):
    require(isinstance(value, str) and datetime.fromisoformat(value.replace("Z", "+00:00")).tzinfo is not None,
            "saved evidence timestamp is invalid")


def digest(value):
    require(isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value),
            "saved evidence checksum is invalid")
    return value


def checksums(value):
    require(isinstance(value, str), "saved artifact checksums are missing")
    result = {}
    for line in value.splitlines():
        match = re.fullmatch(r"([0-9a-f]{64})  ([a-zA-Z0-9.-]+)", line)
        require(match and match[2] not in result, "saved artifact checksums are invalid")
        result[match[2]] = match[1]
    require(set(result) == ARTIFACTS - {"SHA256SUMS"}, "saved artifact checksum set differs")
    return result


def daily_files(generation):
    identifier(generation, "daily")
    return {generation + "-data-volsync-result.json", generation + "-data-backup-result.json"}


def expected_files(files):
    require(set(FILES) <= set(files), "the recovery file set is incomplete")
    generation = identifier(document(files, "latest-daily-backup.json").get("backup_id"), "daily")
    return set(FILES) | daily_files(generation)


def resource(files, filename, kind, name, api="v1", *, namespaced=True):
    value = document(files, filename)
    require(value.get("apiVersion") == api and value.get("kind") == kind and
            value["metadata"].get("name") == name and
            (not namespaced or value.get("metadata", {}).get("namespace") == NAMESPACE),
            "saved resource identity differs")
    return value


def secret(files, name, filename=None):
    data = resource(files, filename or name + ".secret.json", "Secret", name).get("data")
    require(isinstance(data, dict) and data and
            all(isinstance(key, str) and isinstance(value, str) for key, value in data.items()),
            "saved Secret data is missing or invalid")
    return {key: base64.b64decode(value, validate=True) for key, value in data.items()}


def completed(files, filename, kind, name, generation, *, restore_as_of=None):
    value = resource(files, filename, kind, name, "volsync.backube/v1alpha1")
    spec, status = value.get("spec", {}), value.get("status", {})
    restic = spec.get("restic", {})
    require(spec.get("trigger") == {"manual": generation} and not spec.get("paused") and
            status.get("lastManualSync") == generation and status.get("lastSyncTime") and
            not status.get("lastSyncStartTime") and
            status.get("latestMoverStatus", {}).get("result") == "Successful" and
            any(item.get("type") == "Synchronizing" and item.get("status") == "False" and
                item.get("reason") == "WaitingForManual" for item in status.get("conditions", [])),
            "saved VolSync completion is incomplete")
    timestamp(status["lastSyncTime"])
    require(restic.get("copyMethod") == "Direct" and
            not restic.get("cleanupCachePVC") and not restic.get("enableFileDeletion"),
            "saved VolSync copy or cleanup policy differs")
    if kind == "ReplicationSource":
        ca = restic.get("customCA", {})
        require(spec.get("sourcePVC") == "data-backup-staging" and
                restic.get("repository") == "restic-pds-data" and
                restic.get("retain", {}).get("within") == "7d" and
                ca.get("secretName") == "restic-root-ca" and ca.get("key") == "tls.crt",
                "saved upload source, retention, or CA differs")
    else:
        ca = restic.get("customCA", {})
        require(restic.get("destinationPVC") == "data-restore-artifacts" and
                restic.get("repository") == "restic-restore-pds-data" and
                ca.get("secretName") == "restic-restore-ca" and ca.get("key") == "ca.crt" and
                restic.get("restoreAsOf") == restore_as_of,
                "saved artifact restore target, CA, or cutoff differs")
    return value


def validate_contents(value):
    contents = json.loads(value, object_pairs_hook=unique_object)
    require(isinstance(contents, list) and contents, "restored contents inventory is empty")
    result = {}
    for entry in contents:
        require(isinstance(entry, dict) and isinstance(entry.get("path"), str) and
                entry["path"] not in result and entry.get("type") in ("file", "directory"),
                "restored contents inventory is invalid")
        path = entry["path"]
        require(path == "." or (not path.startswith("/") and ".." not in Path(path).parts and
                                "" not in Path(path).parts),
                "restored contents inventory has an unsafe path")
        require(isinstance(entry.get("uid"), int) and entry["uid"] >= 0 and
                isinstance(entry.get("gid"), int) and entry["gid"] >= 0 and
                isinstance(entry.get("mode"), int) and 0 <= entry["mode"] <= 0o7777 and
                isinstance(entry.get("mtime_ns"), int), "restored contents metadata is invalid")
        if entry["type"] == "file":
            require(isinstance(entry.get("bytes"), int) and entry["bytes"] >= 0 and
                    isinstance(entry.get("sha256"), str) and re.fullmatch(r"[0-9a-f]{64}", entry["sha256"]),
                    "restored file inventory is invalid")
        else:
            require("sha256" not in entry and "bytes" not in entry, "restored directory inventory is invalid")
        result[path] = entry
    require(result.get(".") is not None and result["."]["type"] == "directory",
            "restored contents has no root directory")
    return result


def validate_artifacts(artifacts, data_sha256, data_size):
    require(set(artifacts) == SMALL_ARTIFACTS, "the artifact set is incomplete")
    require(data_size > 0 and data_size <= MAX_DATA_ARTIFACT, "the data archive size is invalid")
    sums = checksums(artifacts["SHA256SUMS"].decode())
    for name in SMALL_ARTIFACTS - {"SHA256SUMS"}:
        require(len(artifacts[name]) > 0 and len(artifacts[name]) <= MAX_SMALL_ARTIFACT and
                hashlib.sha256(artifacts[name]).hexdigest() == sums[name],
                "a recovery artifact checksum failed: " + name)
    require(digest(data_sha256) == sums["data.tar"], "the data archive checksum failed")
    contents = validate_contents(artifacts["contents.json"])
    integrity = artifact_document(artifacts, "sqlite-integrity.json")
    application = artifact_document(artifacts, "application.json")
    metadata = artifact_document(artifacts, "metadata.json")
    require(isinstance(integrity.get("databases"), dict), "SQLite integrity evidence is missing")
    health, describe = application.get("health"), application.get("describe")
    dids = application.get("repo_dids")
    require(set(application) == {"captured_at", "health", "describe", "repo_count", "repo_dids"} and
            isinstance(health, dict) and isinstance(health.get("version"), str) and
            re.fullmatch(r"[0-9A-Za-z.+_-]{1,64}", health["version"]) and
            isinstance(describe, dict) and describe.get("did") == SERVICE_DID and
            isinstance(describe.get("availableUserDomains"), list) and describe["availableUserDomains"] and
            all(isinstance(value, str) and value for value in describe["availableUserDomains"]) and
            type(application.get("repo_count")) is int and application["repo_count"] >= 1 and
            isinstance(dids, list) and len(dids) == application["repo_count"] and
            all(isinstance(value, str) and value for value in dids) and
            dids == sorted(dids) and len(dids) == len(set(dids)),
            "application identity evidence is invalid")
    timestamp(application["captured_at"])
    files = {path: entry for path, entry in contents.items() if entry["type"] == "file"}
    databases = {path for path in files if path.endswith(".sqlite") or path.endswith(".sqlite3")}
    require(set(integrity["databases"]) == databases, "SQLite integrity evidence covers a different file set")
    for path, evidence in integrity["databases"].items():
        require(evidence.get("integrity_check") == "ok" and evidence.get("sha256") == files[path]["sha256"],
                "SQLite integrity evidence differs: " + path)
    require(metadata.get("claim") == CLAIM and metadata.get("image") == IMAGE and
            metadata.get("service_did") == application["describe"]["did"] and
            metadata.get("repo_count") == application["repo_count"] and
            metadata.get("file_count") == len(files) and
            metadata.get("database_count") == len(databases) and
            metadata.get("bytes") == sum(entry["bytes"] for entry in files.values()),
            "backup metadata differs from the captured application or contents")
    identifier(metadata.get("backup_id"), "daily")
    timestamp(metadata.get("captured_at"))
    timestamp(metadata.get("completed_at"))
    return {"contents": contents, "integrity": integrity, "application": application, "metadata": metadata, "sums": sums}


def validate_files(files, artifacts, data_sha256, data_size):
    require(set(files) == expected_files(files), "the recovery file set is incomplete or unexpected")
    require(all(isinstance(value, bytes) and 0 < len(value) <= MAX_RECOVERY_FILE for value in files.values()) and
            sum(map(len, files.values())) <= MAX_RECOVERY_CONTENT, "recovery contents exceed the size limits")
    evidence = validate_artifacts(artifacts, data_sha256, data_size)
    application, metadata = evidence["application"], evidence["metadata"]
    generation = identifier(document(files, "latest-daily-backup.json").get("backup_id"), "daily")
    initial = identifier(document(files, "initial-backup.json").get("backup_id"), "initial")
    require(metadata.get("backup_id") == generation, "the staged artifacts belong to a different generation")

    credentials = document(files, "credentials.json")
    entries = credentials.get("repositories", {})
    require(set(credentials) == {"namespace", "repositories"} and credentials["namespace"] == NAMESPACE and
            isinstance(entries, dict) and set(entries) == {CLAIM}, "saved repository identities differ")
    entry = entries[CLAIM]
    require(isinstance(entry, dict) and set(entry) == {
        "RESTIC_REPOSITORY", "RESTIC_PASSWORD", "RESTIC_REST_USERNAME", "RESTIC_REST_PASSWORD", "htpasswd"
    } and all(isinstance(value, str) and value for value in entry.values()) and
        entry["RESTIC_REPOSITORY"] == "rest:https://restic.pds.svc:8000/" + CLAIM + "/" and
        entry["RESTIC_REST_USERNAME"] == CLAIM and entry["htpasswd"].startswith(CLAIM + ":") and
        entry["htpasswd"].endswith("\n") and entry["htpasswd"].count("\n") == 1,
        "saved repository recovery credentials are incomplete or differ")
    fields = {key: value.encode() for key, value in entry.items() if key != "htpasswd"}
    require(secret(files, "restic-" + CLAIM) == fields, "saved repository Secret differs from its credentials")
    require(secret(files, "restic-server-auth") == {".htpasswd": entry["htpasswd"].encode()},
            "saved server authentication differs from the repository credentials")
    root_ca = secret(files, "restic-root-ca")
    require(root_ca.get("tls.crt") == files["ca.crt"] and root_ca.get("tls.key") and
            files["ca.crt"].startswith(b"-----BEGIN CERTIFICATE-----"), "saved CA credentials are missing or differ")
    tls = secret(files, "restic-server-tls")
    require(tls.get("tls.crt") and tls.get("tls.key"), "saved server TLS credentials are missing")

    pds_env = secret(files, "pds-env")
    for key in ("PDS_HOSTNAME", "PDS_JWT_SECRET", "PDS_ADMIN_PASSWORD",
                "PDS_PLC_ROTATION_KEY_K256_PRIVATE_KEY_HEX"):
        require(pds_env.get(key), "saved PDS environment is missing: " + key)
    require(pds_env["PDS_HOSTNAME"] == SERVICE_DID.removeprefix("did:web:").encode(),
            "saved PDS hostname differs from the service DID")
    deployment = resource(files, "pds.deployment.json", "Deployment", "pds", "apps/v1")
    spec = deployment.get("spec", {})
    containers = [item for item in spec.get("template", {}).get("spec", {}).get("containers", [])
                  if item.get("name") == "pds"]
    require(len(containers) == 1 and containers[0].get("image") == IMAGE and
            containers[0].get("envFrom") == [{"secretRef": {"name": "pds-env"}}],
            "saved PDS Deployment image or environment differs")
    claim = resource(files, "pds-data.pvc.json", "PersistentVolumeClaim", CLAIM)
    volume = resource(files, "pds-data.pv.json", "PersistentVolume", CLAIM, namespaced=False)
    require(claim["spec"].get("volumeName") == CLAIM and
            volume["spec"].get("local", {}).get("path") == "/filesystem/k3s/data/pds" and
            volume["spec"].get("persistentVolumeReclaimPolicy") == "Retain",
            "saved live PVC/PV identity differs")
    endpoint = document(files, "endpoint-verification.json")
    require(endpoint.get("result") == "passed" and endpoint.get("namespace") == NAMESPACE and
            endpoint.get("repositories") == [CLAIM], "saved endpoint verification is incomplete")

    initial_result = document(files, "initial-data-backup-result.json")
    initial_sums = checksums(initial_result.get("checksums"))
    require(initial_result.get("result") == "passed" and initial_result.get("backup_id") == initial and
            initial_result.get("claim") == CLAIM and isinstance(initial_result.get("verification_job"), str) and
            initial_result["verification_job"] and digest(initial_result.get("snapshot")),
            "saved initial backup verification is incomplete")
    timestamp(initial_result.get("completed_at"))
    source = completed(files, "initial-data-volsync-result.json", "ReplicationSource",
                       "data-backup", initial)
    restore = document(files, "initial-restore.json")
    require(restore.get("backup_id") == initial and restore.get("snapshot") == initial_result["snapshot"] and
            restore.get("checksums") == initial_result["checksums"] and
            restore.get("restore_as_of") == source["status"]["lastSyncTime"],
            "saved restore generation differs from the initial upload")
    destination = completed(files, "initial-data-volsync-restore-result.json", "ReplicationDestination",
                            "data-restore-initial", initial, restore_as_of=restore["restore_as_of"])
    require(destination["spec"]["restic"].get("restoreAsOf") == restore["restore_as_of"],
            "saved restore cutoff differs")
    restored = document(files, "initial-data-restore-result.json")
    require(restored.get("result") == "passed" and restored.get("backup_id") == initial and
            restored.get("claim") == CLAIM and restored.get("snapshot") == initial_result["snapshot"] and
            restored.get("archive_metadata_verified") is True and
            all(type(restored.get(key)) is int and restored[key] >= 0
                for key in ("files", "directories", "sqlite_databases")) and
            restored.get("service_did") == application["describe"]["did"] and
            restored.get("repo_count") == application["repo_count"],
            "saved isolated restore verification differs")
    smoke = document(files, "initial-data-smoke-result.json")
    require(smoke.get("result") == "passed" and smoke.get("backup_id") == initial and
            smoke.get("claim") == CLAIM and smoke.get("snapshot") == initial_result["snapshot"] and
            smoke.get("network_isolated") is True and
            isinstance(smoke.get("health"), dict) and
            smoke["health"].get("version") == application["health"].get("version") and
            isinstance(smoke.get("describe"), dict) and smoke["describe"].get("did") == SERVICE_DID and
            smoke.get("repo_count") == application["repo_count"] and
            smoke.get("repo_dids") == application["repo_dids"],
            "saved isolated PDS smoke test differs")
    require(artifact_document(artifacts, "application-secret.json")["data"] ==
            {key: base64.b64encode(value).decode() for key, value in pds_env.items()},
            "saved PDS environment differs from the backed-up artifact")
    require(artifact_document(artifacts, "application-config.json") == deployment,
            "saved PDS Deployment differs from the backed-up artifact")
    require(document(files, "latest-application.json") == application,
            "saved latest application evidence differs from the artifact")

    daily = document(files, "latest-daily-backup.json")
    require(daily.get("result") == "passed" and daily.get("initial_backup_id") == initial and
            daily.get("retention") == "within 7d" and
            set(daily.get("components", {})) == {"data"}, "saved daily backup verification is incomplete")
    timestamp(daily.get("completed_at"))
    receipt = daily["components"]["data"]
    require(receipt == document(files, generation + "-data-backup-result.json"),
            "daily backup receipts disagree")
    daily_sums = checksums(receipt.get("checksums"))
    require(receipt.get("result") == "passed" and receipt.get("backup_id") == generation and
            receipt.get("claim") == CLAIM and digest(receipt.get("snapshot")) and
            isinstance(receipt.get("verification_job"), str) and receipt["verification_job"],
            "daily backup verification is incomplete")
    require(daily_sums == evidence["sums"], "daily receipt checksums differ from the published artifacts")
    completed(files, generation + "-data-volsync-result.json", "ReplicationSource", "data-backup", generation)
    previous = daily.get("previous_id")
    require(isinstance(previous, str) and re.fullmatch(r"(?:initial|daily)-[0-9a-f]{32}", previous) and
            previous != generation, "saved daily predecessor differs")
    previous_sums = checksums(daily.get("previous_checksums"))
    require(previous != initial or previous_sums == initial_sums,
            "saved daily predecessor checksums differ from the initial upload")

    journal = document(files, "data-quiescence.json")
    require(journal.get("backup_id") == generation and journal.get("phase") == "resumed" and
            journal.get("original_replicas") == 1 and isinstance(journal.get("deployment_uid"), str) and
            journal["deployment_uid"] and digest(journal.get("template_hash")),
            "the latest PDS writer has not resumed")
    schedule = document(files, "daily-schedule.json")
    first_daily = identifier(schedule.get("first_verified_daily_generation"), "daily")
    schedule_snapshots = schedule.get("snapshots")
    require(schedule.get("result") == "enabled" and schedule.get("unit") == "k3s-pds-backup.timer" and
            schedule.get("schedule_utc") == "*-*-* 13:00:00 UTC" and
            schedule.get("schedule_arizona") == "06:00 daily" and schedule.get("persistent") is True and
            isinstance(schedule.get("revision"), str) and
            re.fullmatch(r"[0-9a-f]{40}", schedule["revision"]) and
            schedule.get("recovery_unit") == "k3s-pds-backup-recovery.service" and
            schedule.get("recovery_at_boot") is True and
            isinstance(schedule_snapshots, dict) and set(schedule_snapshots) == {"data"} and
            all(isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value)
                for value in schedule_snapshots.values()) and
            (first_daily != generation or schedule_snapshots == {"data": receipt["snapshot"]}),
            "saved daily schedule or writer recovery verification is incomplete")
    proof = document(files, "retention-proof.json")
    require(proof.get("result") == "passed" and proof.get("retention") == "within 7d" and
            isinstance(proof.get("evidence"), str) and re.fullmatch(
                r"scratch retention proof passed: 9-day snapshot expired; 6-day and current retained; "
                r"[1-9][0-9]* bytes reclaimed", proof["evidence"]),
            "saved retention and pruning proof is incomplete")
    return {
        "initial_backup_id": initial, "initial_snapshot": initial_result["snapshot"],
        "initial_checksums": initial_sums, "daily_backup_id": generation,
        "daily_snapshot": receipt["snapshot"], "daily_checksums": daily_sums,
        "service_did": application["describe"]["did"], "repo_count": application["repo_count"],
        "files": len(evidence["contents"]) - 1,
        "databases": len(evidence["integrity"]["databases"]),
    }


def private_bytes(path, owner, *, maximum=MAX_RECOVERY_FILE):
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except OSError as error:
        raise RecoveryError("could not open a private recovery file: " + path.name) from error
    with os.fdopen(descriptor, "rb") as content:
        attributes = os.fstat(content.fileno())
        require(stat.S_ISREG(attributes.st_mode) and attributes.st_nlink == 1 and
                attributes.st_uid == owner and stat.S_IMODE(attributes.st_mode) == 0o600,
                "expected a private regular file: " + path.name)
        require(0 < attributes.st_size <= maximum, "private file is empty or too large: " + path.name)
        value = content.read(maximum + 1)
        after = os.fstat(content.fileno())
        require((attributes.st_size, attributes.st_mtime_ns, attributes.st_ctime_ns) ==
                (after.st_size, after.st_mtime_ns, after.st_ctime_ns) and len(value) == attributes.st_size,
                "private file changed while reading: " + path.name)
        return value


class HashingReader:
    def __init__(self, source):
        self.source = source
        self.checksum = hashlib.sha256()
        self.bytes = 0

    def read(self, count):
        data = self.source.read(count)
        self.checksum.update(data)
        self.bytes += len(data)
        return data


def open_data_archive(path, *, maximum):
    """Hold the verified archive open so a daily run cannot replace it mid-export."""
    attributes = path.lstat()
    require(stat.S_ISREG(attributes.st_mode) and attributes.st_nlink == 1 and
            0 < attributes.st_size <= maximum, "data archive has an unexpected file type or size")
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        opened = os.fstat(descriptor)
        require((opened.st_ino, opened.st_dev) == (attributes.st_ino, attributes.st_dev),
                "data archive changed while opening it")
        checksum = hashlib.sha256()
        while chunk := os.read(descriptor, 4 * 1024 * 1024):
            checksum.update(chunk)
        after = os.fstat(descriptor)
        require((after.st_size, after.st_mtime_ns, after.st_ctime_ns) ==
                (opened.st_size, opened.st_mtime_ns, opened.st_ctime_ns),
                "data archive changed while hashing it")
        os.lseek(descriptor, 0, os.SEEK_SET)
        return descriptor, checksum.hexdigest(), opened.st_size
    except BaseException:
        os.close(descriptor)
        raise


def staged_artifacts(generation):
    current = STAGING / "current"
    require(current.is_dir() and not current.is_symlink(), "the published PDS artifact directory is missing")
    root = STAGING
    require({path.name for path in root.iterdir()} == {"current"}, "unexpected staging data exists")
    attributes = current.stat()
    require((attributes.st_uid, attributes.st_gid, stat.S_IMODE(attributes.st_mode)) == (999, 999, 0o700),
            "the published PDS artifact directory ownership or mode changed")
    require({path.name for path in current.iterdir()} == ARTIFACTS, "the published PDS artifact set is incomplete")
    artifacts = {}
    for path in current.iterdir():
        attributes = path.lstat()
        require(stat.S_ISREG(attributes.st_mode) and attributes.st_nlink == 1 and
                (attributes.st_uid, attributes.st_gid, stat.S_IMODE(attributes.st_mode)) == (999, 999, 0o600),
                "a published PDS artifact ownership or type changed: " + path.name)
        if path.name in SMALL_ARTIFACTS:
            require(attributes.st_size <= MAX_SMALL_ARTIFACT, "a published PDS artifact is too large: " + path.name)
            artifacts[path.name] = private_bytes(path, 999, maximum=MAX_SMALL_ARTIFACT)
    require((current / "backup-id").read_text().strip() == generation,
            "the published PDS artifacts belong to a different generation")
    descriptor, data_sha256, data_size = open_data_archive(
        current / "data.tar", maximum=MAX_DATA_ARTIFACT)
    try:
        validate_artifacts(artifacts, data_sha256, data_size)
    except BaseException:
        os.close(descriptor)
        raise
    return artifacts, data_sha256, data_size, descriptor


def add_bytes(archive, name, value, members):
    information = tarfile.TarInfo(name)
    information.size = len(value)
    information.mode = 0o600
    information.uid = information.gid = 0
    information.uname = information.gname = ""
    information.mtime = int(datetime.now(timezone.utc).timestamp())
    members[name.removeprefix("pds/")] = {
        "sha256": hashlib.sha256(value).hexdigest(), "bytes": len(value)}
    archive.addfile(information, io.BytesIO(value))


def add_stream(archive, name, descriptor, members, expected_sha256, expected_size):
    attributes = os.fstat(descriptor)
    require(stat.S_ISREG(attributes.st_mode) and attributes.st_nlink == 1 and
            attributes.st_size == expected_size and 0 < attributes.st_size <= MAX_DATA_ARTIFACT,
            "data archive changed before archiving")
    information = tarfile.TarInfo(name)
    information.size = attributes.st_size
    information.mode = 0o600
    information.uid = information.gid = 0
    information.uname = information.gname = ""
    information.mtime = int(attributes.st_mtime)
    with os.fdopen(os.dup(descriptor), "rb") as source:
        opened = os.fstat(source.fileno())
        require((opened.st_ino, opened.st_dev) == (attributes.st_ino, attributes.st_dev),
                "data archive changed while opening it")
        reader = HashingReader(source)
        archive.addfile(information, reader)
        after = os.fstat(source.fileno())
        require(reader.bytes == attributes.st_size and reader.checksum.hexdigest() == expected_sha256 and
                (after.st_size, after.st_mtime_ns, after.st_ctime_ns) ==
                (opened.st_size, opened.st_mtime_ns, opened.st_ctime_ns),
                "data archive changed while archiving it")
    members["artifacts/data.tar"] = {"sha256": expected_sha256, "bytes": attributes.st_size}


def make_archive(files, artifacts, data_sha256, data_size, descriptor, recipient):
    try:
        evidence = validate_files(files, artifacts, data_sha256, data_size)
        members = {}
        now = datetime.now(timezone.utc).isoformat()
        with tarfile.open(fileobj=sys.stdout.buffer, mode="w|", format=tarfile.GNU_FORMAT) as archive:
            for name in sorted(files):
                add_bytes(archive, "pds/recovery/" + name, files[name], members)
            for name in sorted(SMALL_ARTIFACTS):
                add_bytes(archive, "pds/artifacts/" + name, artifacts[name], members)
            add_stream(archive, "pds/artifacts/data.tar", descriptor,
                       members, data_sha256, data_size)
            manifest = {
                "version": 1, "namespace": NAMESPACE, "claim": CLAIM, "recipient": recipient,
                "exported_at": now, "members": members, **evidence,
            }
            value = (json.dumps(manifest, indent=2) + "\n").encode()
            information = tarfile.TarInfo("pds/" + MANIFEST)
            information.size = len(value)
            information.mode = 0o600
            information.uid = information.gid = 0
            information.uname = information.gname = ""
            information.mtime = int(datetime.now(timezone.utc).timestamp())
            archive.addfile(information, io.BytesIO(value))
        sys.stdout.buffer.flush()
        print("private recovery archive prepared: " + str(len(members)) + " files; " +
              str(data_size) + " byte data archive", file=sys.stderr)
    finally:
        os.close(descriptor)


def collect():
    require(os.geteuid() == 0, "export requires sudo python3")
    require(not sys.stdout.isatty(), "export must be piped directly into age encryption")
    current = Path("/root")
    for component in RECOVERY.relative_to(current).parts:
        current /= component
        attributes = current.lstat()
        require(stat.S_ISDIR(attributes.st_mode) and attributes.st_uid == 0 and
                stat.S_IMODE(attributes.st_mode) == 0o700,
                "recovery directory must be private and root-owned")
    descriptor = os.open(RECOVERY / ".setup.lock", os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(descriptor, "rb") as lock:
        attributes = os.fstat(lock.fileno())
        require(stat.S_ISREG(attributes.st_mode) and attributes.st_nlink == 1 and attributes.st_uid == 0 and
                stat.S_IMODE(attributes.st_mode) == 0o600,
                "namespace lock is not private and root-owned")
        try:
            fcntl.flock(lock, fcntl.LOCK_SH | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise RecoveryError("a pds operation is running; retry after it finishes") from error
        require(not os.path.lexists(RECOVERY / "daily-active.json"),
                "an unfinished daily run remains; finish it before exporting")
        files = {name: private_bytes(RECOVERY / name, 0) for name in FILES}
        generation = identifier(document(files, "latest-daily-backup.json").get("backup_id"), "daily")
        files.update({name: private_bytes(RECOVERY / name, 0) for name in daily_files(generation)})
        artifacts, data_sha256, data_size, descriptor = staged_artifacts(generation)
    return files, artifacts, data_sha256, data_size, descriptor


def export(recipient):
    files, artifacts, data_sha256, data_size, descriptor = collect()
    make_archive(files, artifacts, data_sha256, data_size, descriptor, recipient)


def verify_stream(stream, recipient):
    files, artifacts, members = {}, {}, {}
    manifest = None
    data_sha256 = data_size = None
    manifest_seen = False
    with tarfile.open(fileobj=stream, mode="r|") as archive:
        for member in archive:
            require(not manifest_seen, "the recovery manifest is not the final archive member")
            require(member.isfile() and not member.pax_headers and member.mode == 0o600 and
                    member.uid == 0 and member.gid == 0 and not member.uname and not member.gname and
                    0 < member.size <= MAX_DATA_ARTIFACT, "the recovery archive contains an unexpected entry")
            name = member.name
            require(name.startswith("pds/"), "the recovery archive contains an unexpected path")
            if name == "pds/" + MANIFEST:
                require(0 < member.size <= MAX_RECOVERY_FILE, "the recovery manifest is too large")
                content = archive.extractfile(member)
                require(content is not None, "the recovery manifest is truncated")
                manifest = json.loads(content.read(member.size + 1), object_pairs_hook=unique_object)
                require(isinstance(manifest, dict), "the recovery manifest is invalid")
                manifest_seen = True
                continue
            if name.startswith("pds/recovery/"):
                suffix = name.removeprefix("pds/recovery/")
                require((suffix in FILES or re.fullmatch(
                            r"daily-[0-9a-f]{32}-data-(?:volsync-result|backup-result)\.json", suffix)) and
                        suffix not in files and member.size <= MAX_RECOVERY_FILE,
                        "the recovery archive contains an unexpected recovery file")
                content = archive.extractfile(member)
                require(content is not None, "a recovery file is truncated")
                files[suffix] = content.read(member.size + 1)
                require(len(files[suffix]) == member.size, "a recovery file is truncated")
                members["recovery/" + suffix] = {"sha256": hashlib.sha256(files[suffix]).hexdigest(),
                                                 "bytes": member.size}
            else:
                suffix = name.removeprefix("pds/artifacts/")
                require(suffix in ARTIFACTS, "the recovery archive contains an unexpected artifact")
                if suffix == "data.tar":
                    require(set(artifacts) == SMALL_ARTIFACTS and data_sha256 is None,
                            "the data archive is out of order or duplicated")
                    require(set(files) == expected_files(files),
                            "the data archive precedes its recovery evidence")
                    contents = validate_contents(artifacts["contents.json"])
                    integrity = artifact_document(artifacts, "sqlite-integrity.json")
                    content = archive.extractfile(member)
                    require(content is not None, "the data archive is truncated")
                    reader = HashingReader(content)
                    seen = set()
                    file_count = database_count = 0
                    with tarfile.open(fileobj=reader, mode="r|") as nested:
                        for item in nested:
                            require(item.name in contents and item.name not in seen,
                                    "the data archive contains an unexpected or duplicate path")
                            seen.add(item.name)
                            entry = contents[item.name]
                            require((entry["type"] == "directory") == item.isdir() and
                                    (entry["type"] == "file") == item.isfile() and
                                    item.uid == entry["uid"] and item.gid == entry["gid"] and
                                    item.mode == entry["mode"] and
                                    (entry["type"] == "directory" or item.size == entry["bytes"]),
                                    "the data archive metadata differs from its inventory")
                            if item.isdir():
                                continue
                            file_count += 1
                            if item.name.endswith(".sqlite") or item.name.endswith(".sqlite3"):
                                database_count += 1
                            source = nested.extractfile(item)
                            require(source is not None, "a data archive file is missing")
                            checksum = hashlib.sha256()
                            count = 0
                            while chunk := source.read(4 * 1024 * 1024):
                                checksum.update(chunk)
                                count += len(chunk)
                            require(count == item.size and checksum.hexdigest() == entry["sha256"],
                                    "a data archive file checksum failed")
                    while reader.read(4 * 1024 * 1024):
                        pass
                    require(seen == set(contents), "the data archive inventory is incomplete")
                    require(reader.bytes == member.size and set(integrity["databases"]) == {
                        path for path, entry in contents.items()
                        if entry["type"] == "file" and (path.endswith(".sqlite") or path.endswith(".sqlite3"))} and
                        database_count == len(integrity["databases"]),
                        "the data archive size or SQLite set differs")
                    data_sha256, data_size = reader.checksum.hexdigest(), reader.bytes
                    members["artifacts/data.tar"] = {"sha256": data_sha256, "bytes": data_size}
                    require(hashlib.sha256(artifacts["SHA256SUMS"]).hexdigest() ==
                            members["artifacts/SHA256SUMS"]["sha256"], "the checksum manifest changed")
                else:
                    require(suffix not in artifacts and member.size <= MAX_SMALL_ARTIFACT,
                            "the recovery archive contains an unexpected or duplicate artifact")
                    content = archive.extractfile(member)
                    require(content is not None, "an artifact is truncated")
                    artifacts[suffix] = content.read(member.size + 1)
                    require(len(artifacts[suffix]) == member.size, "an artifact is truncated")
                    members["artifacts/" + suffix] = {
                        "sha256": hashlib.sha256(artifacts[suffix]).hexdigest(), "bytes": member.size}
    require(manifest is not None and manifest_seen, "the recovery manifest is missing")
    require(set(files) == expected_files(files), "the recovery archive is missing files")
    require(set(artifacts) == SMALL_ARTIFACTS and data_sha256 is not None, "the artifact set is incomplete")
    evidence = validate_files(files, artifacts, data_sha256, data_size)
    require(manifest.get("version") == 1 and manifest.get("namespace") == NAMESPACE and
            manifest.get("claim") == CLAIM and manifest.get("recipient") == recipient and
            manifest.get("members") == members, "the recovery manifest identity or checksums differ")
    timestamp(manifest.get("exported_at"))
    require(all(manifest.get(key) == value for key, value in evidence.items()),
            "the recovery manifest evidence differs from the saved results")
    return {"namespace": NAMESPACE, "claim": CLAIM, "files_verified": len(files) + len(artifacts),
            "data_archive_sha256": data_sha256, "recipient": recipient,
            "exported_at": manifest["exported_at"], **evidence}


def verify(arguments):
    require(shutil.which("age") and shutil.which("age-keygen"), "install age on this computer first")
    private_bytes(arguments.identity, os.geteuid(), maximum=MAX_RECOVERY_FILE)
    attributes = arguments.archive.lstat()
    require(stat.S_ISREG(attributes.st_mode) and attributes.st_nlink == 1 and
            attributes.st_uid == os.geteuid() and stat.S_IMODE(attributes.st_mode) == 0o600 and
            0 < attributes.st_size <= MAX_DATA_ARTIFACT + 2 * 1024**3,
            "the encrypted archive must be a private regular file")
    checksum = hashlib.sha256()
    with arguments.archive.open("rb") as source:
        while chunk := source.read(4 * 1024 * 1024):
            checksum.update(chunk)
    digest_value = checksum.hexdigest()
    require(digest_value == arguments.expected_sha256, "the encrypted archive checksum differs from the NAS export")
    recipient = subprocess.run(["age-keygen", "-y", str(arguments.identity)],
                               capture_output=True, text=True, timeout=30)
    require(recipient.returncode == 0, "age could not read the private recovery key")
    process = subprocess.Popen(["age", "--decrypt", "--identity", str(arguments.identity), str(arguments.archive)],
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        receipt = verify_stream(process.stdout, recipient.stdout.strip())
    finally:
        process.stdout.close()
        stderr = process.stderr.read()
        process.stderr.close()
        code = process.wait()
    require(code == 0, "age decryption failed; the copy has not been verified")
    receipt = {"result": "passed", "archive_sha256": digest_value,
               "verified_at": datetime.now(timezone.utc).isoformat(), **receipt}
    if arguments.receipt:
        descriptor = os.open(arguments.receipt, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(descriptor, "w") as output:
            output.write(json.dumps(receipt, indent=2) + "\n")
            output.flush()
            os.fsync(output.fileno())
    print(json.dumps(receipt, indent=2))
    print("off-NAS recovery copy verified; no decrypted files were written")


def main():
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    exporter = commands.add_parser("export")
    exporter.add_argument("--recipient", required=True)
    verifier = commands.add_parser("verify")
    verifier.add_argument("--archive", type=Path, required=True)
    verifier.add_argument("--identity", type=Path, required=True)
    verifier.add_argument("--expected-sha256", required=True)
    verifier.add_argument("--receipt", type=Path)
    arguments = parser.parse_args()
    if arguments.command == "export":
        require(re.fullmatch(r"age1[a-z0-9]{58}", arguments.recipient), "supply your native age public key")
        export(arguments.recipient)
    else:
        require(re.fullmatch(r"[0-9a-f]{64}", arguments.expected_sha256),
                "supply the NAS export's full SHA-256 checksum")
        verify(arguments)


if __name__ == "__main__":
    try:
        main()
    except RecoveryError as error:
        print("recovery stopped: " + str(error), file=sys.stderr)
        sys.exit(1)
    except (OSError, ValueError, KeyError, TypeError, AttributeError, EOFError, RecursionError,
            tarfile.TarError, subprocess.TimeoutExpired):
        # Never echo parsing errors, tool stderr, or private credential contents.
        print("recovery stopped: private files, archive format, or tool execution could not be verified",
              file=sys.stderr)
        sys.exit(1)
