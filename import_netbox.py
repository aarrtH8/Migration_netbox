#!/usr/bin/env python3
"""
NetBox Import Script - v4.4.6
Imports data exported by export_netbox.py into a target NetBox instance.
Uses the NetBox REST API directly (no third-party library required beyond requests).
Errors are logged to import_errors.log; processing continues on failure.

Usage: python import_netbox.py --url https://netbox2.example.com --token <api_token> --input-dir ./export
"""

import argparse
import json
import logging
import os
import sys
import traceback
from pathlib import Path

import requests

BATCH_SIZE = 50          # objects per bulk-create POST
LOOKUP_BATCH_SIZE = 200  # objects per bulk-lookup GET


# ─── NetBox REST client ──────────────────────────────────────────────────────────

class Record(dict):
    """Dict with attribute-style read access (rec.id, rec.name, …)."""
    def __getattr__(self, key):
        try:
            return self[key]
        except KeyError:
            raise AttributeError(key)
    def __setattr__(self, key, value):
        self[key] = value


class Endpoint:
    """Thin wrapper around one NetBox REST endpoint path."""

    def __init__(self, session: requests.Session, base_url: str, path: str):
        self._session = session
        self._url = f"{base_url}/api/{path}/"

    def _pages(self, url, params):
        results = []
        while url:
            r = self._session.get(url, params=params)
            self._raise(r)
            data = r.json()
            results.extend(Record(obj) for obj in data.get("results", []))
            url = data.get("next")
            params = {}
        return results

    @staticmethod
    def _raise(r):
        if not r.ok:
            try:
                detail = r.json()
            except Exception:
                detail = r.text
            raise requests.HTTPError(
                f"HTTP {r.status_code} {r.url}: {detail}", response=r
            )

    def filter(self, **params):
        return self._pages(self._url, {**params, "limit": 1000})

    def get(self, **params):
        hits = self.filter(**params)
        return hits[0] if hits else None

    def create(self, **payload):
        r = self._session.post(self._url, json=payload)
        self._raise(r)
        return Record(r.json())

    def patch(self, obj_id, **payload):
        r = self._session.patch(f"{self._url}{obj_id}/", json=payload)
        self._raise(r)
        return Record(r.json())

    def create_bulk(self, payloads: list) -> list:
        r = self._session.post(self._url, json=payloads)
        self._raise(r)
        data = r.json()
        return [Record(obj) for obj in (data if isinstance(data, list) else [data])]


class _App:
    """Resolves nb.dcim.devices → Endpoint('dcim/devices')."""
    def __init__(self, session, base_url, app):
        self._session = session
        self._base_url = base_url
        self._app = app

    def __getattr__(self, resource):
        if resource.startswith("_"):
            raise AttributeError(resource)
        path = f"{self._app}/{resource.replace('_', '-')}"
        return Endpoint(self._session, self._base_url, path)


