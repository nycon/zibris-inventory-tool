"""VMware Cloud Director API client."""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any, Optional

import httpx

_METRIC_NAMES_USED = {"disk.used.latest", "disk.used"}
_METRIC_NAMES_PROV = {"disk.provisioned.latest", "disk.provisioned"}
_METRIC_SAMPLE_LOGGED = False


def _is_used_metric_name(name: str) -> bool:
    key = (name or "").strip().lower()
    if key in _METRIC_NAMES_USED:
        return True
    # per-disk variants e.g. disk.used.latest.0
    return key.startswith("disk.used.latest") or key.startswith("disk.used.")


def _is_prov_metric_name(name: str) -> bool:
    key = (name or "").strip().lower()
    if key in _METRIC_NAMES_PROV:
        return True
    return key.startswith("disk.provisioned.latest") or key.startswith("disk.provisioned.")


class VcdError(Exception):
    def __init__(self, message: str, status_code: int = 500):
        super().__init__(message)
        self.status_code = status_code


class VcdClient:
    def __init__(
        self,
        base_url: str,
        username: str,
        org: str,
        password: str,
        api_version: str = "39.1",
        timeout: float = 60.0,
    ):
        self.base_url = base_url.rstrip("/")
        self.username = username
        self.org = org
        self.password = password
        self.api_version = api_version
        self.timeout = timeout
        self.token: Optional[str] = None

    def _accept(self, json_plus: bool = False) -> str:
        if json_plus:
            return f"application/*+json;version={self.api_version}"
        return f"application/*;version={self.api_version}"

    def login(self) -> dict[str, Any]:
        url = f"{self.base_url}/cloudapi/1.0.0/sessions"
        with httpx.Client(timeout=self.timeout, verify=True) as client:
            resp = client.post(
                url,
                auth=(f"{self.username}@{self.org}", self.password),
                headers={
                    "Accept": self._accept(False),
                    "Content-Length": "0",
                },
                content=b"",
            )
        if resp.status_code != 200:
            detail = resp.text[:300]
            raise VcdError(
                f"Login fehlgeschlagen (HTTP {resp.status_code}): {detail}",
                status_code=resp.status_code,
            )
        token = resp.headers.get("x-vmware-vcloud-access-token")
        if not token:
            raise VcdError("Kein Access-Token in der Antwort.")
        self.token = token
        return resp.json()

    def _auth_headers(self, json_plus: bool = True) -> dict[str, str]:
        if not self.token:
            raise VcdError("Nicht eingeloggt.", status_code=401)
        return {
            "Authorization": f"Bearer {self.token}",
            "Accept": self._accept(json_plus),
        }

    def query_all(self, record_type: str, filter_expr: str = "", page_size: int = 128) -> list[dict]:
        records: list[dict] = []
        page = 1
        with httpx.Client(timeout=self.timeout, verify=True) as client:
            while True:
                params: dict[str, Any] = {
                    "type": record_type,
                    "format": "records",
                    "page": page,
                    "pageSize": page_size,
                }
                if filter_expr:
                    params["filter"] = filter_expr
                resp = client.get(
                    f"{self.base_url}/api/query",
                    params=params,
                    headers=self._auth_headers(True),
                )
                if resp.status_code != 200:
                    raise VcdError(
                        f"Query {record_type} fehlgeschlagen (HTTP {resp.status_code})",
                        status_code=resp.status_code,
                    )
                data = resp.json()
                batch = data.get("record") or []
                records.extend(batch)
                total = int(data.get("total") or 0)
                pages = max(1, (total + page_size - 1) // page_size)
                if page >= pages:
                    break
                page += 1
        return records

    def vm_disk_capacity_mb(
        self, vm_href: str, client: httpx.Client | None = None
    ) -> tuple[float, int, list[dict[str, Any]]]:
        """Total + count + individual disks (configured capacities)."""
        if not vm_href:
            return 0.0, 0, []
        url = f"{vm_href.rstrip('/')}/virtualHardwareSection/disks"
        owns = client is None
        http = client or httpx.Client(timeout=min(self.timeout, 45.0), verify=True)
        try:
            xml_headers = {
                "Authorization": f"Bearer {self.token}",
                "Accept": f"application/*+xml;version={self.api_version}",
            }
            resp_xml = http.get(url, headers=xml_headers)
            if resp_xml.status_code == 200 and resp_xml.text:
                disks = _parse_disks_from_xml(resp_xml.text)
                if disks:
                    total = sum(float(d["mib"]) for d in disks)
                    return total, len(disks), disks

            json_headers = {
                "Authorization": f"Bearer {self.token}",
                "Accept": f"application/*+json;version={self.api_version}",
            }
            resp = http.get(url, headers=json_headers)
            if resp.status_code == 200:
                try:
                    disks = _parse_disks_from_json(resp.json())
                    if disks:
                        total = sum(float(d["mib"]) for d in disks)
                        return total, len(disks), disks
                except Exception:  # noqa: BLE001
                    pass
            return 0.0, 0, []
        except Exception:  # noqa: BLE001
            return 0.0, 0, []
        finally:
            if owns:
                http.close()

    def vm_disk_usage_metrics(
        self, vm_href: str, client: httpx.Client | None = None
    ) -> dict[str, Any]:
        """Fetch disk.used / disk.provisioned from VM current metrics (if enabled)."""
        empty = {
            "ok": False,
            "status_code": 0,
            "used_kib": None,
            "provisioned_kib": None,
            "detail": "",
            "metric_names": [],
        }
        if not vm_href:
            return empty
        url = f"{vm_href.rstrip('/')}/metrics/current"
        owns = client is None
        http = client or httpx.Client(timeout=min(self.timeout, 45.0), verify=True)

        def _names_from_body(text: str) -> list[str]:
            names = re.findall(r'\bname\s*=\s*"([^"]+)"', text or "", flags=re.I)
            if not names:
                names = re.findall(r'"name"\s*:\s*"([^"]+)"', text or "", flags=re.I)
            # unique, keep order
            seen: set[str] = set()
            out: list[str] = []
            for n in names:
                if n not in seen:
                    seen.add(n)
                    out.append(n)
            return out

        try:
            headers_xml = {
                "Authorization": f"Bearer {self.token}",
                "Accept": f"application/*+xml;version={self.api_version}",
            }
            resp = http.get(url, headers=headers_xml)
            if resp.status_code == 200 and resp.text:
                parsed = _parse_disk_metrics(resp.text, resp.headers.get("content-type", ""))
                if parsed.get("used_kib") is not None or parsed.get("provisioned_kib") is not None:
                    parsed["ok"] = True
                    parsed["status_code"] = resp.status_code
                    parsed["metric_names"] = _names_from_body(resp.text)
                    return parsed

                headers_json = {
                    "Authorization": f"Bearer {self.token}",
                    "Accept": f"application/*+json;version={self.api_version}",
                }
                resp_j = http.get(url, headers=headers_json)
                if resp_j.status_code == 200 and resp_j.text:
                    try:
                        parsed_j = _parse_disk_metrics(
                            resp_j.text, resp_j.headers.get("content-type", "application/json")
                        )
                    except Exception as exc:  # noqa: BLE001
                        parsed_j = {"used_kib": None, "provisioned_kib": None, "detail": str(exc)}
                    if (
                        parsed_j.get("used_kib") is not None
                        or parsed_j.get("provisioned_kib") is not None
                    ):
                        parsed_j["ok"] = True
                        parsed_j["status_code"] = resp_j.status_code
                        parsed_j["metric_names"] = _names_from_body(resp_j.text)
                        return parsed_j
                    names = _names_from_body(resp.text) or _names_from_body(resp_j.text)
                    sample = re.sub(r"\s+", " ", resp.text)[:500]
                    return {
                        **empty,
                        "status_code": 200,
                        "metric_names": names,
                        "detail": f"no disk used/prov; names={names[:30]}; sample={sample}",
                    }

                names = _names_from_body(resp.text)
                sample = re.sub(r"\s+", " ", resp.text)[:500]
                return {
                    **empty,
                    "status_code": 200,
                    "metric_names": names,
                    "detail": f"no disk used/prov; names={names[:30]}; sample={sample}",
                }

            body = (resp.text or "")[:300]
            return {
                **empty,
                "status_code": resp.status_code,
                "detail": body or f"HTTP {resp.status_code}",
            }
        except Exception as exc:  # noqa: BLE001
            return {**empty, "detail": str(exc)}
        finally:
            if owns:
                http.close()

    def vm_ip_addresses(self, vm_href: str, client: httpx.Client | None = None) -> str:
        """NIC IPs from the VM network connection section. Query ipAddress is often empty."""
        if not vm_href:
            return ""
        url = f"{vm_href.rstrip('/')}/networkConnectionSection"
        owns = client is None
        http = client or httpx.Client(timeout=min(self.timeout, 30.0), verify=True)
        try:
            headers_json = {
                "Authorization": f"Bearer {self.token}",
                "Accept": f"application/*+json;version={self.api_version}",
            }
            resp = http.get(url, headers=headers_json)
            if resp.status_code == 200 and resp.text:
                try:
                    ips = _parse_ips_from_network(resp.text, resp.headers.get("content-type", "application/json"))
                except Exception:  # noqa: BLE001
                    ips = []
                if ips:
                    return ", ".join(ips)

            headers_xml = {
                "Authorization": f"Bearer {self.token}",
                "Accept": f"application/*+xml;version={self.api_version}",
            }
            resp_xml = http.get(url, headers=headers_xml)
            if resp_xml.status_code == 200 and resp_xml.text:
                ips = _parse_ips_from_network(
                    resp_xml.text, resp_xml.headers.get("content-type", "application/xml")
                )
                return ", ".join(ips)
            return ""
        except Exception:  # noqa: BLE001
            return ""
        finally:
            if owns:
                http.close()

    def enrich_vm_ips(self, inventory: list[dict], max_workers: int = 16) -> list[dict]:
        """Fill VM IPs when the query record did not include ipAddress."""
        targets: list[dict] = []
        for vapp in inventory:
            for vm in vapp.get("vms") or []:
                if vm.get("href") and not (vm.get("ip") or "").strip():
                    targets.append(vm)
        if not targets:
            return inventory

        def _one(vm: dict) -> tuple[str, str]:
            with httpx.Client(timeout=min(self.timeout, 30.0), verify=True) as client:
                return vm.get("href") or "", self.vm_ip_addresses(vm.get("href") or "", client=client)

        by_href: dict[str, str] = {}
        with ThreadPoolExecutor(max_workers=max_workers) as pool:
            futures = [pool.submit(_one, vm) for vm in targets]
            for fut in as_completed(futures):
                try:
                    href, ip = fut.result()
                except Exception:  # noqa: BLE001
                    continue
                if href:
                    by_href[href] = ip

        filled = 0
        for vapp in inventory:
            for vm in vapp.get("vms") or []:
                ip = by_href.get(vm.get("href") or "", "")
                if ip:
                    vm["ip"] = ip
                    filled += 1
        print(f"[zibris] VM IPs: {filled}/{len(targets)} via networkConnectionSection", flush=True)
        return inventory

    def enrich_assigned_disks(self, inventory: list[dict], max_workers: int = 16) -> list[dict]:
        """Configured HDD + optional datastore 'in use' metrics per VM."""
        targets: list[dict] = []
        for vapp in inventory:
            for vm in vapp.get("vms") or []:
                if vm.get("href"):
                    targets.append(vm)
        if not targets:
            for vapp in inventory:
                for vm in vapp.get("vms") or []:
                    vm["hdd_assigned_from_api"] = False
                    vm["hdd_disk_count"] = 0
                    vm["hdd_disks"] = []
                    vm["hdd_used_from_api"] = False
                    vm["hdd_used_mib"] = 0
                    vm["hdd_used_gib"] = 0.0
            return inventory

        def _one(
            vm: dict,
        ) -> tuple[str, float, int, list[dict[str, Any]], dict[str, Any], str]:
            with httpx.Client(timeout=min(self.timeout, 45.0), verify=True) as client:
                href = vm.get("href") or ""
                mb, count, disks = self.vm_disk_capacity_mb(href, client=client)
                metrics = self.vm_disk_usage_metrics(href, client=client)
                ip = (vm.get("ip") or "").strip() or self.vm_ip_addresses(href, client=client)
            return href, mb, count, disks, metrics, ip

        by_href: dict[str, tuple[float, int, list[dict[str, Any]], dict[str, Any], str]] = {}
        with ThreadPoolExecutor(max_workers=max_workers) as pool:
            futures = [pool.submit(_one, vm) for vm in targets]
            for fut in as_completed(futures):
                try:
                    href, mb, count, disks, metrics, ip = fut.result()
                except Exception:  # noqa: BLE001
                    continue
                if href:
                    by_href[href] = (mb, count, disks, metrics, ip)

        api_ok = 0
        used_ok = 0
        ip_ok = 0
        metric_status: Counter[str] = Counter()
        for vapp in inventory:
            assigned_total = 0.0
            used_total = 0.0
            used_any = False
            for vm in vapp.get("vms") or []:
                mb, count, disks, metrics, ip = by_href.get(
                    vm.get("href") or "",
                    (0.0, 0, [], {"ok": False, "status_code": 0, "detail": "missing"}, ""),
                )
                if ip:
                    vm["ip"] = ip
                    ip_ok += 1
                from_api = mb > 0 and bool(disks)
                if from_api:
                    api_ok += 1
                else:
                    mb = max(0.0, float(vm.get("hdd_mib") or 0))
                    count = 1 if mb else 0
                    disks = (
                        [{"name": "Disk", "mib": mb, "gib": mib_to_gib(mb)}] if mb else []
                    )
                vm["hdd_assigned_from_api"] = from_api
                vm["hdd_disk_count"] = int(count)
                vm["hdd_disks"] = disks
                vm["hdd_assigned_mib"] = int(round(mb)) if mb else 0
                vm["hdd_assigned_gib"] = mib_to_gib(mb)
                assigned_total += mb

                used_kib = metrics.get("used_kib") if isinstance(metrics, dict) else None
                metric_status[str(metrics.get("status_code") or metrics.get("detail") or "?")] += 1
                if metrics.get("ok") and used_kib is not None:
                    used_mib = float(used_kib) / 1024.0
                    vm["hdd_used_from_api"] = True
                    vm["hdd_used_mib"] = int(round(used_mib)) if used_mib else 0
                    vm["hdd_used_gib"] = mib_to_gib(used_mib)
                    used_ok += 1
                    used_any = True
                    used_total += used_mib
                else:
                    vm["hdd_used_from_api"] = False
                    vm["hdd_used_mib"] = 0
                    vm["hdd_used_gib"] = 0.0
                    vm["hdd_used_error"] = (
                        f"HTTP {metrics.get('status_code')}: {metrics.get('detail') or ''}"
                    )[:180]

            vapp["hdd_assigned_mib"] = int(round(assigned_total)) if assigned_total else 0
            vapp["hdd_assigned_gib"] = mib_to_gib(assigned_total)
            vapp["hdd_disk_count"] = sum(int(vm.get("hdd_disk_count") or 0) for vm in (vapp.get("vms") or []))
            vapp["hdd_assigned_from_api"] = any(
                vm.get("hdd_assigned_from_api") for vm in (vapp.get("vms") or [])
            )
            vapp["hdd_used_from_api"] = used_any
            vapp["hdd_used_mib"] = int(round(used_total)) if used_any else 0
            vapp["hdd_used_gib"] = mib_to_gib(used_total) if used_any else 0.0
            # vApp disk list = all VM disks flattened
            vapp_disks: list[dict[str, Any]] = []
            for vm in vapp.get("vms") or []:
                for d in vm.get("hdd_disks") or []:
                    vapp_disks.append(
                        {
                            "name": f"{short_disk_label(vm.get('name'))}: {d.get('name')}",
                            "mib": d.get("mib"),
                            "gib": d.get("gib"),
                        }
                    )
            vapp["hdd_disks"] = vapp_disks

        print(
            f"[zibris] HDD configured: {api_ok}/{len(targets)} VMs via disk API; "
            f"HDD in use (metrics): {used_ok}/{len(targets)}; "
            f"VM IPs: {ip_ok}/{len(targets)}; "
            f"metric_status={dict(metric_status)}; "
            f"Disk-Anzahlen={dict(Counter(len((vm.get('hdd_disks') or [])) for v in inventory for vm in (v.get('vms') or [])))}",
            flush=True,
        )
        if used_ok == 0:
            # Log one sample error so we can see metric names / response shape
            sample_detail = ""
            sample_names: list[str] = []
            for vapp in inventory:
                for vm in vapp.get("vms") or []:
                    if vm.get("hdd_used_error"):
                        sample_detail = str(vm.get("hdd_used_error"))
                        break
                if sample_detail:
                    break
            # Pull names from first cached metrics via by_href
            for _href, payload in by_href.items():
                metrics = payload[3] if len(payload) > 3 else {}
                if isinstance(metrics, dict) and metrics.get("detail"):
                    sample_detail = sample_detail or str(metrics.get("detail"))
                    sample_names = list(metrics.get("metric_names") or [])
                    break
            print(
                f"[zibris] HDD in use sample: names={sample_names}; detail={sample_detail[:600]}",
                flush=True,
            )
        for vapp in inventory:
            vapp["hdd_metrics_checked"] = True
        return inventory


def _usable_ip(value: Any) -> str:
    text = str(value or "").strip()
    if not text or text.lower() in {"none", "null", "n/a", "-", "0.0.0.0"}:
        return ""
    return text


def _parse_ips_from_network(body: str, content_type: str = "") -> list[str]:
    """Collect NIC IPs. Prefer configured addresses over external NAT addresses."""
    text = (body or "").strip()
    if not text:
        return []
    ct = (content_type or "").lower()
    primary: list[str] = []
    external: list[str] = []

    def _add(bucket: list[str], value: Any) -> None:
        ip = _usable_ip(value)
        if ip and ip not in bucket and ip not in primary:
            bucket.append(ip)

    if "json" in ct or text.startswith("{") or text.startswith("["):
        try:
            import json

            data = json.loads(text)
        except Exception:  # noqa: BLE001
            data = None
        connections: list[Any] = []
        if isinstance(data, dict):
            for key in ("networkConnection", "NetworkConnection"):
                val = data.get(key)
                if isinstance(val, list):
                    connections = val
                    break
                if isinstance(val, dict):
                    connections = [val]
                    break
        elif isinstance(data, list):
            connections = data
        for item in connections:
            if not isinstance(item, dict):
                continue
            _add(primary, item.get("ipAddress") or item.get("IpAddress"))
            _add(external, item.get("externalIpAddress") or item.get("ExternalIpAddress"))
        return primary or external

    for match in re.finditer(
        r"<(?:[\w.-]+:)?IpAddress>\s*([^<]+?)\s*</(?:[\w.-]+:)?IpAddress>",
        text,
        flags=re.I,
    ):
        _add(primary, match.group(1))
    for match in re.finditer(
        r"<(?:[\w.-]+:)?ExternalIpAddress>\s*([^<]+?)\s*</(?:[\w.-]+:)?ExternalIpAddress>",
        text,
        flags=re.I,
    ):
        _add(external, match.group(1))
    if not primary:
        for match in re.finditer(r'\bipAddress="([^"]+)"', text, flags=re.I):
            _add(primary, match.group(1))
    return primary or external


def mib_to_gib(value: Any) -> float:
    try:
        n = float(value or 0)
    except (TypeError, ValueError):
        return 0.0
    return round(n / 1024.0, 1)


def _metric_to_kib(name: str, unit: str, value: Any) -> tuple[str, float] | None:
    try:
        raw = float(value)
    except (TypeError, ValueError):
        return None
    u = (unit or "").strip().lower()
    if u in {"kilobyte", "kilobytes", "kb", "kib"}:
        kib = raw
    elif u in {"megabyte", "megabytes", "mb", "mib"}:
        kib = raw * 1024.0
    elif u in {"gigabyte", "gigabytes", "gb", "gib"}:
        kib = raw * 1024.0 * 1024.0
    elif u in {"byte", "bytes", "b"}:
        kib = raw / 1024.0
    else:
        # VCD docs: disk.*.latest is KILOBYTE; assume KB if unit missing
        kib = raw
    key = (name or "").strip().lower()
    # Zibris/VCD often returns per-disk series: disk.used.latest.0, .1, …
    if key in _METRIC_NAMES_USED or key.startswith("disk.used.latest"):
        return "used_kib", kib
    if key in _METRIC_NAMES_PROV or key.startswith("disk.provisioned.latest"):
        return "provisioned_kib", kib
    if key.startswith("disk.used"):
        return "used_kib", kib
    if key.startswith("disk.provisioned"):
        return "provisioned_kib", kib
    return None


def _parse_disk_metrics_json(data: Any) -> dict[str, Any]:
    out: dict[str, Any] = {"used_kib": None, "provisioned_kib": None, "detail": ""}
    metrics: list[Any] = []
    if isinstance(data, dict):
        for key in ("metric", "Metric", "metrics", "MetricValues", "values"):
            val = data.get(key)
            if isinstance(val, list):
                metrics = val
                break
            if isinstance(val, dict):
                metrics = [val]
                break
        if not metrics and "name" in data:
            metrics = [data]
    elif isinstance(data, list):
        metrics = data

    used_sum = 0.0
    prov_sum = 0.0
    used_any = False
    prov_any = False
    for item in metrics:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or item.get("Name") or "")
        unit = str(item.get("unit") or item.get("Unit") or "")
        value = item.get("value", item.get("Value"))
        mapped = _metric_to_kib(name, unit, value)
        if not mapped:
            continue
        if mapped[0] == "used_kib":
            used_sum += mapped[1]
            used_any = True
        elif mapped[0] == "provisioned_kib":
            prov_sum += mapped[1]
            prov_any = True
    if used_any:
        out["used_kib"] = used_sum
    if prov_any:
        out["provisioned_kib"] = prov_sum
    return out


