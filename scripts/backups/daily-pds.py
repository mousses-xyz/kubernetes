#!/usr/bin/env python3
"""Serialize daily PDS capture, VolSync uploads, and verification."""

import argparse
from datetime import datetime, timedelta
import fcntl
import importlib.util
import json
import os
from pathlib import Path
import re
import secrets
import signal
import stat
import subprocess
import sys
import time


module_spec = importlib.util.spec_from_file_location(
    "pds_backup", Path(__file__).with_name("backup-pds.py"),
)
native = importlib.util.module_from_spec(module_spec)
module_spec.loader.exec_module(native)
setup = native.setup
NS = setup.NAMESPACE
ACTIVE = setup.RECOVERY / "daily-active.json"
LATEST = setup.RECOVERY / "latest-daily-backup.json"
PROOF = setup.RECOVERY / "retention-proof.json"
COORDINATION_LOCK = Path("/root/k3s-backup-recovery/board-games/data-postgres-0/.setup.lock")
saved = native.saved
save = native.save
sync_directory = native.sync_directory


def valid_id(value):
    return isinstance(value, str) and re.fullmatch(r"(?:initial|daily)-[0-9a-f]{32}", value)


def history():
    return setup.BASE / "backup-staging-history" / native.CLAIM


def recover_writer():
    if native.QUIESCENCE.exists() or native.QUIESCENCE.is_symlink():
        state = saved(native.QUIESCENCE)
        native.check_quiescence_state(state)
        if state["phase"] != "resumed":
            native.resume_pds(state)


def wait_for_dependencies():
    deadline = time.monotonic() + 180
    announced = False
    while time.monotonic() < deadline:
        try:
            probe = setup.kube(
                "get", "deployment/pds", "deployment/restic-server", "-n", NS,
                "--ignore-not-found=true", "--request-timeout=5s", "-o", "json",
                capture=True, sensitive=True, check=False, timeout=10,
            )
        except subprocess.TimeoutExpired:
            probe = None
        if probe and not probe.returncode and probe.stdout.strip():
            resources = json.loads(probe.stdout)
            resources = resources.get("items", [resources])
            if (len(resources) == 2 and
                    all(item.get("status", {}).get("availableReplicas", 0) >= 1 for item in resources)):
                return
        if not announced:
            print("waiting up to 180 seconds for the API, PDS, and backup endpoint", flush=True)
            announced = True
        time.sleep(5)
    raise setup.SetupError("backup dependencies did not become ready")


def preflight():
    initial = saved(native.STATE).get("backup_id")
    verified = saved(setup.RECOVERY / "initial-data-backup-result.json")
    restored = saved(setup.RECOVERY / "initial-data-restore-result.json")
    smoke = saved(setup.RECOVERY / "initial-data-smoke-result.json")
    journal = saved(native.QUIESCENCE)
    native.check_quiescence_state(journal)
    if (not valid_id(initial) or verified.get("result") != "passed" or verified.get("backup_id") != initial or
            verified.get("claim") != native.CLAIM or journal["phase"] != "resumed" or
            restored.get("result") != "passed" or restored.get("backup_id") != initial or
            restored.get("claim") != native.CLAIM or
            smoke.get("result") != "passed" or smoke.get("backup_id") != initial or
            smoke.get("claim") != native.CLAIM):
        raise setup.SetupError("the initial backup, isolated restore, and smoke test must pass first")
    native.preflight()
    rendered = setup.kube("create", "--dry-run=client", "-k", str(native.MANIFESTS),
                          "-o", "json", capture=True).stdout.lstrip()
    decoder = json.JSONDecoder()
    resources = []
    while rendered:
        resource, end = decoder.raw_decode(rendered)
        resources.extend(resource.get("items", [resource]))
        rendered = rendered[end:].lstrip()
    sources = {item["metadata"]["name"]: item for item in resources if item["kind"] == "ReplicationSource"}
    if set(sources) != {native.SOURCE}:
        raise setup.SetupError("unexpected daily source manifest bundle")
    source = setup.get_object("replicationsource", native.SOURCE, NS)
    if not source:
        raise setup.SetupError("a verified VolSync source is missing")
    native.managed(source)
    expected = {key: value for key, value in sources[native.SOURCE]["spec"].items() if key not in ("paused", "trigger")}
    if not native.contains_fields(source["spec"], expected):
        raise setup.SetupError("VolSync repository, retention, resources, or placement changed")
    if PROOF.exists() or PROOF.is_symlink():
        proof = saved(PROOF)
        if proof.get("result") != "passed" or proof.get("retention") != "within 7d":
            raise setup.SetupError("saved scratch retention proof is invalid")
    return initial, source


