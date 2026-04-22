# NetBox Migration Scripts (v4.4.6)

Scripts d'export/import pour fusionner deux instances NetBox sur la même version.  
Seule dépendance : `requests`.

## Installation

```bash
pip install requests
```

---

## Export

```bash
python export_netbox.py \
  --url https://netbox-source.example.com \
  --token <API_TOKEN> \
  --output-dir ./export
```

| Option | Obligatoire | Description |
|---|---|---|
| `--url` | oui | URL de l'instance source |
| `--token` | oui | Token API NetBox |
| `--output-dir` | oui | Dossier de sortie des fichiers JSON |
| `--no-verify` | non | Désactive la vérification SSL (certificats auto-signés) |

Produit un fichier JSON par type d'objet dans `--output-dir`.

---

## Import

```bash
python import_netbox.py \
  --url https://netbox-target.example.com \
  --token <API_TOKEN> \
  --input-dir ./export \
  --log import_errors.log
```

| Option | Obligatoire | Description |
|---|---|---|
| `--url` | oui | URL de l'instance cible |
| `--token` | oui | Token API NetBox |
| `--input-dir` | oui | Dossier contenant les fichiers JSON exportés |
| `--log` | non | Chemin du fichier de log (défaut : `import_errors.log`) |
| `--no-verify` | non | Désactive la vérification SSL |
| `--skip-sections` | non | Sections à ignorer (voir ci-dessous) |

### Comportement

- Les objets qui **existent déjà** sur la cible sont **skippés** (pas d'écrasement).
- Les erreurs sont loggées et le traitement continue sur l'objet suivant.
- Un résumé `created / skipped / updated / errors` est affiché à la fin.
- Le custom field **Squad** est forcé à `choice1` (DC Network) sur tous les objets importés concernés.

### `--skip-sections`

Permet de sauter des sections entières, utile pour reprendre un import partiel ou ne rejouer qu'une partie.

```bash
python import_netbox.py \
  --url https://netbox-target.example.com \
  --token <API_TOKEN> \
  --input-dir ./export \
  --skip-sections tenancy,sites,clusters
```

| Valeur | Contenu |
|---|---|
| `tenancy` | Tenant Groups, Tenants |
| `sites` | Regions, Site Groups, Sites, Locations, Rack Roles, Racks |
| `clusters` | Cluster Types, Clusters |
| `ipam` | RIRs, ASN Ranges, ASNs, Aggregates, Roles, Route Targets, VRFs, VLAN Groups, VLANs, Prefixes, IP Ranges, IP Addresses, FHRP Groups |
| `devices` | Manufacturers, Device Types, Module Types, Device Roles, Platforms, Virtual Chassis, Devices, Modules, Interfaces, tous les ports, Device Bays, Inventory Items, Power Panels/Feeds, Cables, Virtual Device Contexts |
| `virtual_machines` | Virtual Machines, VM Interfaces |
| `services` | Service Templates, Services, FHRP Assignments |
| `ip_assignments` | Seconde passe d'assignation des IPs aux interfaces |

> **Note** : les sections skippées ne peuplent pas l'`IdMapper` interne. Si une section dépend d'une autre (ex: `devices` dépend de `ipam` pour les IPs primaires), sauter la section parente peut provoquer des erreurs de remappage dans la section enfant.

---

## Ordre d'import

```
Tenancy
  └─ Tenant Groups → Tenants

Sites
  └─ Regions → Site Groups → Sites → Locations → Rack Roles → Racks

Virtualization / Clusters
  └─ Cluster Types → Clusters

IPAM
  └─ RIRs → ASN Ranges → ASNs → Aggregates → Roles → Route Targets
     → VRFs → VLAN Groups → VLANs → Prefixes → IP Ranges
     → IP Addresses (sans assigned_object) → FHRP Groups

DCIM / Devices
  └─ Manufacturers → Device Types → Module Types → Device Roles → Platforms
     → Virtual Chassis (sans master) → Devices → Modules → Interfaces
     → Console Ports/Server Ports → Power Ports/Outlets → Rear/Front Ports
     → Device Bays → Inventory Items → Power Panels → Power Feeds → Cables
     → Virtual Device Contexts
     → [2e passe] Virtual Chassis masters

Virtualization / VMs
  └─ Virtual Machines → VM Interfaces

Services
  └─ Service Templates → Services → FHRP Assignments

[2e passe] IP Assignments
  └─ Assignation des IPs aux interfaces device et VM
```

---

## Objets exportés

| App | Objets |
|---|---|
| **DCIM** | Regions, Site Groups, Sites, Locations, Rack Roles, Racks, Manufacturers, Device Types, Module Types, Device Roles, Platforms, Virtual Chassis, Devices, Modules, Interfaces, Front/Rear Ports, Console Ports/Server Ports, Power Ports/Outlets, Device Bays, Inventory Items, Cables, Power Feeds, Power Panels, Virtual Device Contexts |
| **Tenancy** | Tenant Groups, Tenants |
| **Virtualization** | Cluster Types, Clusters, Virtual Machines, VM Interfaces |
| **IPAM** | RIRs, ASN Ranges, ASNs, Aggregates, Roles, VLAN Groups, VLANs, VRFs, Route Targets, Prefixes, IP Ranges, IP Addresses, Services, Service Templates, FHRP Groups, FHRP Group Assignments |