def _parse_disk_metrics(body: str, content_type: str = "") -> dict[str, Any]:
    """Parse metrics XML or JSON body for disk used/provisioned."""
    ct = (content_type or "").lower()
    text = (body or "").strip()
    if not text:
        return {"used_kib": None, "provisioned_kib": None, "detail": "empty"}

    if "json" in ct or text.startswith("{") or text.startswith("["):
        try:
            import json

            return _parse_disk_metrics_json(json.loads(text))
        except Exception as exc:  # noqa: BLE001
            return {"used_kib": None, "provisioned_kib": None, "detail": str(exc)}

    out: dict[str, Any] = {"used_kib": None, "provisioned_kib": None, "detail": ""}
    used_sum = 0.0
    prov_sum = 0.0
    used_any = False
    prov_any = False

    def _accumulate(name: str, unit: str, value: Any) -> None:
        nonlocal used_sum, prov_sum, used_any, prov_any
        mapped = _metric_to_kib(name, unit, value)
        if not mapped:
            return
        if mapped[0] == "used_kib":
            used_sum += mapped[1]
            used_any = True
        elif mapped[0] == "provisioned_kib":
            prov_sum += mapped[1]
            prov_any = True

    # Attribute form: <Metric name="disk.used.latest.0" unit="KILOBYTE" value="123"/>
    for m in re.finditer(
        r'<(?:[\w.-]+:)?Metric\b([^>]*)/?>',
        text,
        flags=re.IGNORECASE,
    ):
        attrs = m.group(1)
        name_m = re.search(r'\bname\s*=\s*"([^"]+)"', attrs, flags=re.I)
        unit_m = re.search(r'\bunit\s*=\s*"([^"]+)"', attrs, flags=re.I)
        value_m = re.search(r'\bvalue\s*=\s*"([^"]+)"', attrs, flags=re.I)
        if not name_m or not value_m:
            continue
        _accumulate(
            name_m.group(1),
            unit_m.group(1) if unit_m else "",
            value_m.group(1),
        )

    if used_any or prov_any:
        if used_any:
            out["used_kib"] = used_sum
        if prov_any:
            out["provisioned_kib"] = prov_sum
        return out

    # ElementTree fallback (namespaced)
    try:
        root = ET.fromstring(text)
    except ET.ParseError:
        return out
    for el in root.iter():
        tag = el.tag.split("}")[-1] if "}" in el.tag else el.tag
        if tag.lower() != "metric":
            continue
        name = el.attrib.get("name") or el.attrib.get("Name") or ""
        unit = el.attrib.get("unit") or el.attrib.get("Unit") or ""
        value = el.attrib.get("value") or el.attrib.get("Value") or (el.text or "")
        _accumulate(name, unit, value)
    if used_any:
        out["used_kib"] = used_sum
    if prov_any:
        out["provisioned_kib"] = prov_sum
    return out