def run_state(initial, source):
    if ACTIVE.exists() or ACTIVE.is_symlink():
        state = saved(ACTIVE)
        if (not re.fullmatch(r"daily-[0-9a-f]{32}", state.get("backup_id", "")) or
                state.get("initial_backup_id") != initial or not valid_id(state.get("previous_id")) or
                state["previous_id"] == state["backup_id"] or
                state.get("previous_checksums") is None or
                not isinstance(state.get("previous_checksums"), str) or
                state.get("source_uid") != source["metadata"]["uid"]):
            raise setup.SetupError("saved daily run state is invalid; preserving it")
        return state
    if LATEST.exists() or LATEST.is_symlink():
        latest = saved(LATEST)
        if (latest.get("result") != "passed" or latest.get("initial_backup_id") != initial or
                set(latest.get("components", {})) != {native.COMPONENT}):
            raise setup.SetupError("the latest daily backup is not fully verified")
        previous = latest["backup_id"]
        receipt = latest["components"][native.COMPONENT]
    else:
        previous = initial
        receipt = saved(setup.RECOVERY / "initial-data-backup-result.json")
    checksums = native.inspect_artifacts(previous)
    if (not valid_id(previous) or not native.upload_complete(source, previous) or not checksums or
            receipt.get("result") != "passed" or receipt.get("backup_id") != previous or
            receipt.get("claim") != native.CLAIM or receipt.get("checksums") != checksums):
        raise setup.SetupError("the previous verified upload or staged generation differs")
    state = {"backup_id": "daily-" + secrets.token_hex(16), "initial_backup_id": initial,
             "previous_id": previous, "previous_checksums": checksums,
             "source_uid": source["metadata"]["uid"], "started_at": native.now()}
    save(ACTIVE, state)
    return state


def reserve_source(state):
    for attempt in range(5):
        source = setup.get_object("replicationsource", native.SOURCE, NS)
        if not source or source["metadata"]["uid"] != state["source_uid"]:
            raise setup.SetupError("a VolSync source disappeared or was replaced")
        if source["spec"].get("trigger") != {"manual": state["previous_id"]}:
            break
        if not native.upload_complete(source, state["previous_id"]):
            raise setup.SetupError("the previous upload is not idle; preserving staging")
        result = setup.kube("patch", "replicationsource", native.SOURCE, "-n", NS,
                            "--type=merge", "-p", json.dumps({
                                "metadata": {"resourceVersion": source["metadata"]["resourceVersion"]},
                                "spec": {"paused": True, "trigger": {"manual": state["backup_id"]}},
                            }), capture=True, check=False)
        if not result.returncode:
            print(result.stdout, end="", flush=True)
            source = setup.get_object("replicationsource", native.SOURCE, NS)
            break
        if "(Conflict)" not in result.stderr:
            raise setup.SetupError("could not reserve the VolSync source")
        time.sleep(0.2)
    else:
        raise setup.SetupError("VolSync source kept changing; preserving staging")
    if (not source or source["metadata"]["uid"] != state["source_uid"] or
            source["spec"].get("trigger") != {"manual": state["backup_id"]}):
        raise setup.SetupError("VolSync trigger changed outside this daily run")
    if not source["spec"].get("paused"):
        if native.inspect_artifacts(state["backup_id"]) is None:
            raise setup.SetupError("an upload is unpaused without its immutable published generation")
    elif native.active_claim_pods("data-backup-staging"):
        raise setup.SetupError("a Pod still holds staging; wait before moving its data")