class NetBoxClient:
    def __init__(self, url: str, token: str, verify: bool = True):
        self._base_url = url.rstrip("/")
        self._session = requests.Session()
        self._session.headers.update({
            "Authorization": f"Token {token}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        })
        self._session.verify = verify
        self.dcim           = _App(self._session, self._base_url, "dcim")
        self.ipam           = _App(self._session, self._base_url, "ipam")
        self.tenancy        = _App(self._session, self._base_url, "tenancy")
        self.virtualization = _App(self._session, self._base_url, "virtualization")

    def status(self):
        r = self._session.get(f"{self._base_url}/api/status/")
        r.raise_for_status()
        return r.json()


# ─── Logging ────────────────────────────────────────────────────────────────────

def setup_logging(log_path):
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        handlers=[
            logging.FileHandler(log_path, encoding="utf-8"),
            logging.StreamHandler(sys.stdout),
        ],
    )
    return logging.getLogger("netbox_import")


# ─── Helpers ────────────────────────────────────────────────────────────────────

def load_json(input_dir, filename):
    filepath = Path(input_dir) / filename
    if not filepath.exists():
        return []
    with open(filepath, encoding="utf-8") as f:
        return json.load(f)


class IdMapper:
    """Maps old instance IDs → new instance IDs for each object type."""

    def __init__(self):
        self._maps: dict[str, dict[int, int]] = {}

    def register(self, type_name: str, old_id: int, new_id: int):
        self._maps.setdefault(type_name, {})[old_id] = new_id

    def get(self, type_name: str, old_id: int) -> int | None:
        return self._maps.get(type_name, {}).get(old_id)

    def remap(self, type_name: str, old_id: int) -> int:
        new_id = self.get(type_name, old_id)
        if new_id is None:
            raise KeyError(f"No mapping for {type_name} id={old_id}")
        return new_id


# ─── Generic import ─────────────────────────────────────────────────────────────

def import_objects(
    nb_endpoint,
    records: list,
    label: str,
    logger,
    id_mapper: IdMapper,
    type_name: str,
    payload_fn=None,
    lookup_fn=None,
):
    created = skipped = errors = 0

    # ── Phase 1 : lookup ─────────────────────────────────────────────────────
    # For known simple lookup functions use a single bulk GET instead of N individual calls.
    bulk_key_field = None
    existing_map = None  # {key_value: Record} when bulk lookup succeeds
    if lookup_fn in BULK_LOOKUP_MAP and records:
        bulk_key_field, bulk_fn = BULK_LOOKUP_MAP[lookup_fn]
        try:
            existing_map = bulk_fn(nb_endpoint, records)
        except Exception as e:
            logger.warning(f"[{label}] Bulk lookup failed ({e}), falling back to per-record lookups")

    to_create = []  # list of (old_id, display_name, payload)

    for rec in records:
        old_id = rec.get("id")
        display = rec.get("name", rec.get("address", rec.get("slug", old_id)))
        try:
            payload = payload_fn(rec, id_mapper) if payload_fn else clean_payload(rec)

            existing = None
            if existing_map is not None:
                existing = existing_map.get(rec.get(bulk_key_field))
            elif lookup_fn:
                try:
                    existing = lookup_fn(nb_endpoint, rec)
                except Exception:
                    pass

            if existing:
                skipped += 1
                if old_id is not None and type_name:
                    id_mapper.register(type_name, old_id, existing.id)
                logger.info(f"[{label}] SKIP (exists): {display}")
                continue

            to_create.append((old_id, display, payload))

        except Exception as e:
            errors += 1
            logger.error(
                f"[{label}] ERROR on record id={old_id} name={display}: {e}\n"
                f"{traceback.format_exc()}"
            )

    # ── Phase 2 : batch create ───────────────────────────────────────────────
    for i in range(0, len(to_create), BATCH_SIZE):
        chunk = to_create[i:i + BATCH_SIZE]
        payloads = [p for _, _, p in chunk]
        try:
            new_objs = nb_endpoint.create_bulk(payloads)
            for (old_id, display, _), new_obj in zip(chunk, new_objs):
                created += 1
                if old_id is not None and type_name:
                    id_mapper.register(type_name, old_id, new_obj.id)
                logger.info(f"[{label}] CREATED: {display}")
        except Exception as batch_err:
            logger.warning(
                f"[{label}] Batch [{i}:{i + len(chunk)}] failed ({batch_err}), retrying individually..."
            )
            for old_id, display, payload in chunk:
                try:
                    new_obj = nb_endpoint.create(**payload)
                    created += 1
                    if old_id is not None and type_name:
                        id_mapper.register(type_name, old_id, new_obj.id)
                    logger.info(f"[{label}] CREATED: {display}")
                except Exception as e:
                    errors += 1
                    logger.error(
                        f"[{label}] ERROR on record id={old_id} name={display}: {e}\n"
                        f"{traceback.format_exc()}"
                    )

    logger.info(f"[{label}] Done — created={created} skipped={skipped} errors={errors}")
    return created, skipped, errors


def preload_ids(endpoint, records, type_name, id_mapper, lookup_fn, logger):
    """Lookup existing objects on target and register ID mappings without creating anything."""
    if not lookup_fn:
        return
    loaded = missed = 0
    for rec in records:
        old_id = rec.get("id")
        if old_id is None:
            continue
        try:
            existing = lookup_fn(endpoint, rec)
            if existing:
                id_mapper.register(type_name, old_id, existing["id"])
                loaded += 1
            else:
                missed += 1
        except Exception:
            missed += 1
    logger.info(f"[{type_name}] Pre-loaded {loaded} mappings ({missed} not found on target)")


# ─── Lookup helpers ──────────────────────────────────────────────────────────────

def by_slug(ep, rec):
    return ep.get(slug=rec["slug"]) if rec.get("slug") else None

def by_name(ep, rec):
    return ep.get(name=rec["name"]) if rec.get("name") else None

def by_prefix(ep, rec):
    hits = ep.filter(prefix=rec["prefix"])
    return hits[0] if hits else None

def by_address(ep, rec):
    hits = ep.filter(address=rec["address"])
    return hits[0] if hits else None

def by_vid_group(ep, rec):
    params = {"vid": rec["vid"]}
    if rec.get("group") and rec["group"].get("id"):
        params["group_id"] = rec["group"]["id"]
    hits = ep.filter(**params)
    return hits[0] if hits else None


# ─── Bulk lookup helpers ─────────────────────────────────────────────────────────

def _bulk_fetch(ep, field, values):
    """Single multi-value GET per LOOKUP_BATCH_SIZE records; returns {value: Record}."""
    values = [v for v in values if v is not None]
    if not values:
        return {}
    result = {}
    for i in range(0, len(values), LOOKUP_BATCH_SIZE):
        batch = values[i:i + LOOKUP_BATCH_SIZE]
        for obj in ep.filter(**{field: batch}):
            result[obj[field]] = obj
    return result

def bulk_by_slug(ep, records):
    return _bulk_fetch(ep, "slug", [r.get("slug") for r in records])

def bulk_by_name(ep, records):
    return _bulk_fetch(ep, "name", [r.get("name") for r in records])

def bulk_by_address(ep, records):
    return _bulk_fetch(ep, "address", [r.get("address") for r in records])

def bulk_by_prefix(ep, records):
    return _bulk_fetch(ep, "prefix", [r.get("prefix") for r in records])

# Maps simple per-record lookup functions → (key_field, bulk_equivalent)
# import_objects uses this to automatically switch to bulk lookup.
BULK_LOOKUP_MAP = {
    by_slug:    ("slug",    bulk_by_slug),
    by_name:    ("name",    bulk_by_name),
    by_address: ("address", bulk_by_address),
    by_prefix:  ("prefix",  bulk_by_prefix),
}


# ─── Payload builders ────────────────────────────────────────────────────────────

SKIP_FIELDS = {"id", "url", "display", "created", "last_updated",
               "custom_fields", "tags", "local_context_data"}

# Custom fields forcés à la création, quelle que soit la valeur source.
FORCED_CUSTOM_FIELDS = {"Squad": "choice1"}


def clean_payload(obj: dict) -> dict:
    result = {}
    for k, v in obj.items():
        if k in SKIP_FIELDS:
            continue
        if isinstance(v, dict) and "id" in v:
            result[k] = v["id"]
        elif isinstance(v, list):
            ids = [item["id"] for item in v if isinstance(item, dict) and "id" in item]
            result[k] = ids if ids else v
        else:
            result[k] = v
    return result


def build_payload_with_remap(rec, id_mapper: IdMapper, field_map: dict) -> dict:
    payload = {}
    for k, v in rec.items():
        if k in SKIP_FIELDS:
            continue
        if k not in field_map:
            if not isinstance(v, (dict, list)):
                payload[k] = v
            elif isinstance(v, dict) and "value" in v:
                payload[k] = v["value"]
            continue

        type_name = field_map[k]
        if type_name is None:
            payload[k] = v
            continue

        if isinstance(v, dict) and "id" in v:
            new_id = id_mapper.get(type_name, v["id"])
            if new_id is not None:
                payload[k] = new_id
        elif isinstance(v, list):
            ids = []
            for item in v:
                if isinstance(item, dict) and "id" in item:
                    new_id = id_mapper.get(type_name, item["id"])
                    ids.append(new_id if new_id is not None else item["id"])
                else:
                    ids.append(item)
            payload[k] = ids
        else:
            payload[k] = v

    for k in list(payload.keys()):
        if isinstance(payload[k], dict) and "value" in payload[k]:
            payload[k] = payload[k]["value"]

    return payload


# ─── Per-type payload builders ───────────────────────────────────────────────────

def payload_region(rec, im):
    return build_payload_with_remap(rec, im, {"parent": "regions"})

def payload_site_group(rec, im):
    return build_payload_with_remap(rec, im, {"parent": "site_groups"})

def payload_site(rec, im):
    return build_payload_with_remap(rec, im, {
        "region": "regions", "group": "site_groups", "tenant": "tenants", "asns": "asns",
    })

def payload_location(rec, im):
    return build_payload_with_remap(rec, im, {
        "site": "sites", "parent": "locations", "tenant": "tenants",
    })

def payload_rack(rec, im):
    return build_payload_with_remap(rec, im, {
        "site": "sites", "location": "locations", "tenant": "tenants", "role": "rack_roles",
    })

def payload_tenant_group(rec, im):
    return build_payload_with_remap(rec, im, {"parent": "tenant_groups"})

def payload_tenant(rec, im):
    return build_payload_with_remap(rec, im, {"group": "tenant_groups"})

def payload_manufacturer(rec, im):
    return build_payload_with_remap(rec, im, {})

def payload_device_type(rec, im):
    return build_payload_with_remap(rec, im, {"manufacturer": "manufacturers"})

def payload_module_type(rec, im):
    return build_payload_with_remap(rec, im, {"manufacturer": "manufacturers"})

def payload_device_role(rec, im):
    return build_payload_with_remap(rec, im, {})

def payload_platform(rec, im):
    return build_payload_with_remap(rec, im, {"manufacturer": "manufacturers"})

def payload_virtual_chassis(rec, im):
    # Create without master; master is set in a second pass after devices are imported.
    p = build_payload_with_remap(rec, im, {})
    p.pop("master", None)
    p.pop("member_count", None)
    p["custom_fields"] = FORCED_CUSTOM_FIELDS
    return p

def payload_cluster_type(rec, im):
    return build_payload_with_remap(rec, im, {})

def payload_cluster(rec, im):
    return build_payload_with_remap(rec, im, {
        "type": "cluster_types", "site": "sites", "group": "site_groups", "tenant": "tenants",
    })

def payload_virtual_machine(rec, im):
    p = build_payload_with_remap(rec, im, {
        "cluster": "clusters", "site": "sites", "role": "device_roles",
        "platform": "platforms", "tenant": "tenants",
        "primary_ip4": "ip_addresses", "primary_ip6": "ip_addresses",
    })
    p["custom_fields"] = FORCED_CUSTOM_FIELDS
    return p

def payload_vm_interface(rec, im):
    return build_payload_with_remap(rec, im, {
        "virtual_machine": "virtual_machines", "parent": "vm_interfaces",
        "bridge": "vm_interfaces", "untagged_vlan": "vlans",
        "tagged_vlans": "vlans", "vrf": "vrfs",
    })

def payload_virtual_device_context(rec, im):
    return build_payload_with_remap(rec, im, {
        "device": "devices", "tenant": "tenants",
        "primary_ip4": "ip_addresses", "primary_ip6": "ip_addresses",
    })

def payload_device(rec, im):
    p = build_payload_with_remap(rec, im, {
        "site": "sites", "rack": "racks", "location": "locations",
        "device_type": "device_types", "role": "device_roles",
        "platform": "platforms", "tenant": "tenants",
        "primary_ip4": "ip_addresses", "primary_ip6": "ip_addresses",
        "cluster": "clusters", "virtual_chassis": "virtual_chassis",
        "parent_device": "devices",
    })
    # primary_ip4/6 require the IP to already be assigned to a device interface;
    # interfaces don't exist yet at device creation time → applied in a third pass.
    p.pop("primary_ip4", None)
    p.pop("primary_ip6", None)
    p["custom_fields"] = FORCED_CUSTOM_FIELDS
    return p

def payload_interface(rec, im):
    return build_payload_with_remap(rec, im, {
        "device": "devices", "module": "modules",
        "parent": "interfaces", "bridge": "interfaces", "lag": "interfaces",
        "untagged_vlan": "vlans", "tagged_vlans": "vlans", "vrf": "vrfs",
    })

def payload_console_port(rec, im):
    return build_payload_with_remap(rec, im, {"device": "devices", "module": "modules"})

def payload_console_server_port(rec, im):
    return build_payload_with_remap(rec, im, {"device": "devices", "module": "modules"})

def payload_power_port(rec, im):
    return build_payload_with_remap(rec, im, {"device": "devices", "module": "modules"})

def payload_power_outlet(rec, im):
    return build_payload_with_remap(rec, im, {
        "device": "devices", "module": "modules", "power_port": "power_ports",
    })

def payload_front_port(rec, im):
    return build_payload_with_remap(rec, im, {
        "device": "devices", "module": "modules", "rear_port": "rear_ports",
    })

def payload_rear_port(rec, im):
    return build_payload_with_remap(rec, im, {"device": "devices", "module": "modules"})

def payload_device_bay(rec, im):
    return build_payload_with_remap(rec, im, {
        "device": "devices", "installed_device": "devices",
    })

def payload_inventory_item(rec, im):
    return build_payload_with_remap(rec, im, {
        "device": "devices", "parent": "inventory_items", "manufacturer": "manufacturers",
    })

def payload_module(rec, im):
    return build_payload_with_remap(rec, im, {
        "device": "devices", "module_bay": None, "module_type": "module_types",
    })

def payload_power_panel(rec, im):
    return build_payload_with_remap(rec, im, {"site": "sites", "location": "locations"})

def payload_power_feed(rec, im):
    return build_payload_with_remap(rec, im, {
        "power_panel": "power_panels", "rack": "racks", "tenant": "tenants",
    })

def payload_cable(rec, im):
    payload = {}
    for k, v in rec.items():
        if k in SKIP_FIELDS:
            continue
        if isinstance(v, dict) and "value" in v:
            payload[k] = v["value"]
        elif k not in ("a_terminations", "b_terminations"):
            payload[k] = v

    def remap_terminations(terminations):
        type_map = {
            "dcim.interface": "interfaces",
            "dcim.consoleport": "console_ports",
            "dcim.consoleserverport": "console_server_ports",
            "dcim.powerport": "power_ports",
            "dcim.poweroutlet": "power_outlets",
            "dcim.frontport": "front_ports",
            "dcim.rearport": "rear_ports",
            "dcim.powerfeed": "power_feeds",
        }
        result = []
        for t in (terminations or []):
            obj_type = t.get("object_type", "")
            obj_id = t.get("object_id") or (t.get("object", {}) or {}).get("id")
            mapped = type_map.get(obj_type)
            new_id = im.get(mapped, obj_id) if mapped and obj_id else obj_id
            result.append({"object_type": obj_type, "object_id": new_id})
        return result

    payload["a_terminations"] = remap_terminations(rec.get("a_terminations", []))
    payload["b_terminations"] = remap_terminations(rec.get("b_terminations", []))
    return payload

def payload_rir(rec, im):
    return build_payload_with_remap(rec, im, {})

def payload_asn_range(rec, im):
    return build_payload_with_remap(rec, im, {"rir": "rirs", "tenant": "tenants"})

def payload_asn(rec, im):
    p = build_payload_with_remap(rec, im, {"rir": "rirs", "tenant": "tenants"})
    p["custom_fields"] = FORCED_CUSTOM_FIELDS
    return p

def payload_aggregate(rec, im):
    return build_payload_with_remap(rec, im, {"rir": "rirs", "tenant": "tenants"})

def payload_ipam_role(rec, im):
    return build_payload_with_remap(rec, im, {})

def payload_vlan_group(rec, im):
    p = build_payload_with_remap(rec, im, {
        "site": "sites", "location": "locations", "rack": "racks",
        "cluster": "clusters", "tenant": "tenants",
    })
    p["custom_fields"] = FORCED_CUSTOM_FIELDS
    return p

def payload_vlan(rec, im):
    p = build_payload_with_remap(rec, im, {
        "site": "sites", "group": "vlan_groups", "tenant": "tenants", "role": "roles",
    })
    p["custom_fields"] = FORCED_CUSTOM_FIELDS
    return p

def payload_route_target(rec, im):
    return build_payload_with_remap(rec, im, {"tenant": "tenants"})

def payload_vrf(rec, im):
    p = build_payload_with_remap(rec, im, {
        "tenant": "tenants", "import_targets": "route_targets", "export_targets": "route_targets",
    })
    p["custom_fields"] = FORCED_CUSTOM_FIELDS
    return p

def payload_prefix(rec, im):
    p = build_payload_with_remap(rec, im, {
        "site": "sites", "vrf": "vrfs", "tenant": "tenants", "vlan": "vlans", "role": "roles",
    })
    p["custom_fields"] = FORCED_CUSTOM_FIELDS
    return p

def payload_ip_range(rec, im):
    p = build_payload_with_remap(rec, im, {
        "vrf": "vrfs", "tenant": "tenants", "role": "roles",
    })
    p["custom_fields"] = FORCED_CUSTOM_FIELDS
    return p

def payload_ip_address(rec, im):
    # assigned_object_type/id are scalars that slip through build_payload_with_remap;
    # they must be excluded here and applied in the second pass (update_ip_assignments).
    p = build_payload_with_remap(rec, im, {
        "vrf": "vrfs", "tenant": "tenants", "nat_inside": "ip_addresses",
    })
    p.pop("assigned_object_type", None)
    p.pop("assigned_object_id", None)
    p["custom_fields"] = FORCED_CUSTOM_FIELDS
    return p

def payload_service_template(rec, im):
    return build_payload_with_remap(rec, im, {"ipaddresses": "ip_addresses"})

def payload_service(rec, im):
    return build_payload_with_remap(rec, im, {
        "device": "devices", "virtual_machine": "virtual_machines",
        "ipaddresses": "ip_addresses",
    })

def payload_fhrp_group(rec, im):
    return build_payload_with_remap(rec, im, {})

def payload_fhrp_group_assignment(rec, im):
    IFACE_TYPE_MAP = {
        "dcim.interface": "interfaces",
        "virtualization.vminterface": "vm_interfaces",
    }
    payload = {}

    group = rec.get("group")
    if isinstance(group, dict) and "id" in group:
        new_group_id = im.get("fhrp_groups", group["id"])
        if new_group_id is not None:
            payload["group"] = new_group_id

    iface_type = rec.get("interface_type")
    iface_id = rec.get("interface_id")
    if not iface_id and isinstance(rec.get("interface"), dict):
        iface_id = rec["interface"].get("id")

    if iface_type and iface_id:
        mapper_type = IFACE_TYPE_MAP.get(iface_type)
        new_iface_id = im.get(mapper_type, iface_id) if mapper_type else iface_id
        if new_iface_id is not None:
            payload["interface_type"] = iface_type
            payload["interface_id"] = new_iface_id

    if rec.get("priority") is not None:
        payload["priority"] = rec["priority"]

    return payload


# ─── Second-pass update functions ────────────────────────────────────────────────

def update_virtual_chassis_masters(nb, records, im, logger):
    """Second pass: set the master device on each virtual chassis."""
    vc_ep = nb.dcim.virtual_chassis
    updated = skipped = errors = 0
    for rec in records:
        master = rec.get("master")
        if not master:
            continue
        old_master_id = master.get("id") if isinstance(master, dict) else master
        old_vc_id = rec.get("id")
        new_vc_id = im.get("virtual_chassis", old_vc_id)
        new_master_id = im.get("devices", old_master_id)
        if not new_vc_id or not new_master_id:
            logger.warning(f"[VC Master] Cannot remap vc_id={old_vc_id}, master device_id={old_master_id}")
            skipped += 1
            continue
        try:
            vc_ep.patch(new_vc_id, master=new_master_id)
            updated += 1
            logger.info(f"[VC Master] Set master for '{rec.get('name')}'")
        except Exception as e:
            errors += 1
            logger.error(f"[VC Master] ERROR vc_id={old_vc_id}: {e}\n{traceback.format_exc()}")
    logger.info(f"[Virtual Chassis Masters] Done — updated={updated} skipped={skipped} errors={errors}")
    return updated, skipped, errors


def update_ip_assignments(nb, records, im, logger):
    """Second pass: assign IP addresses to their interfaces (device or VM)."""
    ip_ep = nb.ipam.ip_addresses
    OBJECT_TYPE_MAP = {
        "dcim.interface": "interfaces",
        "virtualization.vminterface": "vm_interfaces",
    }
    updated = skipped = errors = 0
    for rec in records:
        obj_type = rec.get("assigned_object_type")
        obj_id = rec.get("assigned_object_id")
        old_ip_id = rec.get("id")
        if not obj_type or not obj_id:
            continue
        mapper_type = OBJECT_TYPE_MAP.get(obj_type)
        if not mapper_type:
            logger.warning(f"[IP Assignment] Unknown assigned_object_type: '{obj_type}' for ip_id={old_ip_id}")
            skipped += 1
            continue
        new_obj_id = im.get(mapper_type, obj_id)
        new_ip_id = im.get("ip_addresses", old_ip_id)
        if not new_obj_id or not new_ip_id:
            logger.warning(f"[IP Assignment] Cannot remap ip_id={old_ip_id} → {obj_type} id={obj_id}")
            skipped += 1
            continue
        try:
            ip_ep.patch(new_ip_id, assigned_object_type=obj_type, assigned_object_id=new_obj_id)
            updated += 1
            logger.info(f"[IP Assignment] {rec.get('address')} → {obj_type} id={new_obj_id}")
        except Exception as e:
            errors += 1
            logger.error(f"[IP Assignment] ERROR ip_id={old_ip_id}: {e}\n{traceback.format_exc()}")
    logger.info(f"[IP Assignments] Done — updated={updated} skipped={skipped} errors={errors}")
    return updated, skipped, errors


def update_device_primary_ips(nb, records, im, logger):
    """Third pass: set primary_ip4/primary_ip6 on devices after IPs are assigned to interfaces."""
    dev_ep = nb.dcim.devices
    updated = skipped = errors = 0
    for rec in records:
        old_dev_id = rec.get("id")
        new_dev_id = im.get("devices", old_dev_id)
        if not new_dev_id:
            skipped += 1
            continue

        patch = {}
        for field in ("primary_ip4", "primary_ip6"):
            ip = rec.get(field)
            if not ip:
                continue
            old_ip_id = ip.get("id") if isinstance(ip, dict) else ip
            new_ip_id = im.get("ip_addresses", old_ip_id)
            if new_ip_id:
                patch[field] = new_ip_id

        if not patch:
            skipped += 1
            continue

        try:
            dev_ep.patch(new_dev_id, **patch)
            updated += 1
            logger.info(f"[Device Primary IPs] Updated '{rec.get('name')}'")
        except Exception as e:
            errors += 1
            logger.error(f"[Device Primary IPs] ERROR device_id={old_dev_id}: {e}\n{traceback.format_exc()}")

    logger.info(f"[Device Primary IPs] Done — updated={updated} skipped={skipped} errors={errors}")
    return updated, skipped, errors


# ─── Main ────────────────────────────────────────────────────────────────────────

def parse_args():
    parser = argparse.ArgumentParser(description="Import NetBox data from JSON files")
    parser.add_argument("--url", required=True, help="Target NetBox URL")
    parser.add_argument("--token", required=True, help="Target NetBox API token")
    parser.add_argument("--input-dir", required=True, help="Directory containing exported JSON files")
    parser.add_argument("--log", default="import_errors.log", help="Log file path (default: import_errors.log)")
    parser.add_argument("--no-verify", action="store_true", help="Disable SSL certificate verification")
    parser.add_argument(
        "--skip-sections",
        default="",
        metavar="SECTIONS",
        help=(
            "Comma-separated list of sections to skip. "
            "Available: tenancy, sites, clusters, ipam, devices, "
            "virtual_machines, services, ip_assignments"
        ),
    )
    return parser.parse_args()


def main():
    args = parse_args()
    logger = setup_logging(args.log)
    im = IdMapper()

    logger.info(f"Connecting to {args.url} ...")
    nb = NetBoxClient(args.url, token=args.token, verify=not args.no_verify)

    try:
        nb.status()
        logger.info("Connection OK")
    except Exception as e:
        logger.error(f"Cannot connect to NetBox: {e}")
        sys.exit(1)

    def load(filename):
        return load_json(args.input_dir, filename)

    skip_sections = {s.strip().lower() for s in args.skip_sections.split(",") if s.strip()}
    if skip_sections:
        logger.info(f"Skipping sections: {', '.join(sorted(skip_sections))}")

    def active(section: str) -> bool:
        return section not in skip_sections

    total_created = total_skipped = total_errors = total_updated = 0

    def run(records, label, endpoint, type_name, payload_fn, lookup_fn=None):
        nonlocal total_created, total_skipped, total_errors
        c, s, e = import_objects(
            endpoint, records, label, logger, im, type_name,
            payload_fn=payload_fn, lookup_fn=lookup_fn,
        )
        total_created += c
        total_skipped += s
        total_errors += e

    def preload(records, endpoint, type_name, lookup_fn):
        preload_ids(endpoint, records, type_name, im, lookup_fn, logger)

    # ── Tenancy ──────────────────────────────────────────────────────────────────
    if active("tenancy"):
        logger.info("=== Tenancy ===")
        run(load("tenant_groups.json"), "Tenant Groups", nb.tenancy.tenant_groups, "tenant_groups", payload_tenant_group, by_slug)
        run(load("tenants.json"),       "Tenants",       nb.tenancy.tenants,       "tenants",        payload_tenant,       by_slug)
    else:
        logger.info("=== Tenancy [SKIPPED — preloading IDs] ===")
        preload(load("tenant_groups.json"), nb.tenancy.tenant_groups, "tenant_groups", by_slug)
        preload(load("tenants.json"),       nb.tenancy.tenants,       "tenants",        by_slug)

    # ── Regions / Sites ───────────────────────────────────────────────────────────
    if active("sites"):
        logger.info("=== Regions / Sites ===")
        run(load("regions.json"),     "Regions",     nb.dcim.regions,     "regions",     payload_region,       by_slug)
        run(load("site_groups.json"), "Site Groups", nb.dcim.site_groups, "site_groups", payload_site_group,   by_slug)
        run(load("sites.json"),       "Sites",       nb.dcim.sites,       "sites",       payload_site,         by_slug)
        run(load("locations.json"),   "Locations",   nb.dcim.locations,   "locations",   payload_location,     by_slug)
        run(load("rack_roles.json"),  "Rack Roles",  nb.dcim.rack_roles,  "rack_roles",  payload_manufacturer, by_slug)
        run(load("racks.json"),       "Racks",       nb.dcim.racks,       "racks",       payload_rack,
            lambda ep, rec: ep.get(name=rec["name"], site_id=im.get("sites", (rec.get("site") or {}).get("id")))
            if rec.get("name") and (rec.get("site") or {}).get("id") else by_name(ep, rec))
    else:
        logger.info("=== Regions / Sites [SKIPPED — preloading IDs] ===")
        preload(load("regions.json"),     nb.dcim.regions,     "regions",     by_slug)
        preload(load("site_groups.json"), nb.dcim.site_groups, "site_groups", by_slug)
        preload(load("sites.json"),       nb.dcim.sites,       "sites",       by_slug)
        preload(load("locations.json"),   nb.dcim.locations,   "locations",   by_slug)
        preload(load("rack_roles.json"),  nb.dcim.rack_roles,  "rack_roles",  by_slug)
        preload(load("racks.json"),       nb.dcim.racks,       "racks",
            lambda ep, rec: ep.get(name=rec["name"], site_id=im.get("sites", (rec.get("site") or {}).get("id")))
            if rec.get("name") and (rec.get("site") or {}).get("id") else by_name(ep, rec))

    # ── Virtualization / Clusters (before IPAM so vlan_groups can reference clusters) ──
    if active("clusters"):
        logger.info("=== Virtualization / Clusters ===")
        run(load("cluster_types.json"), "Cluster Types", nb.virtualization.cluster_types, "cluster_types", payload_cluster_type, by_slug)
        run(load("clusters.json"),      "Clusters",      nb.virtualization.clusters,      "clusters",      payload_cluster,      by_name)
    else:
        logger.info("=== Virtualization / Clusters [SKIPPED — preloading IDs] ===")
        preload(load("cluster_types.json"), nb.virtualization.cluster_types, "cluster_types", by_slug)
        preload(load("clusters.json"),      nb.virtualization.clusters,      "clusters",      by_name)

    # ── IPAM ─────────────────────────────────────────────────────────────────────
    # IP addresses are imported without assigned_object; assignments are applied
    # in a second pass after all interfaces (device + VM) are imported.
    ip_address_records = load("ip_addresses.json")
    if active("ipam"):
        logger.info("=== IPAM ===")
        run(load("rirs.json"),       "RIRs",       nb.ipam.rirs,       "rirs",       payload_rir,       by_slug)
        run(load("asn_ranges.json"), "ASN Ranges", nb.ipam.asn_ranges, "asn_ranges", payload_asn_range, by_name)
        run(load("asns.json"),       "ASNs",       nb.ipam.asns,       "asns",       payload_asn,
            lambda ep, rec: (ep.filter(asn=rec["asn"]) or [None])[0])
        run(load("aggregates.json"), "Aggregates", nb.ipam.aggregates, "aggregates", payload_aggregate,
            lambda ep, rec: (ep.filter(prefix=rec["prefix"]) or [None])[0])
        run(load("roles.json"),      "IPAM Roles", nb.ipam.roles,      "roles",      payload_ipam_role, by_slug)
        run(load("route_targets.json"), "Route Targets", nb.ipam.route_targets, "route_targets", payload_route_target,
            lambda ep, rec: (ep.filter(name=rec["name"]) or [None])[0])
        run(load("vrfs.json"), "VRFs", nb.ipam.vrfs, "vrfs", payload_vrf,
            lambda ep, rec: (ep.filter(name=rec["name"], rd=rec.get("rd")) or [None])[0])
        run(load("vlan_groups.json"), "VLAN Groups", nb.ipam.vlan_groups, "vlan_groups", payload_vlan_group, by_slug)
        run(load("vlans.json"), "VLANs", nb.ipam.vlans, "vlans", payload_vlan,
            lambda ep, rec: (ep.filter(
                vid=rec["vid"],
                **( {"group_id": im.get("vlan_groups", rec["group"]["id"])}
                    if rec.get("group") and rec["group"].get("id") else {} )
            ) or [None])[0])
        run(load("prefixes.json"),  "Prefixes",  nb.ipam.prefixes,  "prefixes",  payload_prefix,  by_prefix)
        run(load("ip_ranges.json"), "IP Ranges", nb.ipam.ip_ranges, "ip_ranges", payload_ip_range,
            lambda ep, rec: (ep.filter(start_address=rec["start_address"], end_address=rec["end_address"]) or [None])[0])
        run(ip_address_records, "IP Addresses", nb.ipam.ip_addresses, "ip_addresses", payload_ip_address, by_address)
        run(load("fhrp_groups.json"), "FHRP Groups", nb.ipam.fhrp_groups, "fhrp_groups", payload_fhrp_group, by_name)
    else:
        logger.info("=== IPAM [SKIPPED — preloading IDs] ===")
        preload(load("rirs.json"),       nb.ipam.rirs,       "rirs",       by_slug)
        preload(load("asn_ranges.json"), nb.ipam.asn_ranges, "asn_ranges", by_name)
        preload(load("asns.json"),       nb.ipam.asns,       "asns",
            lambda ep, rec: (ep.filter(asn=rec["asn"]) or [None])[0])
        preload(load("aggregates.json"), nb.ipam.aggregates, "aggregates",
            lambda ep, rec: (ep.filter(prefix=rec["prefix"]) or [None])[0])
        preload(load("roles.json"),      nb.ipam.roles,      "roles",      by_slug)
        preload(load("route_targets.json"), nb.ipam.route_targets, "route_targets",
            lambda ep, rec: (ep.filter(name=rec["name"]) or [None])[0])
        preload(load("vrfs.json"), nb.ipam.vrfs, "vrfs",
            lambda ep, rec: (ep.filter(name=rec["name"], rd=rec.get("rd")) or [None])[0])
        preload(load("vlan_groups.json"), nb.ipam.vlan_groups, "vlan_groups", by_slug)
        preload(load("vlans.json"), nb.ipam.vlans, "vlans",
            lambda ep, rec: (ep.filter(
                vid=rec["vid"],
                **( {"group_id": im.get("vlan_groups", rec["group"]["id"])}
                    if rec.get("group") and rec["group"].get("id") else {} )
            ) or [None])[0])
        preload(load("prefixes.json"),  nb.ipam.prefixes,  "prefixes",  by_prefix)
        preload(load("ip_ranges.json"), nb.ipam.ip_ranges, "ip_ranges",
            lambda ep, rec: (ep.filter(start_address=rec["start_address"], end_address=rec["end_address"]) or [None])[0])
        preload(ip_address_records, nb.ipam.ip_addresses, "ip_addresses", by_address)
        preload(load("fhrp_groups.json"), nb.ipam.fhrp_groups, "fhrp_groups", by_name)

    # ── DCIM / Devices ────────────────────────────────────────────────────────────
    virtual_chassis_records = load("virtual_chassis.json")
    if active("devices"):
        logger.info("=== DCIM / Devices ===")
        run(load("manufacturers.json"), "Manufacturers", nb.dcim.manufacturers, "manufacturers", payload_manufacturer, by_slug)
        run(load("device_types.json"),  "Device Types",  nb.dcim.device_types,  "device_types",  payload_device_type,
            lambda ep, rec: ep.get(
                manufacturer_id=im.get("manufacturers", (rec.get("manufacturer") or {}).get("id")),
                model=rec["model"]) if rec.get("manufacturer") else None)
        run(load("module_types.json"),  "Module Types",  nb.dcim.module_types,  "module_types",  payload_module_type,  by_name)
        run(load("device_roles.json"),  "Device Roles",  nb.dcim.device_roles,  "device_roles",  payload_device_role,  by_slug)
        run(load("platforms.json"),     "Platforms",     nb.dcim.platforms,     "platforms",     payload_platform,     by_slug)
        run(virtual_chassis_records, "Virtual Chassis", nb.dcim.virtual_chassis, "virtual_chassis", payload_virtual_chassis,
            lambda ep, rec: ep.get(name=rec["name"]) if rec.get("name") else None)
        run(load("devices.json"), "Devices", nb.dcim.devices, "devices", payload_device,
            lambda ep, rec: ep.get(name=rec["name"], site_id=im.get("sites", (rec.get("site") or {}).get("id"))))
        run(load("modules.json"),   "Modules",   nb.dcim.modules,   "modules",   payload_module,   None)
        run(load("interfaces.json"), "Interfaces", nb.dcim.interfaces, "interfaces", payload_interface,
            lambda ep, rec: ep.get(device_id=im.get("devices", (rec.get("device") or {}).get("id")), name=rec["name"]))
        run(load("console_ports.json"), "Console Ports", nb.dcim.console_ports, "console_ports", payload_console_port,
            lambda ep, rec: ep.get(device_id=im.get("devices", (rec.get("device") or {}).get("id")), name=rec["name"]))
        run(load("console_server_ports.json"), "Console Server Ports", nb.dcim.console_server_ports, "console_server_ports",
            payload_console_server_port,
            lambda ep, rec: ep.get(device_id=im.get("devices", (rec.get("device") or {}).get("id")), name=rec["name"]))
        run(load("power_ports.json"),   "Power Ports",   nb.dcim.power_ports,   "power_ports",   payload_power_port,
            lambda ep, rec: ep.get(device_id=im.get("devices", (rec.get("device") or {}).get("id")), name=rec["name"]))
        run(load("power_outlets.json"), "Power Outlets", nb.dcim.power_outlets, "power_outlets", payload_power_outlet,
            lambda ep, rec: ep.get(device_id=im.get("devices", (rec.get("device") or {}).get("id")), name=rec["name"]))
        run(load("rear_ports.json"),    "Rear Ports",    nb.dcim.rear_ports,    "rear_ports",    payload_rear_port,
            lambda ep, rec: ep.get(device_id=im.get("devices", (rec.get("device") or {}).get("id")), name=rec["name"]))
        run(load("front_ports.json"),   "Front Ports",   nb.dcim.front_ports,   "front_ports",   payload_front_port,
            lambda ep, rec: ep.get(device_id=im.get("devices", (rec.get("device") or {}).get("id")), name=rec["name"]))
        run(load("device_bays.json"),   "Device Bays",   nb.dcim.device_bays,   "device_bays",   payload_device_bay,
            lambda ep, rec: ep.get(device_id=im.get("devices", (rec.get("device") or {}).get("id")), name=rec["name"]))
        run(load("inventory_items.json"), "Inventory Items", nb.dcim.inventory_items, "inventory_items", payload_inventory_item, None)
        run(load("power_panels.json"),  "Power Panels",  nb.dcim.power_panels,  "power_panels",  payload_power_panel,  by_name)
        run(load("power_feeds.json"),   "Power Feeds",   nb.dcim.power_feeds,   "power_feeds",   payload_power_feed,   by_name)
        run(load("cables.json"),        "Cables",        nb.dcim.cables,        "cables",        payload_cable,        None)
        run(load("virtual_device_contexts.json"), "Virtual Device Contexts", nb.dcim.virtual_device_contexts,
            "virtual_device_contexts", payload_virtual_device_context,
            lambda ep, rec: ep.get(device_id=im.get("devices", (rec.get("device") or {}).get("id")), name=rec["name"]))
        logger.info("=== Virtual Chassis Masters (second pass) ===")
        u, s, e = update_virtual_chassis_masters(nb, virtual_chassis_records, im, logger)
        total_updated += u
        total_errors += e
    else:
        logger.info("=== DCIM / Devices [SKIPPED — preloading IDs] ===")
        preload(load("manufacturers.json"),  nb.dcim.manufacturers,  "manufacturers",  by_slug)
        preload(load("device_types.json"),   nb.dcim.device_types,   "device_types",
            lambda ep, rec: ep.get(
                manufacturer_id=im.get("manufacturers", (rec.get("manufacturer") or {}).get("id")),
                model=rec["model"]) if rec.get("manufacturer") else None)
        preload(load("module_types.json"),   nb.dcim.module_types,   "module_types",   by_name)
        preload(load("device_roles.json"),   nb.dcim.device_roles,   "device_roles",   by_slug)
        preload(load("platforms.json"),      nb.dcim.platforms,      "platforms",      by_slug)
        preload(virtual_chassis_records,     nb.dcim.virtual_chassis, "virtual_chassis",
            lambda ep, rec: ep.get(name=rec["name"]) if rec.get("name") else None)
        preload(load("devices.json"),        nb.dcim.devices,        "devices",
            lambda ep, rec: ep.get(name=rec["name"], site_id=im.get("sites", (rec.get("site") or {}).get("id"))))
        preload(load("interfaces.json"),     nb.dcim.interfaces,     "interfaces",
            lambda ep, rec: ep.get(device_id=im.get("devices", (rec.get("device") or {}).get("id")), name=rec["name"]))
        preload(load("console_ports.json"),  nb.dcim.console_ports,  "console_ports",
            lambda ep, rec: ep.get(device_id=im.get("devices", (rec.get("device") or {}).get("id")), name=rec["name"]))
        preload(load("console_server_ports.json"), nb.dcim.console_server_ports, "console_server_ports",
            lambda ep, rec: ep.get(device_id=im.get("devices", (rec.get("device") or {}).get("id")), name=rec["name"]))
        preload(load("power_ports.json"),    nb.dcim.power_ports,    "power_ports",
            lambda ep, rec: ep.get(device_id=im.get("devices", (rec.get("device") or {}).get("id")), name=rec["name"]))
        preload(load("power_outlets.json"),  nb.dcim.power_outlets,  "power_outlets",
            lambda ep, rec: ep.get(device_id=im.get("devices", (rec.get("device") or {}).get("id")), name=rec["name"]))
        preload(load("rear_ports.json"),     nb.dcim.rear_ports,     "rear_ports",
            lambda ep, rec: ep.get(device_id=im.get("devices", (rec.get("device") or {}).get("id")), name=rec["name"]))
        preload(load("front_ports.json"),    nb.dcim.front_ports,    "front_ports",
            lambda ep, rec: ep.get(device_id=im.get("devices", (rec.get("device") or {}).get("id")), name=rec["name"]))
        preload(load("device_bays.json"),    nb.dcim.device_bays,    "device_bays",
            lambda ep, rec: ep.get(device_id=im.get("devices", (rec.get("device") or {}).get("id")), name=rec["name"]))
        preload(load("power_panels.json"),   nb.dcim.power_panels,   "power_panels",   by_name)
        preload(load("power_feeds.json"),    nb.dcim.power_feeds,    "power_feeds",    by_name)
        preload(load("virtual_device_contexts.json"), nb.dcim.virtual_device_contexts, "virtual_device_contexts",
            lambda ep, rec: ep.get(device_id=im.get("devices", (rec.get("device") or {}).get("id")), name=rec["name"]))

    # ── Virtualization / Virtual Machines ─────────────────────────────────────────
    if active("virtual_machines"):
        logger.info("=== Virtualization / Virtual Machines ===")
        run(load("virtual_machines.json"), "Virtual Machines", nb.virtualization.virtual_machines, "virtual_machines",
            payload_virtual_machine,
            lambda ep, rec: (ep.filter(name=rec["name"]) or [None])[0])
        run(load("vm_interfaces.json"), "VM Interfaces", nb.virtualization.interfaces, "vm_interfaces",
            payload_vm_interface,
            lambda ep, rec: ep.get(
                virtual_machine_id=im.get("virtual_machines", (rec.get("virtual_machine") or {}).get("id")),
                name=rec["name"]))
    else:
        logger.info("=== Virtualization / Virtual Machines [SKIPPED — preloading IDs] ===")
        preload(load("virtual_machines.json"), nb.virtualization.virtual_machines, "virtual_machines",
            lambda ep, rec: (ep.filter(name=rec["name"]) or [None])[0])
        preload(load("vm_interfaces.json"), nb.virtualization.interfaces, "vm_interfaces",
            lambda ep, rec: ep.get(
                virtual_machine_id=im.get("virtual_machines", (rec.get("virtual_machine") or {}).get("id")),
                name=rec["name"]))

    # ── Services ─────────────────────────────────────────────────────────────────
    if active("services"):
        logger.info("=== Services ===")
        run(load("service_templates.json"), "Service Templates", nb.ipam.service_templates, "service_templates", payload_service_template, by_name)
        run(load("services.json"),          "Services",          nb.ipam.services,          "services",          payload_service, None)
        run(load("fhrp_group_assignments.json"), "FHRP Assignments", nb.ipam.fhrp_group_assignments,
            "fhrp_group_assignments", payload_fhrp_group_assignment, None)
    else:
        logger.info("=== Services [SKIPPED — preloading IDs] ===")
        preload(load("service_templates.json"), nb.ipam.service_templates, "service_templates", by_name)
        preload(load("fhrp_groups.json"),        nb.ipam.fhrp_groups,       "fhrp_groups",        by_name)

    # ── IP Address interface assignments (second pass) ────────────────────────────
    if active("ip_assignments"):
        logger.info("=== IP Address Assignments (second pass) ===")
        u, s, e = update_ip_assignments(nb, ip_address_records, im, logger)
        total_updated += u
        total_errors += e

        logger.info("=== Device Primary IPs (third pass) ===")
        u, s, e = update_device_primary_ips(nb, load("devices.json"), im, logger)
        total_updated += u
        total_errors += e
    else:
        logger.info("=== IP Address Assignments [SKIPPED] ===")

    logger.info(
        f"\n=== Import complete ===\n"
        f"  Total created  : {total_created}\n"
        f"  Total skipped  : {total_skipped}\n"
        f"  Total updated  : {total_updated}\n"
        f"  Total errors   : {total_errors}\n"
        f"  Log file       : {args.log}"
    )


if __name__ == "__main__":
    main()
