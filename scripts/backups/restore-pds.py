#!/usr/bin/env python3
"""Restore the initial PDS backup into isolated scratch PVCs and smoke-test it."""

import base64
from collections import Counter
from datetime import datetime
import importlib.util
import json
import os
from pathlib import Path
import re
import shutil
import signal
import stat
import subprocess
import sys
import time


module_spec = importlib.util.spec_from_file_location(
    "pds_backup", Path(__file__).with_name("backup-pds.py"),
)
backup = importlib.util.module_from_spec(module_spec)
module_spec.loader.exec_module(backup)
setup = backup.setup
NS = setup.NAMESPACE
MANIFESTS = setup.MANIFESTS / "restore"
STATE = setup.RECOVERY / "initial-restore.json"
BASE_RESTORE = setup.BASE / "restore-validation" / backup.CLAIM / "initial"
DESTINATION = "data-restore-initial"
JOB = "data-restore-validation-initial"
SMOKE = "pds-restore-smoke"


def managed(resource):
    if resource["metadata"].get("labels", {}).get("app.kubernetes.io/managed-by") != setup.MANAGER:
        raise setup.SetupError("scratch resource belongs to another operation")


def restore_state():
    backup.preflight()
    identifier = backup.saved(backup.STATE).get("backup_id")
    if not isinstance(identifier, str) or not re.fullmatch(r"initial-[0-9a-f]{32}", identifier):
        raise setup.SetupError("invalid saved initial generation")
    journal = backup.saved(backup.QUIESCENCE)
    backup.check_quiescence_state(journal)
    if journal["phase"] != "resumed":
        raise setup.SetupError("the initial PDS writer must be running before a restore test")
    result = backup.saved(setup.RECOVERY / "initial-data-backup-result.json")
    saved_source = backup.saved(setup.RECOVERY / "initial-data-volsync-result.json")
    source = setup.get_object("replicationsource", backup.SOURCE, NS)
    checksums = backup.inspect_artifacts(identifier)
    if (not source or not checksums or not backup.upload_complete(saved_source, identifier) or
            not backup.upload_complete(source, identifier) or
            source["spec"].get("trigger") != {"manual": identifier} or
            result.get("result") != "passed" or result.get("backup_id") != identifier or
            result.get("claim") != backup.CLAIM or result.get("checksums") != checksums or
            not re.fullmatch(r"[0-9a-f]{64}", result.get("snapshot", ""))):
        raise setup.SetupError("verified initial upload evidence differs")
    cutoff = saved_source["status"]["lastSyncTime"]
    if datetime.fromisoformat(cutoff.replace("Z", "+00:00")).tzinfo is None:
        raise setup.SetupError("saved upload timestamp has no timezone")
    state = {"backup_id": identifier, "restore_as_of": cutoff,
             "snapshot": result["snapshot"], "checksums": checksums}
    if STATE.exists() or STATE.is_symlink():
        if backup.saved(STATE) != state:
            raise setup.SetupError("saved restore generation differs; preserving scratch data")
    else:
        backup.save(STATE, state)
    return state


