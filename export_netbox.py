#!/usr/bin/env python3
"""
NetBox Export Script - v4.4.6
Exports IPAM, Devices, and Sites data to JSON files (one per object type).
Usage: python export_netbox.py --url https://netbox.example.com --token <api_token> --output-dir ./export
"""

import argparse
import json
import os
import sys
import traceback
from pathlib import Path

import pynetbox


def parse_args():
    parser = argparse.ArgumentParser(description="Export NetBox data to JSON files")
    parser.add_argument("--url", required=True, help="NetBox instance URL (e.g. https://netbox.example.com)")
    parser.add_argument("--token", required=True, help="NetBox API token")
    parser.add_argument("--output-dir", required=True, help="Directory where JSON files will be written")
    return parser.parse_args()


def record_to_dict(record):
    """Convert a pynetbox Record to a plain dict."""
    return dict(record)


def export_objects(nb_endpoint, label):
    """Fetch all objects from a pynetbox endpoint and return as list of dicts."""
    print(f"  Exporting {label}...", end=" ", flush=True)
    try:
        records = list(nb_endpoint.all())
        result = [record_to_dict(r) for r in records]
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

    print(f"Connecting to {args.url} ...")
    nb = pynetbox.api(args.url, token=args.token)
    nb.http_session.verify = True

    # Test connection
    try:
        nb.status()
        print("Connection OK\n")
    except Exception as e:
        print(f"Cannot connect to NetBox: {e}")
        sys.exit(1)

    exports = {}

    # ─── DCIM / Sites ───────────────────────────────────────────────────────────
    print("=== DCIM / Sites ===")
    exports["regions"]             = export_objects(nb.dcim.regions,        "Regions")
    exports["site_groups"]         = export_objects(nb.dcim.site_groups,    "Site Groups")
    exports["sites"]               = export_objects(nb.dcim.sites,          "Sites")
    exports["locations"]           = export_objects(nb.dcim.locations,      "Locations")
    exports["rack_roles"]          = export_objects(nb.dcim.rack_roles,     "Rack Roles")
    exports["racks"]               = export_objects(nb.dcim.racks,          "Racks")

    # ─── Tenancy ────────────────────────────────────────────────────────────────
    print("\n=== Tenancy ===")
    exports["tenant_groups"]       = export_objects(nb.tenancy.tenant_groups, "Tenant Groups")
    exports["tenants"]             = export_objects(nb.tenancy.tenants,       "Tenants")

    # ─── DCIM / Devices ─────────────────────────────────────────────────────────
    print("\n=== DCIM / Devices ===")
    exports["manufacturers"]           = export_objects(nb.dcim.manufacturers,           "Manufacturers")
    exports["device_types"]            = export_objects(nb.dcim.device_types,            "Device Types")
    exports["module_types"]            = export_objects(nb.dcim.module_types,            "Module Types")
    exports["device_roles"]            = export_objects(nb.dcim.device_roles,            "Device Roles")
    exports["platforms"]               = export_objects(nb.dcim.platforms,               "Platforms")
    exports["virtual_chassis"]         = export_objects(nb.dcim.virtual_chassis,         "Virtual Chassis")
    exports["devices"]                 = export_objects(nb.dcim.devices,                 "Devices")
    exports["modules"]                 = export_objects(nb.dcim.modules,                 "Modules")
    exports["interfaces"]              = export_objects(nb.dcim.interfaces,              "Interfaces")
    exports["front_ports"]             = export_objects(nb.dcim.front_ports,             "Front Ports")
    exports["rear_ports"]              = export_objects(nb.dcim.rear_ports,              "Rear Ports")
    exports["console_ports"]           = export_objects(nb.dcim.console_ports,           "Console Ports")
    exports["console_server_ports"]    = export_objects(nb.dcim.console_server_ports,    "Console Server Ports")
    exports["power_ports"]             = export_objects(nb.dcim.power_ports,             "Power Ports")
    exports["power_outlets"]           = export_objects(nb.dcim.power_outlets,           "Power Outlets")
    exports["device_bays"]             = export_objects(nb.dcim.device_bays,             "Device Bays")
    exports["inventory_items"]         = export_objects(nb.dcim.inventory_items,         "Inventory Items")
    exports["cables"]                  = export_objects(nb.dcim.cables,                  "Cables")
    exports["power_feeds"]             = export_objects(nb.dcim.power_feeds,             "Power Feeds")
    exports["power_panels"]            = export_objects(nb.dcim.power_panels,            "Power Panels")
    exports["virtual_device_contexts"] = export_objects(nb.dcim.virtual_device_contexts, "Virtual Device Contexts")

    # ─── Virtualization ─────────────────────────────────────────────────────────
    print("\n=== Virtualization ===")
    exports["cluster_types"]      = export_objects(nb.virtualization.cluster_types,    "Cluster Types")
    exports["clusters"]           = export_objects(nb.virtualization.clusters,         "Clusters")
    exports["virtual_machines"]   = export_objects(nb.virtualization.virtual_machines, "Virtual Machines")
    exports["vm_interfaces"]      = export_objects(nb.virtualization.interfaces,       "VM Interfaces")

    # ─── IPAM ───────────────────────────────────────────────────────────────────
    print("\n=== IPAM ===")
    exports["rirs"]                = export_objects(nb.ipam.rirs,            "RIRs")
    exports["asn_ranges"]          = export_objects(nb.ipam.asn_ranges,      "ASN Ranges")
    exports["asns"]                = export_objects(nb.ipam.asns,            "ASNs")
    exports["aggregates"]          = export_objects(nb.ipam.aggregates,      "Aggregates")
    exports["roles"]               = export_objects(nb.ipam.roles,           "Roles (IPAM)")
    exports["vlan_groups"]         = export_objects(nb.ipam.vlan_groups,     "VLAN Groups")
    exports["vlans"]               = export_objects(nb.ipam.vlans,           "VLANs")
    exports["vrfs"]                = export_objects(nb.ipam.vrfs,            "VRFs")
    exports["route_targets"]       = export_objects(nb.ipam.route_targets,   "Route Targets")
    exports["prefixes"]            = export_objects(nb.ipam.prefixes,        "Prefixes")
    exports["ip_ranges"]           = export_objects(nb.ipam.ip_ranges,       "IP Ranges")
    exports["ip_addresses"]        = export_objects(nb.ipam.ip_addresses,    "IP Addresses")
    exports["services"]            = export_objects(nb.ipam.services,        "Services")
    exports["service_templates"]   = export_objects(nb.ipam.service_templates, "Service Templates")
    exports["fhrp_groups"]         = export_objects(nb.ipam.fhrp_groups,     "FHRP Groups")
    exports["fhrp_group_assignments"] = export_objects(nb.ipam.fhrp_group_assignments, "FHRP Group Assignments")

    # ─── Write files ────────────────────────────────────────────────────────────
    print("\n=== Writing files ===")
    for key, data in exports.items():
        filepath = save_json(data, args.output_dir, f"{key}.json")
        print(f"  Wrote {filepath}  ({len(data)} records)")

    print(f"\nExport complete. Files written to: {args.output_dir}")


if __name__ == "__main__":
    main()
