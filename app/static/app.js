(() => {
  const $ = (sel) => document.querySelector(sel);

  const loginPanel = $("#login-panel");
  const appPanel = $("#app-panel");
  const topActions = $("#top-actions");
  const loading = $("#loading");
  const loadingText = $("#loading-text");
  const toast = $("#toast");
  const tbody = $("#inventory-body");
  const emptyState = $("#empty-state");
  const searchInput = $("#search");
  const statusFilter = $("#filter-status");
  const vdcFilter = $("#filter-vdc");
  const calcPanel = $("#calc-panel");
  const btnCalc = $("#btn-calc");
  const priceCpu = $("#price-cpu");
  const priceRam = $("#price-ram");
  const priceHdd = $("#price-hdd");
  const priceIp = $("#price-ip");
  const priceEdge = $("#price-edge");
  const includeEdge = $("#include-edge");
  const btnHddAssigned = $("#btn-hdd-assigned");

  let inventory = [];
  let inventoryAt = "";
  let openRows = new Set();
  let hddAssignedOn = false;
  const FETCH_TIMEOUT_MS = 180000;
  const PRICE_KEY = "zibris_vm_prices_v3";
  const HDD_ASSIGNED_KEY = "zibris_hdd_assigned_on_v1";

  function showLoading(text) {
    loadingText.textContent = text || "Lade…";
    loading.hidden = false;
  }

  function hideLoading() {
    loading.hidden = true;
  }

  function showToast(msg) {
    toast.textContent = msg;
    toast.hidden = false;
    clearTimeout(showToast._t);
    showToast._t = setTimeout(() => {
      toast.hidden = true;
    }, 3200);
  }

  function statusBadge(status) {
    const s = status || "-";
    let cls = "other";
    let label = s;
    if (s === "POWERED_ON") {
      cls = "on";
      label = "ON";
    } else if (s === "POWERED_OFF") {
      cls = "off";
      label = "OFF";
    } else if (s === "SUSPENDED") {
      cls = "other";
      label = "SUS";
    } else if (s === "RESOLVED") {
      cls = "other";
      label = "RES";
    }
    return `<span class="badge ${cls}" title="${escapeHtml(s)}">${escapeHtml(label)}</span>`;
  }

  function shortName(name) {
    const raw = String(name || "-");
    // UUID-Suffix ausblenden: name-xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx
    const cleaned = raw.replace(
      /-[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i,
      ""
    );
    return cleaned || raw;
  }

  function escapeHtml(value) {
    return String(value ?? "")
      .replaceAll("&", "&amp;")
      .replaceAll("<", "&lt;")
      .replaceAll(">", "&gt;")
      .replaceAll('"', "&quot;");
  }

  function fmtGib(n) {
    const v = Number(n || 0);
    if (!v) return "—";
    return `${Math.round(v * 10) / 10}\u00A0GiB`;
  }

  function fmtStorageTotal(n) {
    const v = Number(n || 0);
    if (!v) return "—";
    if (v >= 1024) {
      return `${Math.round((v / 1024) * 10) / 10}\u00A0TiB`;
    }
    return `${Math.round(v * 10) / 10}\u00A0GiB`;
  }

  function fmtDiskCell(totalGib, disks, fromApi) {
    const total = fmtGib(totalGib);
    const list = Array.isArray(disks) ? disks : [];
    if (!list.length) {
      return `<div class="disk-cell"><strong>${escapeHtml(total)}</strong></div>`;
    }
    const lines = list
      .map(
        (d, i) =>
          `<li><span>${escapeHtml(d.name || `Disk ${i + 1}`)}</span><em>${escapeHtml(
            fmtGib(d.gib)
          )}</em></li>`
      )
      .join("");
    return `<div class="disk-cell" title="${escapeHtml(
      fromApi ? `${list.length} Festplatte(n)` : "Schätzwert"
    )}">
      <strong class="disk-total">${escapeHtml(total)} <span class="disk-count">(${list.length}×)</span></strong>
      <ul class="disk-list">${lines}</ul>
    </div>`;
  }

  function updateExportLinks() {
    const params = new URLSearchParams();
    const vdc = vdcFilter.value || "";
    const status = statusFilter.value || "";
    if (vdc) params.set("vdc", vdc);
    if (status) params.set("status", status);
    params.set("price_cpu", String(parsePrice(priceCpu)));
    params.set("price_ram", String(parsePrice(priceRam)));
    params.set("price_hdd", String(parsePrice(priceHdd)));
    params.set("price_ip", String(parsePrice(priceIp)));
    params.set("price_edge", String(parsePrice(priceEdge)));
    params.set("include_edge", includeEdge?.checked ? "1" : "0");
    const q = `?${params.toString()}`;
    const csv = $("#btn-csv");
    const xlsx = $("#btn-xlsx");
    if (csv) csv.href = `/api/export.csv${q}`;
    if (xlsx) xlsx.href = `/api/export.xlsx${q}`;

    const bits = [];
    bits.push(vdc ? `VDC: ${vdc}` : "alle VDCs");
    if (status === "POWERED_ON") bits.push("nur ON");
    else if (status === "POWERED_OFF") bits.push("nur OFF");
    else if (status) bits.push(status);
    bits.push("inkl. Kosten");
    const label = bits.join(" · ");
    if (xlsx) xlsx.title = `Excel: ${label}`;
    if (csv) csv.title = `CSV: ${label}`;
  }

  function loadPrices() {
    try {
      const raw =
        localStorage.getItem(PRICE_KEY) ||
        localStorage.getItem("zibris_vm_prices_v2") ||
        localStorage.getItem("zibris_vm_prices_v1");
      if (!raw) return;
      const data = JSON.parse(raw);
      if (data.cpu != null) priceCpu.value = data.cpu;
      if (data.ram != null) priceRam.value = data.ram;
      if (data.hdd != null) priceHdd.value = data.hdd;
      if (data.ip != null) priceIp.value = data.ip;
      if (data.edge != null) priceEdge.value = data.edge;
      if (includeEdge && data.include_edge != null) {
        includeEdge.checked = Boolean(data.include_edge);
      }
    } catch {
      // ignore
    }
    updateExportLinks();
  }

  function savePrices() {
    localStorage.setItem(
      PRICE_KEY,
      JSON.stringify({
        cpu: priceCpu.value,
        ram: priceRam.value,
        hdd: priceHdd.value,
        ip: priceIp.value,
        edge: priceEdge.value,
        include_edge: Boolean(includeEdge?.checked),
      })
    );
  }

  function parsePrice(el) {
    const v = parseFloat(String(el.value || "").replace(",", "."));
    return Number.isFinite(v) && v >= 0 ? v : 0;
  }

  function fmtChf(n) {
    return new Intl.NumberFormat("de-CH", {
      style: "currency",
      currency: "CHF",
      minimumFractionDigits: 2,
      maximumFractionDigits: 2,
    }).format(n || 0);
  }

  function visibleResources() {
    const status = statusFilter.value;
    const vdc = vdcFilter.value;
    const q = (searchInput.value || "").trim().toLowerCase();
    const visible = inventory.filter((v) => matches(v, q, status, vdc));
    let cpu = 0;
    let ram = 0;
    let storage = 0;
    let storageAssigned = 0;
    let storageUsed = 0;
    let storageUsedAny = false;
    let vms = 0;
    // Annahme: 1 externe IP pro VDC
    const ips = new Set(visible.map((v) => v.vdc).filter(Boolean)).size;
    for (const v of visible) {
      const list = filteredVms(v, status);
      vms += list.length;
      if (status && list.length) {
        cpu += list.reduce((s, vm) => s + (Number(vm.cpu) || 0), 0);
        ram += list.reduce((s, vm) => s + (Number(vm.ram_gib) || 0), 0);
        storage += list.reduce((s, vm) => s + (Number(vm.hdd_gib) || 0), 0);
        storageAssigned += list.reduce(
          (s, vm) => s + (Number(vm.hdd_assigned_gib) || 0),
          0
        );
        for (const vm of list) {
          if (vm.hdd_used_from_api) {
            storageUsedAny = true;
            storageUsed += Number(vm.hdd_used_gib) || 0;
          }
        }
      } else {
        cpu += Number(v.cpu) || 0;
        ram += Number(v.ram_gib) || 0;
        storage += Number(v.hdd_gib) || 0;
        storageAssigned += Number(v.hdd_assigned_gib) || 0;
        if (v.hdd_used_from_api) {
          storageUsedAny = true;
          storageUsed += Number(v.hdd_used_gib) || 0;
        } else {
          for (const vm of v.vms || []) {
            if (vm.hdd_used_from_api) {
              storageUsedAny = true;
              storageUsed += Number(vm.hdd_used_gib) || 0;
            }
          }
        }
      }
    }
    return {
      cpu,
      ram,
      storage,
      storageAssigned,
      storageUsed,
      storageUsedAny,
      ips,
      edges: ips,
      vms,
      vapps: visible.length,
    };
  }

  function updateCalc() {
    if (!calcPanel || calcPanel.hidden) return;
    const { cpu, ram, storage, storageAssigned, ips, edges, vms, vapps } =
      visibleResources();
    const pCpu = parsePrice(priceCpu);
    const pRam = parsePrice(priceRam);
    const pHdd = parsePrice(priceHdd);
    const pIp = parsePrice(priceIp);
    const pEdge = parsePrice(priceEdge);
    const edgeOn = Boolean(includeEdge?.checked);
    const edgeQty = edgeOn ? edges : 0;
    const hddBillable =
      hddAssignedOn && storageAssigned > 0 ? storageAssigned : storage;
    const costCpu = cpu * pCpu;
    const costRam = ram * pRam;
    const costHdd = hddBillable * pHdd;
    const costIp = ips * pIp;
    const costEdge = edgeQty * pEdge;
    const total = costCpu + costRam + costHdd + costIp + costEdge;

    $("#calc-cpu").textContent = fmtChf(costCpu);
    $("#calc-ram").textContent = fmtChf(costRam);
    $("#calc-hdd").textContent = fmtChf(costHdd);
    $("#calc-ip").textContent = fmtChf(costIp);
    $("#calc-edge").textContent = fmtChf(costEdge);
    $("#calc-total").textContent = fmtChf(total);
    $("#calc-cpu-qty").textContent = `${cpu} vCPU`;
    $("#calc-ram-qty").textContent = `${Math.round(ram * 10) / 10} GiB`;
    $("#calc-hdd-qty").textContent = `${Math.round(hddBillable * 10) / 10} GiB zug.`;
    $("#calc-ip-qty").textContent = `${ips} × VDC`;
    $("#calc-edge-qty").textContent = edgeOn ? `${edgeQty} × VDC` : "nicht gewählt";

    const bits = [];
    bits.push(vdcFilter.value ? `VDC: ${vdcFilter.value}` : "alle VDCs");
    if (statusFilter.value === "POWERED_ON") bits.push("nur ON");
    else if (statusFilter.value === "POWERED_OFF") bits.push("nur OFF");
    else if (statusFilter.value) bits.push(statusFilter.value);
    bits.push(`${vapps} vApps · ${vms} VMs`);
    $("#calc-scope").textContent = bits.join(" · ");
  }

  function applyInventory(data, at) {
    inventory = data || [];
    inventoryAt = at || "";
    fillVdcFilter();
    updateExportLinks();
    render();
    showApp();
  }

  function setHddAssignedUi(on) {
    hddAssignedOn = Boolean(on);
    document.body.classList.toggle("hdd-assigned-on", hddAssignedOn);
    if (btnHddAssigned) {
      btnHddAssigned.classList.toggle("active-toggle", hddAssignedOn);
      btnHddAssigned.setAttribute("aria-pressed", hddAssignedOn ? "true" : "false");
      btnHddAssigned.title = hddAssignedOn
        ? "HDD konfig. ist aktiv – erneut klicken zum Ausschalten"
        : "HDD konfig. nachladen und anzeigen";
    }
    const wrap = $("#stat-hdd-assigned-wrap");
    if (wrap) wrap.hidden = !hddAssignedOn;
    document.querySelectorAll(".col-hdd-assigned").forEach((el) => {
      el.hidden = !hddAssignedOn;
    });
    try {
      localStorage.setItem(HDD_ASSIGNED_KEY, hddAssignedOn ? "1" : "0");
    } catch {
      // ignore
    }
  }

  function loadHddAssignedPref() {
    try {
      return localStorage.getItem(HDD_ASSIGNED_KEY) === "1";
    } catch {
      return false;
    }
  }

  function inventoryHasDiskDetails(data) {
    // Nur echte Disk-API-Daten zählen – vApp-hdd_assigned_gib ist sonst oft ein Fallback
    return (data || []).some((v) =>
      (v.vms || []).some(
        (vm) =>
          vm.hdd_assigned_from_api === true &&
          Array.isArray(vm.hdd_disks) &&
          vm.hdd_disks.length > 0
      )
    );
  }

  function updateStats() {
    const { cpu, ram, storage, storageAssigned, storageUsed, storageUsedAny, vms, vapps } =
      visibleResources();
    $("#stat-vapps").textContent = String(vapps);
    $("#stat-vms").textContent = String(vms);
    $("#stat-cpu").textContent = `${cpu} vCPU`;
    $("#stat-ram").textContent = fmtGib(ram);
    if (hddAssignedOn) {
      $("#stat-hdd-assigned").textContent = fmtStorageTotal(storageAssigned);
    }
    const usedEl = $("#stat-storage-used");
    if (usedEl) {
      usedEl.textContent = storageUsedAny ? fmtStorageTotal(storageUsed) : "—";
      usedEl.title = storageUsedAny
        ? "VCD-Metrik disk.used.latest (Storage-Belegung)"
        : "Keine HDD-in-use-Metriken (HDD konfig. laden oder Hoster ohne Metriken)";
    }
    $("#stat-storage").textContent = fmtStorageTotal(storage);
    $("#stat-at").textContent = inventoryAt || "—";
    updateCalc();
  }

  function fillVdcFilter() {
    const vdcs = [...new Set(inventory.map((v) => v.vdc).filter(Boolean))].sort();
    const current = vdcFilter.value;
    vdcFilter.innerHTML =
      `<option value="">Alle VDCs</option>` +
      vdcs.map((v) => `<option value="${escapeHtml(v)}">${escapeHtml(v)}</option>`).join("");
    if (vdcs.includes(current)) vdcFilter.value = current;
  }

  function matches(vapp, q, status, vdc) {
    if (vdc && vapp.vdc !== vdc) return false;
    if (status) {
      const vappOk = vapp.status === status;
      const hasVm = (vapp.vms || []).some((vm) => vm.status === status);
      if (!vappOk && !hasVm) return false;
    }
    if (!q) return true;
    const hay = [
      vapp.name,
      vapp.vdc,
      vapp.status,
      ...(vapp.vms || []).flatMap((vm) => [vm.name, vm.os, vm.network]),
    ]
      .join(" ")
      .toLowerCase();
    return hay.includes(q);
  }

  function filteredVms(vapp, status) {
    const vms = vapp.vms || [];
    if (!status) return vms;
    return vms.filter((vm) => vm.status === status);
  }

  function fmtUsedCell(vmOrVapp) {
    if (vmOrVapp && vmOrVapp.hdd_used_from_api) {
      return escapeHtml(fmtGib(vmOrVapp.hdd_used_gib));
    }
    return `<span class="muted" title="Metrik nicht verfügbar">—</span>`;
  }

  function render() {
    const q = (searchInput.value || "").trim().toLowerCase();
    const status = statusFilter.value;
    const vdc = vdcFilter.value;
    const rows = inventory.filter((v) => matches(v, q, status, vdc));
    updateStats();

    if (!rows.length) {
      tbody.innerHTML = "";
      emptyState.hidden = false;
      return;
    }
    emptyState.hidden = true;

    tbody.innerHTML = rows
      .map((vapp, idx) => {
        const key = vapp.href || vapp.name || String(idx);
        const open = openRows.has(key);
        const vmRows = filteredVms(vapp, status)
          .map(
            (vm) => `
            <tr>
              <td title="${escapeHtml(vm.name)}">${escapeHtml(shortName(vm.name))}</td>
              <td class="status-cell">${statusBadge(vm.status)}</td>
              <td class="num">${escapeHtml(vm.cpu)}</td>
              <td class="num">${escapeHtml(fmtGib(vm.ram_gib))}</td>
              <td class="num hdd col-hdd-assigned" ${hddAssignedOn ? "" : "hidden"}>
                ${
                  hddAssignedOn
                    ? fmtDiskCell(vm.hdd_assigned_gib, vm.hdd_disks, vm.hdd_assigned_from_api)
                    : ""
                }
              </td>
              <td class="num hdd" title="Storage in use (disk.used)">${fmtUsedCell(vm)}</td>
              <td class="netz">${escapeHtml(vm.network)}</td>
              <td class="muted">${escapeHtml(vm.os)}</td>
            </tr>`
          )
          .join("");

        return `
          <tr class="vapp-row ${open ? "open" : ""}" data-key="${escapeHtml(key)}">
            <td><span class="toggle">${open ? "−" : "+"}</span></td>
            <td><strong class="vapp-name" title="${escapeHtml(vapp.name)}">${escapeHtml(shortName(vapp.name))}</strong></td>
            <td class="status-cell">${statusBadge(vapp.status)}</td>
            <td>${escapeHtml(vapp.vdc)}</td>
            <td class="num">${escapeHtml(vapp.cpu)}</td>
            <td class="num">${escapeHtml(fmtGib(vapp.ram_gib))}</td>
            <td class="num col-hdd-assigned" ${hddAssignedOn ? "" : "hidden"}>
              ${
                hddAssignedOn
                  ? fmtDiskCell(
                      vapp.hdd_assigned_gib,
                      vapp.hdd_disks,
                      vapp.hdd_assigned_from_api
                    )
                  : ""
              }
            </td>
            <td class="num" title="Storage in use (disk.used)">${fmtUsedCell(vapp)}</td>
            <td class="num">${escapeHtml(vapp.vm_count)}</td>
          </tr>
          <tr class="vm-details" ${open ? "" : "hidden"}>
            <td colspan="${hddAssignedOn ? 9 : 8}">
              <table class="vm-table">
                <thead>
                  <tr>
                    <th>VM</th><th>Status</th><th class="num">CPU</th><th class="num">RAM</th>${
                      hddAssignedOn
                        ? `<th class="num hdd col-hdd-assigned">HDD konfig.</th>`
                        : ""
                    }<th class="num hdd">HDD in use</th><th class="netz">Netz</th><th>OS</th>
                  </tr>
                </thead>
                <tbody>
                  ${
                    vmRows ||
                    `<tr><td colspan="${hddAssignedOn ? 8 : 7}" class="muted">Keine VMs in dieser vApp.</td></tr>`
                  }
                </tbody>
              </table>
            </td>
          </tr>`;
      })
      .join("");
  }

  async function api(url, options = {}) {
    const ctrl = new AbortController();
    const timer = setTimeout(() => ctrl.abort(), FETCH_TIMEOUT_MS);
    try {
      const res = await fetch(url, {
        credentials: "same-origin",
        headers: { "Content-Type": "application/json", ...(options.headers || {}) },
        signal: ctrl.signal,
        ...options,
      });
      let data = null;
      const ct = res.headers.get("content-type") || "";
      if (ct.includes("application/json")) {
        data = await res.json();
      }
      if (!res.ok) {
        const detail = data?.detail || res.statusText || "Fehler";
        throw new Error(typeof detail === "string" ? detail : JSON.stringify(detail));
      }
      return data;
    } catch (err) {
      if (err?.name === "AbortError") {
        throw new Error("Zeitüberschreitung – bitte nochmals anmelden.");
      }
      throw err;
    } finally {
      clearTimeout(timer);
    }
  }

  function showApp() {
    loginPanel.hidden = true;
    appPanel.hidden = false;
    topActions.hidden = false;
  }

  function showLogin() {
    loginPanel.hidden = false;
    appPanel.hidden = true;
    topActions.hidden = true;
    if (calcPanel) {
      calcPanel.hidden = true;
      btnCalc?.classList.remove("active-calc");
      btnCalc?.setAttribute("aria-expanded", "false");
    }
    inventory = [];
    hideLoading();
  }

  async function loadInventory(refresh = false) {
    const withHdd = hddAssignedOn;
    showLoading(
      refresh
        ? withHdd
          ? "Aktualisiere (inkl. HDD konfig.)…"
          : "Aktualisiere…"
        : "Lade…"
    );
    try {
      const data = await api(
        `/api/inventory?refresh=${refresh ? "true" : "false"}&include_hdd_assigned=${
          withHdd ? "true" : "false"
        }`
      );
      applyInventory(data.inventory || [], data.inventory_at);
    } finally {
      hideLoading();
    }
  }

  $("#login-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    const err = $("#login-error");
    err.hidden = true;
    const fd = new FormData(e.target);
    const payload = {
      base_url: fd.get("base_url"),
      username: fd.get("username"),
      org: fd.get("org"),
      password: fd.get("password"),
      include_hdd_assigned: hddAssignedOn,
    };
    showLoading(
      hddAssignedOn ? "Anmelden & HDD konfig. laden…" : "Anmelden…"
    );
    try {
      const result = await api("/api/login", {
        method: "POST",
        body: JSON.stringify(payload),
      });
      showToast(`Geladen: ${result.vapps} vApps · ${result.vms} VMs`);
      applyInventory(result.inventory || [], result.inventory_at);
    } catch (ex) {
      err.textContent = ex.message || String(ex);
      err.hidden = false;
      showLogin();
    } finally {
      hideLoading();
    }
  });

  $("#btn-logout").addEventListener("click", async () => {
    try {
      await api("/api/logout", { method: "POST", body: "{}" });
    } catch {
      // ignore
    }
    showLogin();
    showToast("Abgemeldet");
  });

  $("#btn-refresh").addEventListener("click", async () => {
    try {
      await loadInventory(true);
      showToast("Aktualisiert");
    } catch (ex) {
      showToast(ex.message || String(ex));
      showLogin();
    }
  });

  btnHddAssigned?.addEventListener("click", async () => {
    const next = !hddAssignedOn;
    setHddAssignedUi(next);
    render();
    updateCalc();
    if (next) {
      // Immer neu laden, wenn noch keine Einzel-Disks da sind
      if (!inventoryHasDiskDetails(inventory)) {
        try {
          await loadInventory(true);
          showToast("HDD konfig. + in use geladen");
        } catch (ex) {
          showToast(ex.message || String(ex));
          setHddAssignedUi(false);
          render();
        }
      } else {
        showToast("HDD konfig. ein – Einzeldisks in der Spalte");
      }
    } else {
      showToast("HDD konfig. aus");
    }
  });

  tbody.addEventListener("click", (e) => {
    const row = e.target.closest("tr.vapp-row");
    if (!row) return;
    const key = row.dataset.key;
    if (openRows.has(key)) openRows.delete(key);
    else openRows.add(key);
    render();
  });

  searchInput.addEventListener("input", render);
  statusFilter.addEventListener("change", () => {
    updateExportLinks();
    render();
  });
  vdcFilter.addEventListener("change", () => {
    updateExportLinks();
    render();
  });

  btnCalc?.addEventListener("click", () => {
    const open = calcPanel.hidden;
    calcPanel.hidden = !open;
    btnCalc.classList.toggle("active-calc", open);
    btnCalc.setAttribute("aria-expanded", open ? "true" : "false");
    if (open) updateCalc();
  });

  for (const el of [priceCpu, priceRam, priceHdd, priceIp, priceEdge]) {
    el?.addEventListener("input", () => {
      savePrices();
      updateExportLinks();
      updateCalc();
    });
  }
  includeEdge?.addEventListener("change", () => {
    savePrices();
    updateExportLinks();
    updateCalc();
  });

  loadPrices();
  setHddAssignedUi(loadHddAssignedPref());

  // Kein Auto-Load mehr – nur Login-Formular. Verhindert hängenden Spinner.
  hideLoading();
  showLogin();
})();