def resources_for(state):
    rendered = setup.kube("create", "--dry-run=client", "-k", str(MANIFESTS),
                          "-o", "json", capture=True).stdout.lstrip()
    decoder = json.JSONDecoder()
    resources = []
    while rendered:
        resource, end = decoder.raw_decode(rendered)
        resources.extend(resource.get("items", [resource]))
        rendered = rendered[end:].lstrip()
    if Counter(resource["kind"] for resource in resources) != {
            "PersistentVolume": 3, "PersistentVolumeClaim": 2,
            "ReplicationDestination": 1, "NetworkPolicy": 1}:
        raise setup.SetupError("unexpected restore resource bundle")
    existing = {}
    for resource in resources:
        kind = resource["kind"]
        name = resource["metadata"]["name"]
        if kind == "ReplicationDestination":
            resource["spec"]["restic"]["restoreAsOf"] = state["restore_as_of"]
        group = resource["apiVersion"].split("/")[0] if "/" in resource["apiVersion"] else ""
        current = setup.get_object(kind.lower() + ("." + group if group else ""), name,
                                   resource["metadata"].get("namespace"))
        existing[name] = current
        if not current:
            continue
        managed(current)
        if kind == "PersistentVolume" and current.get("status", {}).get("phase") in ("Released", "Failed"):
            raise setup.SetupError("existing scratch PV needs binding inspection; preserving it")
        expected = json.loads(json.dumps(resource["spec"]))
        if kind == "ReplicationDestination":
            expected.pop("paused")
            expected.pop("trigger")
            for flag in ("cleanupCachePVC", "enableFileDeletion"):
                if current["spec"]["restic"].get(flag, False):
                    raise setup.SetupError("restore destination unexpectedly enables deletion")
                expected["restic"].pop(flag, None)  # The API can omit default-false fields.
            trigger = current["spec"].get("trigger", {})
            if (set(trigger) != {"manual"} or trigger["manual"] not in (
                    "controlled-by-restore-runner", state["backup_id"]) or
                    (trigger["manual"] == "controlled-by-restore-runner" and
                     not current["spec"].get("paused"))):
                raise setup.SetupError("existing restore destination trigger differs")
        if kind == "NetworkPolicy":
            for direction in ("ingress", "egress"):
                if current["spec"].get(direction):
                    raise setup.SetupError("validation policy permits unexpected traffic")
                expected.pop(direction, None)
        if not backup.contains_fields(current["spec"], expected):
            raise setup.SetupError("existing restore resource differs: " + name)
    return resources, existing


def check_binding(claim_name, volume_name, path):
    claim = setup.get_object("pvc", claim_name, NS)
    volume = setup.get_object("pv", volume_name)
    if (not claim or not volume or claim.get("status", {}).get("phase") != "Bound" or
            claim["spec"].get("volumeName") != volume_name or
            volume["spec"].get("claimRef", {}).get("uid") != claim["metadata"]["uid"] or
            volume["spec"].get("local", {}).get("path") != str(path) or
            volume["spec"].get("persistentVolumeReclaimPolicy") != "Retain"):
        raise setup.SetupError("scratch PVC binding differs: " + claim_name)
    setup.checked_directory(path)


def prepare_storage(resources, existing):
    storage_class = setup.get_object("storageclass", "pds-backup-local")
    if (not storage_class or storage_class.get("provisioner") != "kubernetes.io/no-provisioner" or
            storage_class.get("volumeBindingMode") != "Immediate"):
        raise setup.SetupError("the approved static backup storage class changed")
    if shutil.disk_usage(setup.BASE).free < 20 * 1024**3:
        raise setup.SetupError("less than 20 GiB remains on the array")
    allowed = {BASE_RESTORE / purpose for purpose in ("artifacts", "cache", "data")}
    for resource in resources:
        if resource["kind"] != "PersistentVolume":
            continue
        path = Path(resource["spec"]["local"]["path"])
        if path not in allowed:
            raise setup.SetupError("unexpected scratch storage path")
        setup.checked_directory(path)
        if path.exists():
            if any(path.iterdir()) and not existing[resource["metadata"]["name"]]:
                raise setup.SetupError("nonempty scratch data has no managed PV; preserving it")
            attributes = path.stat()
            if (attributes.st_uid, attributes.st_gid, stat.S_IMODE(attributes.st_mode)) != (999, 999, 0o2770):
                raise setup.SetupError("existing scratch directory ownership or mode differs: " + str(path))
        else:
            path.mkdir(parents=True, mode=0o2770)
            os.chown(path, 999, 999)
            os.chmod(path, 0o2770)
        print("array restore directory verified: " + str(path), flush=True)
    if not existing[DESTINATION] and any((BASE_RESTORE / "artifacts").iterdir()):
        raise setup.SetupError("recovered artifacts have no managed destination; preserving them")
    order = {"PersistentVolume": 0, "PersistentVolumeClaim": 1, "NetworkPolicy": 2, "ReplicationDestination": 3}
    for resource in sorted(resources, key=lambda item: order[item["kind"]]):
        if resource["kind"] == "ReplicationDestination" and existing[resource["metadata"]["name"]]:
            continue
        setup.kube("apply", "-f", "-", stdin=json.dumps(resource))
    for purpose in ("artifacts", "data"):
        name = "data-restore-" + purpose
        setup.kube("wait", "-n", NS, "pvc/" + name, "--for=jsonpath={.status.phase}=Bound", "--timeout=60s")
        check_binding(name, "pds-data-restore-" + purpose, BASE_RESTORE / purpose)


