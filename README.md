# NetBox Migration Scripts (v4.4.6)

Two scripts to export/import the full IPAM, Devices and Sites data between two NetBox instances running the same version.

## Installation

```bash
pip install -r requirements.txt
```

## Export (instance 1)

```bash
python export_netbox.py \
  --url https://netbox1.example.com \
  --token <API_TOKEN_1> \
  --output-dir ./export
```

Produces one JSON file per object type in `./export/`.

## Import (instance 2)

```bash
python import_netbox.py \
  --url https://netbox2.example.com \
  --token <API_TOKEN_2> \
  --input-dir ./export \
  --log import_errors.log
```

- Objects that already exist on the target are **skipped** (no overwrite).
- All errors are logged to `import_errors.log` and processing continues.
- A summary (created / skipped / errors) is printed at the end.

## Import order

The import respects dependency order:

1. Tenant Groups → Tenants  
2. Regions → Site Groups → Sites → Locations → Rack Roles → Racks  
3. IPAM: RIRs → ASN Ranges → ASNs → Aggregates → Roles → Route Targets → VRFs → VLAN Groups → VLANs → Prefixes → IP Ranges → IP Addresses → FHRP Groups  
4. DCIM: Manufacturers → Device Types → Module Types → Device Roles → Platforms → Devices → Modules → Interfaces → Ports → Device Bays → Inventory Items → Power Panels → Power Feeds → Cables  
5. Services → FHRP Assignments  