def _as_float(value: Any) -> float:
    try:
        return float(str(value).replace(",", "."))
    except (TypeError, ValueError):
        return 0.0


def _capacity_from_host_resource(host: Any) -> float:
    """Extract capacity (MB) from HostResource (dict, list, or str)."""
    if host is None:
        return 0.0
    if isinstance(host, list):
        for entry in host:
            cap = _capacity_from_host_resource(entry)
            if cap:
                return cap
        return 0.0
    if isinstance(host, str):
        return _as_float(host)
    if not isinstance(host, dict):
        return 0.0

    candidates: list[Any] = [
        host.get("capacity"),
        host.get("vcloud:capacity"),
        host.get("Capacity"),
        host.get("value"),
    ]
    other = host.get("otherAttributes") or host.get("other_attributes") or {}
    if isinstance(other, dict):
        for key, val in other.items():
            key_s = str(key)
            if key_s.endswith("capacity") or key_s == "capacity" or "capacity" in key_s.lower():
                candidates.append(val)
        candidates.extend(other.values())
    for key, val in host.items():
        key_s = str(key)
        if "capacity" in key_s.lower():
            candidates.append(val)

    for val in candidates:
        cap = _as_float(val)
        if cap > 0:
            return cap
    return 0.0


def _disk_capacity_mb_from_item(item: dict[str, Any]) -> float:
    """Capacity of a hard disk (ResourceType 17) in MB."""
    # 1) HostResource capacity is the configured size in MB (per disk)
    host = item.get("hostResource") or item.get("HostResource")
    cap = _capacity_from_host_resource(host)
    if cap > 0:
        return cap

    # 2) VirtualQuantity (usually bytes)
    qty = _as_float(item.get("virtualQuantity") or item.get("VirtualQuantity"))
    units = str(
        item.get("virtualQuantityUnits") or item.get("VirtualQuantityUnits") or "byte"
    ).lower()
    if qty > 0:
        if "giga" in units or units in {"gb", "gib"}:
            return qty * 1024
        if "mega" in units or units in {"mb", "mib"}:
            return qty
        if "kilo" in units or units in {"kb", "kib"}:
            return qty / 1024
        if "byte" in units or units in {"", "bytes", "b"}:
            return qty / (1024 * 1024)
        if qty >= 1_048_576:
            return qty / (1024 * 1024)
        return qty
    return 0.0