def idle():
    deadline = time.monotonic() + 120
    while True:
        pods = json.loads(setup.kube("get", "pods", "-n", NS, "-o", "json", capture=True).stdout)["items"]
        active = [pod for pod in pods if pod.get("status", {}).get("phase") not in ("Succeeded", "Failed") and (
            pod["metadata"].get("labels", {}).get("backups.mousses.xyz/restic-client") == "true" or
            pod["metadata"].get("labels", {}).get("app.kubernetes.io/name") == "pds-restore-validation")]
        if not active:
            return
        if time.monotonic() >= deadline:
            raise setup.SetupError("a previous restore worker remains active; preserving it")
        time.sleep(2)


def receipt_file(path):
    attributes = path.lstat()
    # The artifacts PVC is setgid 999, so the root-owned receipt inherits GID
    # 999. Mode 0600 still restricts the receipt to UID 0.
    if (not stat.S_ISREG(attributes.st_mode) or attributes.st_nlink != 1 or
            attributes.st_uid != 0 or stat.S_IMODE(attributes.st_mode) != 0o600):
        raise setup.SetupError(
            "restored validation receipt ownership or mode differs: "
            f"uid={attributes.st_uid} gid={attributes.st_gid} "
            f"mode={stat.S_IMODE(attributes.st_mode):04o} nlink={attributes.st_nlink}")
    return path


def restore_artifacts(state):
    identifier = state["backup_id"]
    artifacts = BASE_RESTORE / "artifacts"
    destination = setup.get_object("replicationdestination", DESTINATION, NS)
    if not destination:
        raise setup.SetupError("the restore destination is missing")
    destination_uid = destination["metadata"]["uid"]
    if destination["spec"].get("paused"):
        if any(artifacts.iterdir()):
            raise setup.SetupError("paused destination already has data; preserving it")
        backup.worker_budget()
        setup.kube("patch", "replicationdestination", DESTINATION, "-n", NS, "--type=merge", "-p", json.dumps({
            "metadata": {"resourceVersion": destination["metadata"]["resourceVersion"]},
            "spec": {"paused": False, "trigger": {"manual": identifier}},
        }))
    deadline = time.monotonic() + 1800
    while True:
        destination = setup.get_object("replicationdestination", DESTINATION, NS)
        if (not destination or destination["metadata"]["uid"] != destination_uid or
                destination["spec"].get("trigger") != {"manual": identifier} or
                destination["spec"].get("paused")):
            raise setup.SetupError("restore destination changed while restoring")
        if backup.upload_complete(destination, identifier):
            break
        if time.monotonic() >= deadline:
            backup.save(setup.RECOVERY / "initial-data-volsync-restore-pending.json", destination)
            raise setup.SetupError("VolSync restore is still pending; scratch data preserved")
        time.sleep(2)
    idle()
    names = {path.name for path in artifacts.iterdir()}
    if "validated-result.json" in names:
        receipt_file(artifacts / "validated-result.json")
        names.remove("validated-result.json")
    if names == {"current"}:
        current = artifacts / "current"
    elif names == {"data"}:
        setup.checked_directory(artifacts / "data")
        if {path.name for path in (artifacts / "data").iterdir()} != {"current"}:
            raise setup.SetupError("unexpected recovered artifact layout")
        current = artifacts / "data/current"
    else:
        raise setup.SetupError("unexpected recovered artifact layout")
    checksums = backup.inspect_artifacts(identifier, current=current)
    if checksums != state["checksums"]:
        raise setup.SetupError("recovered artifacts differ from the verified initial snapshot")
    check_binding("volsync-dst-" + DESTINATION + "-cache", "pds-data-restore-cache", BASE_RESTORE / "cache")
    backup.save(setup.RECOVERY / "initial-data-volsync-restore-result.json", destination)
    print("VolSync restored and verified all " + str(len(backup.ARTIFACTS)) +
          " artifacts: pds-data", flush=True)
    return current