def archive_previous(state):
    directory = history()
    setup.checked_directory(directory)
    if directory.exists():
        attributes = directory.stat()
        if attributes.st_uid != 0 or stat.S_IMODE(attributes.st_mode) != 0o700:
            raise setup.SetupError("staging history must be root-owned with mode 0700")
    else:
        directory.mkdir(parents=True, mode=0o700)
        directory.chmod(0o700)
    current = native.staging() / "current"
    previous = directory / state["previous_id"]
    if current.exists() or current.is_symlink():
        setup.checked_directory(current)
        identifier = (current / "backup-id").read_text().strip()
        if identifier == state["backup_id"]:
            native.inspect_artifacts(identifier)
        else:
            if identifier != state["previous_id"] or previous.exists() or previous.is_symlink():
                raise setup.SetupError("staging history differs from this daily run")
            native.ensure_source_idle(state["backup_id"])
            if native.inspect_artifacts(identifier) != state["previous_checksums"]:
                raise setup.SetupError("previous staged artifacts differ from their verification receipt")
            current.rename(previous)
            sync_directory(native.staging())
            sync_directory(directory)
            print("previous verified capture retained outside staging: pds-data/" + identifier, flush=True)
    if native.inspect_artifacts(state["previous_id"], current=previous) != state["previous_checksums"]:
        raise setup.SetupError("the previous capture is missing or changed in staging history")


def expiry_receipt(identifier):
    return setup.RECOVERY / ("history-expiry-pds-data-" + identifier + ".json")


def remove_expired_history(directory):
    identifier = directory.name.removeprefix(".expired-")
    if not directory.name.startswith(".expired-") or not valid_id(identifier):
        raise setup.SetupError("invalid expired generation name")
    receipt = saved(expiry_receipt(identifier))
    if receipt.get("backup_id") != identifier or receipt.get("component") != native.COMPONENT:
        raise setup.SetupError("expired history has no matching cleanup approval")
    checksums = {}
    for line in receipt["checksums"].splitlines():
        match = re.fullmatch(r"([0-9a-f]{64})  ([a-zA-Z0-9.-]+)", line)
        if not match or match[2] in checksums:
            raise setup.SetupError("expired history cleanup manifest is invalid")
        checksums[match[2]] = match[1]
    if set(checksums) != native.ARTIFACTS - {"SHA256SUMS"}:
        raise setup.SetupError("expired history cleanup manifest is incomplete")
    setup.checked_directory(directory)
    attributes = directory.stat()
    if (attributes.st_uid, attributes.st_gid, stat.S_IMODE(attributes.st_mode)) != (999, 999, 0o700):
        raise setup.SetupError("expired history ownership or mode differs")
    artifacts = list(directory.iterdir())
    for artifact in artifacts:
        attributes = artifact.lstat()
        if (artifact.name not in native.ARTIFACTS or not stat.S_ISREG(attributes.st_mode) or
                attributes.st_nlink != 1 or
                (attributes.st_uid, attributes.st_gid, stat.S_IMODE(attributes.st_mode)) != (999, 999, 0o600)):
            raise setup.SetupError("unexpected expired history file; preserving it")
        if artifact.name == "SHA256SUMS":
            if artifact.read_text() != receipt["checksums"]:
                raise setup.SetupError("expired history checksum manifest differs")
        elif native.digest_file(artifact) != checksums[artifact.name]:
            raise setup.SetupError("expired history file differs from its cleanup approval")
    for artifact in artifacts:
        artifact.unlink()
    sync_directory(directory)
    directory.rmdir()
    sync_directory(history())


def expire_history(state):
    current = native.staging() / "current"
    captured = datetime.fromisoformat(json.loads((current / "metadata.json").read_text())["captured_at"])
    if captured.tzinfo is None:
        raise setup.SetupError("capture metadata has no timezone")
    cutoff = captured - timedelta(days=7)
    for directory in list(history().iterdir()):
        if directory.name.startswith(".expired-"):
            remove_expired_history(directory)
            continue
        if not valid_id(directory.name):
            raise setup.SetupError("unrecognized staging history; preserving it")
        checksums = native.inspect_artifacts(directory.name, current=directory)
        timestamp = datetime.fromisoformat(json.loads((directory / "metadata.json").read_text())["captured_at"])
        if timestamp.tzinfo is None:
            raise setup.SetupError("staging history has no capture timezone")
        if timestamp < cutoff and directory.name != state["previous_id"]:
            expired = directory.with_name(".expired-" + directory.name)
            if expired.exists() or expired.is_symlink():
                raise setup.SetupError("expired history name is already occupied")
            save(expiry_receipt(directory.name), {
                "backup_id": directory.name, "component": native.COMPONENT,
                "checksums": checksums, "approved_at": native.now(),
            })
            directory.rename(expired)
            sync_directory(history())
            remove_expired_history(expired)
            print("expired validated staging history: pds-data/" + directory.name, flush=True)