def _is_hard_disk_item(item: dict[str, Any]) -> bool:
    rtype = item.get("resourceType") or item.get("ResourceType")
    try:
        if int(float(rtype)) == 17:
            return True
    except (TypeError, ValueError):
        pass
    desc = str(item.get("description") or item.get("Description") or "").lower()
    name = str(item.get("elementName") or item.get("ElementName") or "").lower()
    return "hard disk" in desc or "hard disk" in name


def short_disk_label(name: Any) -> str:
    raw = str(name or "VM")
    return raw[:28] + ("…" if len(raw) > 28 else "")


def _disk_entry(name: str, mib: float, index: int) -> dict[str, Any]:
    label = (name or "").strip() or f"Hard disk {index}"
    return {"name": label, "mib": round(mib, 1), "gib": mib_to_gib(mib)}


def _parse_disks_from_json(data: Any) -> list[dict[str, Any]]:
    items: list[Any] = []
    if isinstance(data, dict):
        raw = (
            data.get("item")
            or data.get("Item")
            or data.get("rasdItems")
            or data.get("values")
            or []
        )
        if isinstance(raw, list):
            items = raw
        elif raw:
            items = [raw]
        if not items and "RasdItemsList" in data:
            return _parse_disks_from_json(data["RasdItemsList"])
    elif isinstance(data, list):
        items = data

    disks: list[dict[str, Any]] = []
    idx = 1
    for item in items:
        if not isinstance(item, dict) or not _is_hard_disk_item(item):
            continue
        cap = _disk_capacity_mb_from_item(item)
        if cap <= 0:
            continue
        name = str(item.get("elementName") or item.get("ElementName") or f"Hard disk {idx}")
        disks.append(_disk_entry(name, cap, idx))
        idx += 1
    return disks


