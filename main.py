import os
import sys
import time
import requests
import oci

def send_telegram(msg):
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    chat_id = os.getenv("TELEGRAM_CHAT_ID")
    if token and chat_id:
        try:
            url = f"https://api.telegram.org/bot{token}/sendMessage"
            requests.post(url, json={"chat_id": chat_id, "text": msg, "parse_mode": "Markdown"}, timeout=10)
        except Exception as e:
            print(f"Telegram notification error: {e}", flush=True)

def trigger_next_run():
    gh_token = os.getenv("GH_PAT")
    repo_full = os.getenv("GITHUB_REPOSITORY", "Alixan1/oci-sniper")
    if gh_token:
        try:
            print(f"Chaining next workflow run for {repo_full}...", flush=True)
            res = requests.post(
                f"https://api.github.com/repos/{repo_full}/actions/workflows/sniper.yml/dispatches",
                headers={"Authorization": f"Bearer {gh_token}", "Accept": "application/vnd.github+json"},
                json={"ref": "main"},
                timeout=15
            )
            print(f"Next workflow dispatched: {res.status_code}", flush=True)
        except Exception as e:
            print(f"Error triggering next run: {e}", flush=True)

def try_launch(compute, launch_details, spec_label):
    try:
        res = compute.launch_instance(launch_details).data
        return True, res.id, ""
    except oci.exceptions.ServiceError as e:
        if "Out of host capacity" in str(e.message):
            return False, None, "Out of host capacity"
        return False, None, f"OCI Error ({e.status}) {e.code} - {e.message}"
    except Exception as e:
        return False, None, f"Exception: {e}"

def main():
    user = os.getenv("OCI_USER_OCID")
    tenancy = os.getenv("OCI_TENANCY_OCID")
    fingerprint = os.getenv("OCI_KEY_FINGERPRINT")
    key_content = os.getenv("OCI_KEY_CONTENT")
    region = os.getenv("OCI_REGION", "us-ashburn-1")

    ad = os.getenv("OCI_AD", "RbsL:US-ASHBURN-AD-1")
    boot_vol_id = os.getenv("BOOT_VOLUME_OCID", "ocid1.bootvolume.oc1.iad.abuwcljrgirwkpyaptzw35i3qmnq5jlei76qgktdnk6be6zgds6ln7m5f4bq")
    subnet_id = os.getenv("SUBNET_OCID", "ocid1.subnet.oc1.iad.aaaaaaaacykkjtru6urue2upaxvt3pifv63zvp5fitfg2ui4lluayjjsufza")
    ssh_public_key = os.getenv("SSH_PUBLIC_KEY")

    if not all([user, tenancy, fingerprint, key_content]):
        print("CRITICAL: Missing OCI credentials in environment variables!", flush=True)
        sys.exit(1)

    key_path = "/tmp/oci_api_key.pem" if sys.platform != "win32" else "C:\\Temp\\oci_api_key.pem"
    os.makedirs(os.path.dirname(key_path), exist_ok=True)
    with open(key_path, "w", encoding="utf-8") as f:
        f.write(key_content.strip() + "\n")

    config = {
        "user": user,
        "key_file": key_path,
        "fingerprint": fingerprint,
        "tenancy": tenancy,
        "region": region
    }

    compute = oci.core.ComputeClient(config, timeout=(15.0, 60.0))
    vn_client = oci.core.VirtualNetworkClient(config, timeout=(15.0, 60.0))

    source_details = oci.core.models.InstanceSourceViaBootVolumeDetails(
        source_type="bootVolume",
        boot_volume_id=boot_vol_id
    )

    create_vnic_details = oci.core.models.CreateVnicDetails(
        subnet_id=subnet_id,
        assign_public_ip=True,
        display_name="primary-vnic"
    )

    metadata = {}
    if ssh_public_key:
        metadata["ssh_authorized_keys"] = ssh_public_key.strip()

    # Configurations to test in each cycle: First 2 OCPU, then fallback to 1 OCPU
    candidate_specs = [
        {"ocpus": 2.0, "memory": 12.0, "label": "2 OCPU / 12 GB RAM"},
        {"ocpus": 1.0, "memory": 6.0, "label": "1 OCPU / 6 GB RAM"}
    ]

    start_time = time.time()
    max_duration = 10800 # 3-hour continuous loop
    attempt = 1

    print(f"Starting OCI Multi-Tier Sniper (2 OCPU -> 1 OCPU fallback) in {ad}...", flush=True)

    while time.time() - start_time < max_duration:
        ts = time.strftime('%Y-%m-%d %H:%M:%S')
        instance_created = False
        created_id = None
        active_label = None

        for spec in candidate_specs:
            shape_cfg = oci.core.models.LaunchInstanceShapeConfigDetails(
                ocpus=spec["ocpus"],
                memory_in_gbs=spec["memory"]
            )
            launch_details = oci.core.models.LaunchInstanceDetails(
                availability_domain=ad,
                compartment_id=tenancy,
                display_name="instance-arm-primary",
                shape="VM.Standard.A1.Flex",
                shape_config=shape_cfg,
                source_details=source_details,
                create_vnic_details=create_vnic_details,
                metadata=metadata
            )

            success, inst_id, err_msg = try_launch(compute, launch_details, spec["label"])
            if success:
                instance_created = True
                created_id = inst_id
                active_label = spec["label"]
                print(f"[{ts}] *** SUCCESS! Created instance with {active_label}! ID: {created_id} ***", flush=True)
                break
            else:
                print(f"[{ts}] Attempt #{attempt} ({spec['label']}): {err_msg}", flush=True)

        if instance_created:
            print("Waiting for instance to reach RUNNING state...", flush=True)
            for _ in range(30):
                time.sleep(10)
                inst = compute.get_instance(created_id).data
                if inst.lifecycle_state == "RUNNING":
                    break

            public_ip = "Unknown"
            vnics = compute.list_vnic_attachments(compartment_id=tenancy, instance_id=created_id).data
            for va in vnics:
                v = vn_client.get_vnic(va.vnic_id).data
                public_ip = v.public_ip
                break

            msg = (
                f"🚀 *Oracle VPS muvaffaqiyatli ishga tushdi!*\n\n"
                f"📍 *Public IP:* `{public_ip}`\n"
                f"⚙️ *Specs:* {active_label} / 200 GB Disk\n"
                f"🔑 *SSH:* `ssh -i ~/.ssh/oci_key ubuntu@{public_ip}`"
            )
            print(msg, flush=True)
            send_telegram(msg)
            sys.exit(0)

        attempt += 1
        time.sleep(30)

    print("3-Hour loop completed. Automatically dispatching next cycle...", flush=True)
    trigger_next_run()

if __name__ == "__main__":
    main()