def backup():
    wait_for_dependencies()
    initial, source = preflight()
    state = run_state(initial, source)
    reserve_source(state)
    archive_previous(state)
    native.capture_data(state["backup_id"])
    journal = saved(native.QUIESCENCE)
    native.check_quiescence_state(journal, state["backup_id"])
    if journal["phase"] != "resumed":
        raise setup.SetupError("the PDS writer did not resume after capture")
    prove_retention = not (PROOF.exists() or PROOF.is_symlink())
    native.upload(state["backup_id"], evidence_prefix=state["backup_id"])
    receipt = native.verify_backup(state["backup_id"], evidence_prefix=state["backup_id"],
                                   retention_proof=prove_retention)
    if not native.upload_complete(setup.get_object("replicationsource", native.SOURCE, NS), state["backup_id"]):
        raise setup.SetupError("VolSync completion changed after snapshot verification")
    if prove_retention:
        save(PROOF, {"result": "passed", "retention": "within 7d", "verified_at": native.now(),
                     "job": receipt["verification_job"], "evidence": receipt["retention_proof"]})
    expire_history(state)
    setup.live_preflight(check_resources=False)
    result = {**state, "result": "passed", "completed_at": native.now(),
              "components": {native.COMPONENT: receipt}, "retention": "within 7d"}
    save(setup.RECOVERY / (state["backup_id"] + ".result.json"), result)
    save(LATEST, result)
    ACTIVE.unlink()
    sync_directory(setup.RECOVERY)
    print("daily backup verified: " + state["backup_id"], flush=True)
    print("PDS quiesced capture, upload, retention, and full artifact checks completed in sequence", flush=True)


def coordination_descriptor():
    current = Path("/root")
    for part in COORDINATION_LOCK.parent.relative_to(current).parts:
        current /= part
        attributes = current.lstat()
        if not stat.S_ISDIR(attributes.st_mode) or attributes.st_uid != 0 or stat.S_IMODE(attributes.st_mode) != 0o700:
            raise setup.SetupError("board-games operation-lock directory is not private and root-owned")
    attributes = COORDINATION_LOCK.lstat()
    if (not stat.S_ISREG(attributes.st_mode) or attributes.st_nlink != 1 or
            attributes.st_uid != 0 or stat.S_IMODE(attributes.st_mode) != 0o600):
        raise setup.SetupError("board-games operation lock is missing or changed")
    descriptor = os.open(COORDINATION_LOCK, os.O_RDWR | os.O_NOFOLLOW)
    opened = os.fstat(descriptor)
    if (opened.st_dev, opened.st_ino) != (attributes.st_dev, attributes.st_ino):
        os.close(descriptor)
        raise setup.SetupError("board-games operation lock changed while opening it")
    return descriptor


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--recover-writer-only", action="store_true")
    arguments = parser.parse_args()
    if os.geteuid() != 0:
        raise setup.SetupError("run with sudo python3 on OMV")
    os.umask(0o077)
    os.environ["PATH"] = "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
    setup.recovery_directory()
    for number in (signal.SIGHUP, signal.SIGINT, signal.SIGTERM):
        signal.signal(number, native.interrupted)
    descriptor = os.open(setup.RECOVERY / ".setup.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, "w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            if arguments.recover_writer_only:
                print("an active pds operation owns writer recovery", flush=True)
                return
            raise setup.SetupError("another pds operation holds the namespace lock") from error
        recover_writer()  # Recover an interrupted application before waiting on another namespace.
        if arguments.recover_writer_only:
            return
        with os.fdopen(coordination_descriptor(), "r+") as coordination:
            deadline = time.monotonic() + 2700
            announced = False
            while True:
                try:
                    fcntl.flock(coordination, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if time.monotonic() >= deadline:
                        raise setup.SetupError("board-games operation lock remained busy for 45 minutes")
                    if not announced:
                        print("waiting for the board-games operation lock before using backup worker capacity", flush=True)
                        announced = True
                    time.sleep(5)
            print("backup worker sequence serialized with board-games", flush=True)
            backup()


if __name__ == "__main__":
    try:
        main()
    except (setup.SetupError, OSError, ValueError, KeyError, TypeError, subprocess.TimeoutExpired) as error:
        print("daily backup stopped: " + str(error), file=sys.stderr)
        sys.exit(1)
