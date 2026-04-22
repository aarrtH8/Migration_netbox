#!/usr/bin/env python3
"""
NetBox Export Script - v4.4.6
Exports IPAM, Devices, Sites, and Virtualization data to JSON files (one per object type).
Uses the NetBox REST API directly (no third-party library required beyond requests).

Usage: python export_netbox.py --url https://netbox.example.com --token <api_token> --output-dir ./export
"""

import argparse
import json
import os
import sys
import traceback
from pathlib import Path

import requests


def parse_args():
    parser = argparse.ArgumentParser(description="Export NetBox data to JSON files")
    parser.add_argument("--url", required=True, help="NetBox instance URL (e.g. https://netbox.example.com)")
    parser.add_argument("--token", required=True, help="NetBox API token")
    parser.add_argument("--output-dir", required=True, help="Directory where JSON files will be written")
    parser.add_argument("--no-verify", action="store_true", help="Disable SSL certificate verification")
    return parser.parse_args()


def make_session(token: str, verify: bool) -> requests.Session:
    session = requests.Session()
    session.headers.update({
        "Authorization": f"Token {token}",
        "Accept": "application/json",
    })
    session.verify = verify
    return session


def fetch_all(session: requests.Session, base_url: str, app: str, resource: str) -> list:
    """Fetch every page from a NetBox REST endpoint and return a flat list of dicts."""
    path = resource.replace("_", "-")
    url = f"{base_url}/api/{app}/{path}/"
    results = []
    params: dict = {"limit": 1000}
    while url:
        r = session.get(url, params=params)
        r.raise_for_status()
        data = r.json()
        results.extend(data.get("results", []))
        url = data.get("next")
        params = {}
    return results


def export_objects(session: requests.Session, base_url: str, app: str, resource: str, label: str) -> list:
    print(f"  Exporting {label}...", end=" ", flush=True)
    try:
        result = fetch_all(session, base_url, app, resource)
        print(f"{len(result)} records")
        return result
    except Exception as e:
        print(f"ERROR: {e}")
        traceback.print_exc()
        return []


def save_json(data, output_dir, filename):
    filepath = Path(output_dir) / filename
    with open(filepath, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, default=str)
    return filepath