def validation(state, current):
    job = backup.read_template(MANIFESTS / "validate-job.yaml")
    job["metadata"]["annotations"] = {
        "backups.mousses.xyz/generation": state["backup_id"],
        "backups.mousses.xyz/snapshot": state["snapshot"],
    }
    environment = {
        "BACKUP_ID": state["backup_id"],
        "ARTIFACT_DIRECTORY": "/artifacts/" + current.relative_to(BASE_RESTORE / "artifacts").as_posix(),
        "EXPECTED_SHA256SUMS": state["checksums"],
    }
    job["spec"]["template"]["spec"]["containers"][0]["env"] = [
        {"name": key, "value": value} for key, value in environment.items()]
    existing = setup.get_object("job", JOB, NS)
    if existing:
        managed(existing)
        if (existing["metadata"].get("annotations") != job["metadata"]["annotations"] or
                existing["spec"].get("backoffLimit") != 0 or
                existing["spec"].get("activeDeadlineSeconds") != 1200 or
                not backup.contains_fields(existing["spec"]["template"]["spec"],
                                           job["spec"]["template"]["spec"])):
            raise setup.SetupError("existing validation Job differs; preserving it")
    else:
        idle()
        if any((BASE_RESTORE / "data").iterdir()):
            raise setup.SetupError("scratch validation data has no matching Job; preserving it")
        if any(backup.active_claim_pods("data-restore-artifacts") or
               backup.active_claim_pods("data-restore-data")):
            raise setup.SetupError("scratch PVCs are still in use; wait before validating")
        backup.worker_budget()
        setup.kube("create", "-f", "-", stdin=json.dumps(job))
    try:
        setup.wait_for_job(JOB, timeout=1260)
    finally:
        setup.print_check_logs(JOB)
    logs = setup.kube("logs", "-n", NS, "job/" + JOB, capture=True, sensitive=True).stdout
    results = [json.loads(line.removeprefix("RESTORE_VALIDATED_JSON=")) for line in logs.splitlines()
               if line.startswith("RESTORE_VALIDATED_JSON=")]
    if (len(results) != 1 or results[0].get("result") != "passed" or
            results[0].get("claim") != backup.CLAIM or results[0].get("backup_id") != state["backup_id"]):
        raise setup.SetupError("completed Job did not report the expected restored generation")
    result = results[0]
    evidence_path = receipt_file(BASE_RESTORE / "artifacts/validated-result.json")
    if json.loads(evidence_path.read_text()) != result:
        raise setup.SetupError("scratch validation evidence differs from the completed Job")
    application = json.loads((current / "application.json").read_text())
    if (result.get("archive_metadata_verified") is not True or
            not all(type(result.get(key)) is int and result[key] >= 0
                    for key in ("files", "directories", "sqlite_databases")) or
            result.get("service_did") != application["describe"]["did"] or
            result.get("repo_count") != application["repo_count"]):
        raise setup.SetupError("restored SQLite, blob, ownership, or identity evidence is incomplete")
    backup.save(setup.RECOVERY / "initial-data-restore-result.json", {
        **result, "snapshot": state["snapshot"], "completed_at": backup.now(), "job": JOB,
    })
    print("isolated archive, SQLite, blob, ownership, and identity validation passed", flush=True)
    return result


