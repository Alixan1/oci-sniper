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

    shape_config = oci.core.models.LaunchInstanceShapeConfigDetails(
        ocpus=2.0,
        memory_in_gbs=12.0
    )

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

    launch_details = oci.core.models.LaunchInstanceDetails(
        availability_domain=ad,
        compartment_id=tenancy,
        display_name="instance-arm-primary",
        shape="VM.Standard.A1.Flex",
        shape_config=shape_config,
        source_details=source_details,
        create_vnic_details=create_vnic_details,
        metadata=metadata
    )

    start_time = time.time()
    # Run for up to 5 minutes per action run
    max_duration = 300 
    attempt = 1

    print(f"Starting OCI Auto-Sniper for AD: {ad}...", flush=True)

    while time.time() - start_time < max_duration:
        print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] Attempt #{attempt}: Launching instance...", flush=True)
        try:
            res = compute.launch_instance(launch_details).data
            instance_id = res.id
            print(f"SUCCESS! Instance created: {instance_id}", flush=True)
            print("Waiting for instance to be in RUNNING state...", flush=True)

            for _ in range(30):
                time.sleep(10)
                inst = compute.get_instance(instance_id).data
                if inst.lifecycle_state == "RUNNING":
                    break

            public_ip = "Unknown"
            vnics = compute.list_vnic_attachments(compartment_id=tenancy, instance_id=instance_id).data
            for va in vnics:
                v = vn_client.get_vnic(va.vnic_id).data
                public_ip = v.public_ip
                break

            msg = (
                f"🚀 *Oracle VPS muvaffaqiyatli ishga tushdi!*\n\n"
                f"📍 *Public IP:* `{public_ip}`\n"
                f"⚙️ *Specs:* 2 OCPU / 12 GB RAM / 200 GB Disk\n"
                f"🔑 *SSH:* `ssh -i ~/.ssh/oci_key ubuntu@{public_ip}`"
            )
            print(msg, flush=True)
            send_telegram(msg)
            sys.exit(0)

        except oci.exceptions.ServiceError as e:
            if "Out of host capacity" in str(e.message):
                print(f"Attempt #{attempt}: Out of host capacity. Retrying in 30 seconds...", flush=True)
            else:
                print(f"Attempt #{attempt}: OCI Error ({e.status}) {e.code} - {e.message}", flush=True)
        except Exception as e:
            print(f"Attempt #{attempt}: Unexpected error: {e}", flush=True)

        attempt += 1
        time.sleep(30)

    print("Workflow interval elapsed. Next scheduled run will continue automatically.", flush=True)

if __name__ == "__main__":
    main()