def main():
    args = parse_args()

    os.makedirs(args.output_dir, exist_ok=True)

    base_url = args.url.rstrip("/")
    print(f"Connecting to {base_url} ...")

    session = make_session(args.token, verify=not args.no_verify)

    try:
        r = session.get(f"{base_url}/api/status/")
        r.raise_for_status()
        print("Connection OK\n")
    except Exception as e:
        print(f"Cannot connect to NetBox: {e}")
        sys.exit(1)

    def exp(app, resource, label):
        return export_objects(session, base_url, app, resource, label)

    exports = {}

    # ─── DCIM / Sites ───────────────────────────────────────────────────────────
    print("=== DCIM / Sites ===")
    exports["regions"]             = exp("dcim", "regions",      "Regions")
    exports["site_groups"]         = exp("dcim", "site_groups",  "Site Groups")
    exports["sites"]               = exp("dcim", "sites",        "Sites")
    exports["locations"]           = exp("dcim", "locations",    "Locations")
    exports["rack_roles"]          = exp("dcim", "rack_roles",   "Rack Roles")
    exports["racks"]               = exp("dcim", "racks",        "Racks")

    # ─── Tenancy ────────────────────────────────────────────────────────────────
    print("\n=== Tenancy ===")
    exports["tenant_groups"]       = exp("tenancy", "tenant_groups", "Tenant Groups")
    exports["tenants"]             = exp("tenancy", "tenants",       "Tenants")

    # ─── DCIM / Devices ─────────────────────────────────────────────────────────
    print("\n=== DCIM / Devices ===")
    exports["manufacturers"]           = exp("dcim", "manufacturers",           "Manufacturers")
    exports["device_types"]            = exp("dcim", "device_types",            "Device Types")
    exports["module_types"]            = exp("dcim", "module_types",            "Module Types")
    exports["device_roles"]            = exp("dcim", "device_roles",            "Device Roles")
    exports["platforms"]               = exp("dcim", "platforms",               "Platforms")
    exports["virtual_chassis"]         = exp("dcim", "virtual_chassis",         "Virtual Chassis")
    exports["devices"]                 = exp("dcim", "devices",                 "Devices")
    exports["modules"]                 = exp("dcim", "modules",                 "Modules")
    exports["interfaces"]              = exp("dcim", "interfaces",              "Interfaces")
    exports["front_ports"]             = exp("dcim", "front_ports",             "Front Ports")
    exports["rear_ports"]              = exp("dcim", "rear_ports",              "Rear Ports")
    exports["console_ports"]           = exp("dcim", "console_ports",           "Console Ports")
    exports["console_server_ports"]    = exp("dcim", "console_server_ports",    "Console Server Ports")
    exports["power_ports"]             = exp("dcim", "power_ports",             "Power Ports")
    exports["power_outlets"]           = exp("dcim", "power_outlets",           "Power Outlets")
    exports["device_bays"]             = exp("dcim", "device_bays",             "Device Bays")
    exports["inventory_items"]         = exp("dcim", "inventory_items",         "Inventory Items")
    exports["cables"]                  = exp("dcim", "cables",                  "Cables")
    exports["power_feeds"]             = exp("dcim", "power_feeds",             "Power Feeds")
    exports["power_panels"]            = exp("dcim", "power_panels",            "Power Panels")
    exports["virtual_device_contexts"] = exp("dcim", "virtual_device_contexts", "Virtual Device Contexts")

    # ─── Virtualization ─────────────────────────────────────────────────────────
    print("\n=== Virtualization ===")
    exports["cluster_types"]      = exp("virtualization", "cluster_types",    "Cluster Types")
    exports["clusters"]           = exp("virtualization", "clusters",         "Clusters")
    exports["virtual_machines"]   = exp("virtualization", "virtual_machines", "Virtual Machines")
    exports["vm_interfaces"]      = exp("virtualization", "interfaces",       "VM Interfaces")

    # ─── IPAM ───────────────────────────────────────────────────────────────────
    print("\n=== IPAM ===")
    exports["rirs"]                   = exp("ipam", "rirs",                    "RIRs")
    exports["asn_ranges"]             = exp("ipam", "asn_ranges",              "ASN Ranges")
    exports["asns"]                   = exp("ipam", "asns",                    "ASNs")
    exports["aggregates"]             = exp("ipam", "aggregates",              "Aggregates")
    exports["roles"]                  = exp("ipam", "roles",                   "Roles (IPAM)")
    exports["vlan_groups"]            = exp("ipam", "vlan_groups",             "VLAN Groups")
    exports["vlans"]                  = exp("ipam", "vlans",                   "VLANs")
    exports["vrfs"]                   = exp("ipam", "vrfs",                    "VRFs")
    exports["route_targets"]          = exp("ipam", "route_targets",           "Route Targets")
    exports["prefixes"]               = exp("ipam", "prefixes",                "Prefixes")
    exports["ip_ranges"]              = exp("ipam", "ip_ranges",               "IP Ranges")
    exports["ip_addresses"]           = exp("ipam", "ip_addresses",            "IP Addresses")
    exports["services"]               = exp("ipam", "services",                "Services")
    exports["service_templates"]      = exp("ipam", "service_templates",       "Service Templates")
    exports["fhrp_groups"]            = exp("ipam", "fhrp_groups",             "FHRP Groups")
    exports["fhrp_group_assignments"] = exp("ipam", "fhrp_group_assignments",  "FHRP Group Assignments")

    # ─── Write files ────────────────────────────────────────────────────────────
    print("\n=== Writing files ===")
    for key, data in exports.items():
        filepath = save_json(data, args.output_dir, f"{key}.json")
        print(f"  Wrote {filepath}  ({len(data)} records)")

    print(f"\nExport complete. Files written to: {args.output_dir}")


if __name__ == "__main__":
    main()
