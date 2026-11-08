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

def try_launch(compute, launch_details, spec_label, ad_name):
    try:
        res = compute.launch_instance(launch_details).data
        return True, res.id, ""
    except oci.exceptions.ServiceError as e:
        if e.status == 429:
            time.sleep(15)
            return False, None, "Rate limited (429) - backed off 15s"
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

    # Canonical Ubuntu 24.04 aarch64 image in us-ashburn-1
    arm_img_id = os.getenv("IMAGE_OCID", "ocid1.image.oc1.iad.aaaaaaaabcwypxed4llxx3bkwdfsbmqhbmpxxa4jsiyanobo55rjbkftcr4a")
    subnet_id = os.getenv("SUBNET_OCID", "ocid1.subnet.oc1.iad.aaaaaaaacykkjtru6urue2upaxvt3pifv63zvp5fitfg2ui4lluayjjsufza")
    ssh_public_key = os.getenv("SSH_PUBLIC_KEY")

    ads = [
        "RbsL:US-ASHBURN-AD-1",
        "RbsL:US-ASHBURN-AD-2",
        "RbsL:US-ASHBURN-AD-3"
    ]

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

    compute = oci.core.ComputeClient(config, timeout=(10.0, 30.0))
    vn_client = oci.core.VirtualNetworkClient(config, timeout=(10.0, 30.0))

    # 150 GB boot volume keeps total tenancy storage under 200 GB
    source_details = oci.core.models.InstanceSourceViaImageDetails(
        source_type="image",
        image_id=arm_img_id,
        boot_volume_size_in_gbs=50
    )

    create_vnic_details = oci.core.models.CreateVnicDetails(
        subnet_id=subnet_id,
        assign_public_ip=True,
        display_name="primary-arm-vnic"
    )

    metadata = {}
    if ssh_public_key:
        metadata["ssh_authorized_keys"] = ssh_public_key.strip()

    candidate_specs = [
        {"ocpus": 2.0, "memory": 12.0, "label": "2 OCPU / 12 GB RAM"},
        {"ocpus": 1.0, "memory": 6.0, "label": "1 OCPU / 6 GB RAM"}
    ]

    start_time = time.time()
    max_duration = 10800 # 3-hour continuous loop
    attempt = 1

    print(f"Starting OCI Ultra-Aggressive Sniper (2s cycle, AD-1/AD-2/AD-3)...", flush=True)

    while time.time() - start_time < max_duration:
        ts = time.strftime('%Y-%m-%d %H:%M:%S')
        instance_created = False
        created_id = None
        active_label = None
        winning_ad = None

        for ad in ads:
            ad_short = ad.split(":")[-1]
            for spec in candidate_specs:
                shape_cfg = oci.core.models.LaunchInstanceShapeConfigDetails(
                    ocpus=spec["ocpus"],
                    memory_in_gbs=spec["memory"]
                )
                launch_details = oci.core.models.LaunchInstanceDetails(
                    availability_domain=ad,
                    compartment_id=tenancy,
                    display_name="oracle-arm-primary",
                    shape="VM.Standard.A1.Flex",
                    shape_config=shape_cfg,
                    source_details=source_details,
                    create_vnic_details=create_vnic_details,
                    metadata=metadata
                )

                success, inst_id, err_msg = try_launch(compute, launch_details, spec["label"], ad)
                if success:
                    instance_created = True
                    created_id = inst_id
                    active_label = spec["label"]
                    winning_ad = ad
                    print(f"[{ts}] *** SUCCESS! Created instance in {ad_short} with {active_label}! ID: {created_id} ***", flush=True)
                    break
                else:
                    print(f"[{ts}] Attempt #{attempt} [{ad_short}] ({spec['label']}): {err_msg}", flush=True)

            if instance_created:
                break

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
                f"🚀 *Oracle ARM VPS muvaffaqiyatli ishga tushdi!*\n\n"
                f"📍 *Public IP:* `{public_ip}`\n"
                f"⚙️ *Specs:* {active_label} / 150 GB Disk ({winning_ad})\n"
                f"🔑 *SSH:* `ssh -i ~/.ssh/oci_key ubuntu@{public_ip}`"
            )
            print(msg, flush=True)
            send_telegram(msg)
            sys.exit(0)

        attempt += 1
        time.sleep(3) # Safe 3-second interval to prevent 429

    print("3-Hour loop completed. Automatically dispatching next cycle...", flush=True)
    trigger_next_run()

if __name__ == "__main__":
    main()