def _parse_disks_from_xml(text: str) -> list[dict[str, Any]]:
    import re

    if not text:
        return []
    disks: list[dict[str, Any]] = []
    idx = 1
    # Namespace prefixes: <Item>, <rasd:Item>, <ovf:Item>, …
    item_blocks = re.findall(
        r"<(?:\w+:)?Item\b[\s\S]*?</(?:\w+:)?Item>",
        text,
        flags=re.IGNORECASE,
    )
    for block in item_blocks:
        if not re.search(
            r"<(?:\w+:)?ResourceType>\s*17\s*</(?:\w+:)?ResourceType>",
            block,
            flags=re.IGNORECASE,
        ):
            continue
        name_m = re.search(
            r"<(?:\w+:)?ElementName>\s*([^<]+)\s*</(?:\w+:)?ElementName>",
            block,
            flags=re.IGNORECASE,
        )
        name = (name_m.group(1).strip() if name_m else "") or f"Hard disk {idx}"
        cap = 0.0
        match = re.search(
            r'(?:[\w.]+:)?capacity\s*=\s*["\'](\d+(?:\.\d+)?)["\']',
            block,
            flags=re.IGNORECASE,
        )
        if match:
            cap = _as_float(match.group(1))
        if cap <= 0:
            qty_m = re.search(
                r"<(?:\w+:)?VirtualQuantity>\s*(\d+(?:\.\d+)?)\s*</(?:\w+:)?VirtualQuantity>",
                block,
                flags=re.IGNORECASE,
            )
            if qty_m:
                qty = _as_float(qty_m.group(1))
                cap = qty / (1024 * 1024) if qty >= 1_048_576 else qty
        if cap > 0:
            disks.append(_disk_entry(name, cap, idx))
            idx += 1

    if not disks:
        caps = re.findall(
            r'(?:[\w.]+:)?capacity\s*=\s*["\'](\d+(?:\.\d+)?)["\']',
            text,
            flags=re.IGNORECASE,
        )
        for i, c in enumerate(caps, start=1):
            cap = _as_float(c)
            if cap > 0:
                disks.append(_disk_entry(f"Hard disk {i}", cap, i))
    return disks