def smoke(state, current):
    # The smoke Pod reserves less than production, but still needs headroom
    # beside the running PDS for the few minutes it serves isolated requests.
    node = setup.get_object("node", "openmediavault")
    pods = json.loads(setup.kube("get", "pods", "-n", NS, "-o", "json", capture=True).stdout)["items"]
    setup.resource_budget(node, pods, worker=False, extra={"cpu": "100m", "memory": "256Mi"})
    credentials = json.loads((current / "application-secret.json").read_text())
    if (credentials.get("kind") != "Secret" or credentials["metadata"].get("name") != "pds-env" or
            credentials["metadata"].get("namespace") != NS or not isinstance(credentials.get("data"), dict)):
        raise setup.SetupError("the restored PDS environment Secret is invalid")
    try:
        fields = {key: base64.b64decode(value, validate=True).decode()
                  for key, value in credentials["data"].items()}
    except (ValueError, UnicodeDecodeError) as error:
        raise setup.SetupError("the restored PDS environment Secret is not valid UTF-8") from error
    setup.ensure_secret("pds-restore-env", fields)
    template = backup.read_template(MANIFESTS / "smoke-deployment.yaml")
    deployment = setup.get_object("deployment", SMOKE, NS)
    if deployment:
        managed(deployment)
        expected = {key: value for key, value in template["spec"].items() if key != "replicas"}
        if not backup.contains_fields(deployment["spec"], expected):
            raise setup.SetupError("existing smoke Deployment differs from its reviewed manifest")
        if deployment["spec"].get("replicas") not in (0, 1):
            raise setup.SetupError("smoke Deployment replica count changed outside this restore")
    else:
        setup.kube("apply", "-f", "-", stdin=json.dumps(template))
        deployment = setup.get_object("deployment", SMOKE, NS)
    if deployment["spec"].get("replicas") == 0:
        setup.kube("scale", "deployment/" + SMOKE, "-n", NS, "--replicas=1", "--current-replicas=0",
                   "--resource-version=" + deployment["metadata"]["resourceVersion"])
    try:
        setup.kube("rollout", "status", "deployment/" + SMOKE, "-n", NS, "--timeout=300s")
        pods = json.loads(setup.kube("get", "pods", "-n", NS, "-l", "app.kubernetes.io/name=" + SMOKE,
                                     "-o", "json", capture=True).stdout)["items"]
        ready = [pod for pod in pods if not pod["metadata"].get("deletionTimestamp") and setup.ready(pod)]
        if len(ready) != 1:
            raise setup.SetupError("expected one ready isolated PDS smoke Pod")
        pod = ready[0]
        result = setup.kube("exec", "-n", NS, "pod/" + pod["metadata"]["name"], "--",
                            "node", "-e", backup.APPLICATION_SCRIPT, capture=True, sensitive=True, timeout=120)
        lines = [line.removeprefix("PDS_EVIDENCE_JSON=") for line in result.stdout.splitlines()
                 if line.startswith("PDS_EVIDENCE_JSON=")]
        if len(lines) != 1:
            raise setup.SetupError("the isolated PDS did not report exactly one application snapshot")
        try:
            evidence = json.loads(lines[0])
        except ValueError as error:
            raise setup.SetupError("the isolated PDS application snapshot is not valid JSON") from error
        observed = backup.validate_application(evidence)
        expected = json.loads((current / "application.json").read_text())
        if (observed["health"].get("version") != expected["health"].get("version") or
                observed["describe"].get("did") != expected["describe"].get("did") or
                observed["describe"].get("availableUserDomains") != expected["describe"].get("availableUserDomains") or
                observed["describe"].get("inviteCodeRequired") != expected["describe"].get("inviteCodeRequired") or
                observed["describe"].get("phoneVerificationRequired") !=
                expected["describe"].get("phoneVerificationRequired") or
                observed["repo_dids"] != expected["repo_dids"] or
                observed["repo_count"] != expected["repo_count"]):
            raise setup.SetupError("the isolated PDS served a different identity or repository set")
        receipt = {
            "result": "passed", "backup_id": state["backup_id"], "claim": backup.CLAIM,
            "snapshot": state["snapshot"], "verified_at": backup.now(), "deployment": SMOKE,
            "pod_uid": pod["metadata"]["uid"], "health": observed["health"],
            "describe": observed["describe"], "repo_count": observed["repo_count"],
            "repo_dids": observed["repo_dids"],
            "network_isolated": True,
        }
        backup.save(setup.RECOVERY / "initial-data-smoke-result.json", receipt)
        print("isolated PDS served matching health, service identity, and repository data", flush=True)
    except (setup.SetupError, subprocess.TimeoutExpired):
        logs = setup.kube("logs", "-n", NS, "deployment/" + SMOKE, "--tail=200", "--pod-running-timeout=5s",
                          capture=True, sensitive=True, check=False, timeout=20)
        if not logs.returncode:
            setup.private_write(setup.RECOVERY / "initial-data-smoke.log", logs.stdout[-1024**2:])
        raise
    finally:
        current_deployment = setup.get_object("deployment", SMOKE, NS)
        if current_deployment and current_deployment["spec"].get("replicas") == 1:
            setup.kube("scale", "deployment/" + SMOKE, "-n", NS, "--replicas=0", "--current-replicas=1",
                       "--resource-version=" + current_deployment["metadata"]["resourceVersion"], check=False)
            deadline = time.monotonic() + 180
            while time.monotonic() < deadline:
                pods = json.loads(setup.kube("get", "pods", "-n", NS, "-l", "app.kubernetes.io/name=" + SMOKE,
                                             "-o", "json", capture=True).stdout)["items"]
                if not any(pod.get("status", {}).get("phase") not in ("Succeeded", "Failed")
                           for pod in pods):
                    break
                time.sleep(2)
            else:
                print("isolated PDS smoke Pod is still terminating", flush=True)
    return receipt


