#!/usr/bin/env python3
"""
Reproduce presync failure when site has site_config.json but no logs/ directory.

Frappe's DB layer opens site/logs/database.log on connect; if logs/ is missing
you get: [Errno 2] No such file or directory: '.../logs/database.log'

Usage:
  In a container (Frappe installed):
    python -m frappe_microservice.scripts.repro_presync_logs_enoent
  Or copy script into container and run:
    podman cp scripts/repro_presync_logs_enoent.py <container>:/tmp/
    podman exec <container> python /tmp/repro_presync_logs_enoent.py
  From repo root with bench/Frappe env:
    PYTHONPATH=. python scripts/repro_presync_logs_enoent.py

Step 1: Presync WITHOUT create_site_config (broken state) -> expect ENOENT warning.
Step 2: create_site_config (creates logs/) then presync -> no ENOENT.
"""
import os
import json

def main():
    site_name = "central.atxinvox.com.au"
    # Use real FRAPPE_SITES_PATH so frappe.init finds apps.txt; we only create one site dir (no logs/)
    sites_path = os.getenv("FRAPPE_SITES_PATH", "/app/sites")
    site_path = os.path.join(sites_path, site_name)
    os.makedirs(site_path, exist_ok=True)
    # Write site_config.json but do NOT create logs/ - this is the broken state
    config_file = os.path.join(site_path, "site_config.json")
    if not os.path.exists(config_file):
        config = {
            "db_host": os.getenv("DB_HOST", "localhost"),
            "db_port": int(os.getenv("DB_PORT", "3306")),
            "db_name": os.getenv("DB_NAME", "_central_site"),
            "db_user": os.getenv("DB_USER", "root"),
            "db_password": os.getenv("DB_PASSWORD", "changeme"),
        }
        with open(config_file, "w") as f:
            json.dump(config, f, indent=2)

    # Need doctypes or fixtures so presync doesn't return early
    doctypes_dir = os.path.join(sites_path, "_repro_doctypes", "dummy")
    os.makedirs(doctypes_dir, exist_ok=True)
    dummy_json = os.path.join(doctypes_dir, "dummy.json")
    if not os.path.exists(dummy_json):
        with open(dummy_json, "w") as f:
            json.dump({"name": "Dummy DT", "doctype": "DocType", "module": "Test"}, f)

    os.environ["FRAPPE_SITES_PATH"] = sites_path
    os.environ["FRAPPE_SITE"] = site_name
    os.environ["DOCTYPES_PATH"] = os.path.join(sites_path, "_repro_doctypes")
    os.environ["SERVICE_NAME"] = "repro-service"

    # Remove logs/ if present so we start in broken state
    logs_dir = os.path.join(site_path, "logs")
    if os.path.isdir(logs_dir):
        import shutil
        shutil.rmtree(logs_dir)

    print("Site path:", site_path)
    print("Has site_config.json:", os.path.exists(config_file))
    print("Has logs/ (before fix):", os.path.isdir(logs_dir))
    print()

    # --- Step 1: Presync WITHOUT create_site_config -> reproduces ENOENT ---
    print("=== Step 1: presync WITHOUT create_site_config (broken state) ===")
    from frappe_microservice.isolation import presync_service_doctypes
    presync_service_doctypes()
    print("(Check output above for: 'presync: cannot connect to DB ([Errno 2] ... database.log)')\n")

    # --- Step 2: create_site_config (creates logs/) then presync -> fixed ---
    print("=== Step 2: create_site_config (creates logs/) then presync ===")
    from frappe_microservice.site_config import create_site_config
    create_site_config()
    print("Has logs/ (after create_site_config):", os.path.isdir(logs_dir))
    presync_service_doctypes()
    print("Done. If Step 1 showed the ENOENT warning and Step 2 did not, repro is correct.")


if __name__ == "__main__":
    main()