def _sum_disk_capacity_mb(data: Any) -> tuple[float, int]:
    disks = _parse_disks_from_json(data)
    total = sum(float(d["mib"]) for d in disks)
    return total, len(disks)


def _sum_disk_capacity_from_xml(text: str) -> tuple[float, int]:
    disks = _parse_disks_from_xml(text)
    total = sum(float(d["mib"]) for d in disks)
    return total, len(disks)


def _apply_assigned_fallback(inventory: list[dict]) -> None:
    for vapp in inventory:
        assigned_total = 0.0
        for vm in vapp.get("vms") or []:
            # hdd_mib is already without RAM
            mb = max(0.0, float(vm.get("hdd_mib") or 0))
            vm["hdd_assigned_mib"] = int(round(mb)) if mb else 0
            vm["hdd_assigned_gib"] = mib_to_gib(mb)
            assigned_total += mb
        vapp["hdd_assigned_mib"] = int(round(assigned_total)) if assigned_total else 0
        vapp["hdd_assigned_gib"] = mib_to_gib(assigned_total)


def normalize_inventory(
    vapps: list[dict],
    vms: list[dict],
) -> list[dict]:
    """Build nested inventory: vApp -> VMs with CPU/RAM/HDD."""
    by_href: dict[str, list[dict]] = {}
    by_name: dict[str, list[dict]] = {}

    for vm in vms:
        total_mib = float(vm.get("totalStorageAllocatedMb") or 0)
        ram_mib = float(vm.get("memoryMB") or 0)
        # VCD: totalStorageAllocatedMb = disks + snapshots + memory → subtract RAM
        hdd_used_mib = max(0.0, total_mib - ram_mib)
        # Placeholder for assigned until disk API runs (keep 0 so assigned ≠ used)
        assigned_mib = 0.0
        item = {
            "name": vm.get("name") or "-",
            "status": vm.get("status") or "-",
            "vdc": vm.get("vdcName") or "-",
            "vapp": vm.get("containerName") or "-",
            "os": vm.get("guestOs") or vm.get("detectedGuestOs") or "-",
            "cpu": int(vm.get("numberOfCpus") or 0),
            "ram_mib": int(ram_mib) if ram_mib else 0,
            "ram_gib": mib_to_gib(ram_mib),
            # Allocated HDD without RAM (VCD); may include snapshots
            "hdd_mib": int(round(hdd_used_mib)) if hdd_used_mib else 0,
            "hdd_gib": mib_to_gib(hdd_used_mib),
            "hdd_assigned_mib": 0,
            "hdd_assigned_gib": 0.0,
            "hdd_assigned_from_api": False,
            "hdd_used_mib": 0,
            "hdd_used_gib": 0.0,
            "hdd_used_from_api": False,
            "hdd_disk_count": 0,
            "hdd_disks": [],
            "network": vm.get("networkName") or "—",
            "ip": (vm.get("ipAddress") or "").strip(),
            "href": vm.get("href") or "",
        }
        href = vm.get("container") or ""
        name = vm.get("containerName") or ""
        if href:
            by_href.setdefault(href, []).append(item)
        if name:
            by_name.setdefault(name, []).append(item)

    inventory: list[dict] = []
    for vapp in sorted(vapps, key=lambda x: ((x.get("vdcName") or ""), (x.get("name") or ""))):
        children = list(by_href.get(vapp.get("href") or "", []))
        if not children:
            children = list(by_name.get(vapp.get("name") or "", []))
        total_mib = float(vapp.get("totalStorageAllocatedMb") or 0)
        ram_mib = float(vapp.get("memoryAllocationMB") or 0)
        # vApp total also includes memory → subtract for HDD
        hdd_used_from_vapp = max(0.0, total_mib - ram_mib)
        hdd_used_from_vms = sum(float(c.get("hdd_mib") or 0) for c in children)
        hdd_used_mib = hdd_used_from_vms if children else hdd_used_from_vapp
        # No fallback to allocated HDD – otherwise UI thinks disk details are ready
        assigned_mib = sum(float(c.get("hdd_assigned_mib") or 0) for c in children)
        inventory.append(
            {
                "name": vapp.get("name") or "-",
                "status": vapp.get("status") or "-",
                "vdc": vapp.get("vdcName") or "-",
                "cpu": int(vapp.get("numberOfCpus") or 0),
                "ram_mib": int(ram_mib) if ram_mib else 0,
                "ram_gib": mib_to_gib(ram_mib),
                "hdd_mib": int(round(hdd_used_mib)) if hdd_used_mib else 0,
                "hdd_gib": mib_to_gib(hdd_used_mib),
                "hdd_assigned_mib": int(round(assigned_mib)) if assigned_mib else 0,
                "hdd_assigned_gib": mib_to_gib(assigned_mib),
                "hdd_assigned_from_api": False,
                "hdd_used_mib": 0,
                "hdd_used_gib": 0.0,
                "hdd_used_from_api": False,
                "hdd_disks": [],
                "vm_count": len(children),
                "href": vapp.get("href") or "",
                "vms": children,
            }
        )
    return inventory