def restore():
    production = setup.live_preflight(check_resources=False)
    state = restore_state()
    credentials = backup.saved(setup.RECOVERY / "credentials.json")["repositories"]
    setup.ensure_secret("restic-restore-" + backup.CLAIM,
                        setup.credential_fields(backup.CLAIM, credentials[backup.CLAIM]))
    saved_ca = backup.saved(setup.RECOVERY / "restic-root-ca.secret.json")
    ca = base64.b64decode(saved_ca["data"]["tls.crt"], validate=True).decode()
    setup.ensure_secret("restic-restore-ca", {"ca.crt": ca})
    resources, existing = resources_for(state)
    prepare_storage(resources, existing)
    current = restore_artifacts(state)
    validation(state, current)
    smoke(state, current)
    deployment = setup.live_preflight(check_resources=False)
    if deployment["metadata"]["uid"] != production["metadata"]["uid"]:
        raise setup.SetupError("production PDS was replaced during isolated validation")
    backup.save(setup.RECOVERY / "initial-restore-verification.json", {
        "result": "passed", "backup_id": state["backup_id"], "snapshot": state["snapshot"],
        "restore_as_of": state["restore_as_of"], "completed_at": backup.now(),
        "validation": backup.saved(setup.RECOVERY / "initial-data-restore-result.json"),
        "smoke": backup.saved(setup.RECOVERY / "initial-data-smoke-result.json"),
        "production": {"deployment_uid": deployment["metadata"]["uid"],
                       "template_hash": backup.template_hash(deployment["spec"]["template"])},
    })
    print("initial PDS backup, isolated restore, SQLite/blob validation, and startup smoke test passed", flush=True)


def main():
    if os.geteuid() != 0:
        raise setup.SetupError("run with sudo python3 on OMV")
    os.umask(0o077)
    os.environ["PATH"] = "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
    setup.recovery_directory()
    for number in (signal.SIGHUP, signal.SIGINT, signal.SIGTERM):
        signal.signal(number, backup.interrupted)
    with setup.operation_lock(setup.RECOVERY / ".setup.lock", create=True):
        with setup.operation_lock(setup.COORDINATION_LOCK, create=False):
            print("pds restore worker serialized with the existing namespace backup workers", flush=True)
            restore()


if __name__ == "__main__":
    try:
        main()
    except (setup.SetupError, OSError, ValueError, KeyError, TypeError, subprocess.TimeoutExpired) as error:
        print("restore validation stopped: " + str(error), file=sys.stderr)
        sys.exit(1)