def flatten_rows(inventory: list[dict]) -> list[dict]:
    """One row per VM (or empty vApp) for CSV/XLS export."""
    rows: list[dict] = []

    def _disks_label(disks: Any) -> str:
        parts = []
        for d in disks or []:
            parts.append(f"{d.get('name')}: {d.get('gib')} GiB")
        return "; ".join(parts)

    def _used_export(obj: dict) -> Any:
        if obj.get("hdd_used_from_api"):
            return obj.get("hdd_used_gib") or 0
        return ""

    for vapp in inventory:
        if not vapp["vms"]:
            rows.append(
                {
                    "vapp": vapp["name"],
                    "vapp_status": vapp["status"],
                    "vdc": vapp["vdc"],
                    "vm": "",
                    "vm_status": "",
                    "os": "",
                    "cpu": vapp.get("cpu") or "",
                    "ram_gib": vapp.get("ram_gib") or "",
                    "hdd_assigned_gib": vapp.get("hdd_assigned_gib") or 0,
                    "hdd_disks": _disks_label(vapp.get("hdd_disks")),
                    "hdd_used_gib": _used_export(vapp),
                    "ip": "",
                    "network": "",
                }
            )
            continue
        for vm in vapp["vms"]:
            rows.append(
                {
                    "vapp": vapp["name"],
                    "vapp_status": vapp["status"],
                    "vdc": vapp["vdc"],
                    "vm": vm["name"],
                    "vm_status": vm["status"],
                    "os": vm["os"],
                    "cpu": vm["cpu"],
                    "ram_gib": vm["ram_gib"],
                    "hdd_assigned_gib": vm.get("hdd_assigned_gib") or 0,
                    "hdd_disks": _disks_label(vm.get("hdd_disks")),
                    "hdd_used_gib": _used_export(vm),
                    "ip": vm.get("ip") or "",
                    "network": vm["network"] if vm["network"] != "—" else "",
                }
            )
    return rows
