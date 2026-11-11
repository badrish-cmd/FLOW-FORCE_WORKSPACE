/* Vendor quote page: mounts the Excel-like sheet and saves it back to the server. */
(function () {
    const data = JSON.parse(document.getElementById("sheet-data").textContent || "{}");
    let sheet = null;

    function mountSheet(onChange) {
        const host = document.getElementById("sheet-host");
        if (!host || !window.FFSheet) return;
        sheet = window.FFSheet.mount(host, data, { readOnly: !window.VQ.editable, onChange: onChange });
        window.vqSheet = sheet;
    }

    window.quotePage = function () {
        return {
            showPdf: false,
            dirty: false,
            saving: false,
            saveLabel: window.VQ.editable ? "All changes saved" : "",
            decision: null,
            modal: null,
            summary: window.VQ.summary,
            aiRunning: window.VQ.aiRunning || false,
            aiPollTimer: null,
            details: window.VQ.details || {},
            markDirty() {
                if (window.VQ.editable) {
                    this.dirty = true;
                    this.saveLabel = "Unsaved changes";
                }
            },
            onSalespersonSelect(e) {
                const sel = e.target;
                const opt = sel.options[sel.selectedIndex];
                this.details.salesperson_id = sel.value;
                if (opt && opt.dataset.name) {
                    this.details.salesperson_name = opt.dataset.name;
                }
                this.markDirty();
            },
            updateLineDescription(lineNo, val) {
                if (!this.details.line_details) this.details.line_details = {};
                if (!this.details.line_details[lineNo]) this.details.line_details[lineNo] = {};
                this.details.line_details[lineNo].quote_description = val;
                this.markDirty();
            },
            updateLineCommodityCode(lineNo, val) {
                if (!this.details.line_details) this.details.line_details = {};
                if (!this.details.line_details[lineNo]) this.details.line_details[lineNo] = {};
                this.details.line_details[lineNo].commodity_code = val;
                this.markDirty();
            },
            get calculatedExpiryDate() {
                const days = parseInt(this.details.validity_days) || 30;
                const d = new Date();
                d.setDate(d.getDate() + days);
                return d.toLocaleDateString("en-US", { year: "numeric", month: "short", day: "numeric" });
            },
            init() {
                let ready = false;
                setTimeout(() => { ready = true; }, 2000);   // ignore recalculation events while loading
                mountSheet(() => { if (ready && !this.saving) { this.dirty = true; this.saveLabel = "Unsaved changes"; } });
                if (window.VQ.editable) {
                    window.addEventListener("keydown", (e) => {
                        if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === "s") { e.preventDefault(); this.save(); }
                    });
                    window.addEventListener("beforeunload", (e) => { if (this.dirty) { e.preventDefault(); e.returnValue = ""; } });
                    setInterval(() => { if (this.dirty && !this.saving) this.save(); }, 60000);
                }
                if (this.aiRunning) {
                    this.pollAiStatus();
                }
            },
            pollAiStatus() {
                if (this.aiPollTimer) clearInterval(this.aiPollTimer);
                this.aiPollTimer = setInterval(async () => {
                    try {
                        const resp = await fetch(window.VQ.aiStatusUrl);
                        if (!resp.ok) return;
                        const data = await resp.json();
                        if (!data.ok) return;
                        if (data.status === "DONE" || data.status === "FAILED") {
                            clearInterval(this.aiPollTimer);
                            this.aiPollTimer = null;
                            window.location.reload();
                        }
                    } catch (e) {
                        console.error("AI status polling error:", e);
                    }
                }, 3000);
            },
            async startAiRead() {
                if (this.aiRunning) return;
                if (this.summary && this.summary.lines > 0) {
                    if (!confirm("Replace existing lines?")) {
                        return;
                    }
                }
                this.aiRunning = true;
                try {
                    const resp = await fetch(window.VQ.aiReadUrl, {
                        method: "POST",
                        headers: {
                            "Content-Type": "application/json",
                            "X-CSRFToken": window.VQ.csrf,
                        },
                    });
                    const out = await resp.json();
                    if (!resp.ok || !out.ok) {
                        this.aiRunning = false;
                        alert(out.error || "Failed to start AI reading.");
                        return;
                    }
                    this.pollAiStatus();
                } catch (err) {
                    this.aiRunning = false;
                    alert("Network error: " + err.message);
                }
            },
            fmt(n) { return Math.round(n || 0).toLocaleString("en-US"); },
            openDecision(kind) { this.decision = kind; },
            openModal(name) { this.modal = name; },
            closeModal() { this.modal = null; },
            async save() {
                if (!window.VQ.editable || !sheet) return true;
                this.saving = true;
                this.saveLabel = "Saving…";
                try {
                    // Give the formula engine a moment to finish recalculating before reading values.
                    await new Promise((r) => setTimeout(r, 300));
                    const snapshot = sheet.getSnapshot();
                    const resp = await fetch(window.VQ.saveUrl, {
                        method: "POST",
                        headers: { "Content-Type": "application/json", "X-CSRFToken": window.VQ.csrf },
                        body: JSON.stringify({
                            snapshot: snapshot,
                            quotation_details: this.details,
                        }),
                    });
                    const out = await resp.json();
                    if (!resp.ok || !out.ok) throw new Error(out.error || "Save failed");
                    this.dirty = false;
                    this.summary = { lines: out.lines, total: out.total, problems: out.problems || [] };
                    this.saveLabel = "Saved " + new Date().toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
                    return true;
                } catch (err) {
                    this.saveLabel = "Not saved: " + err.message;
                    alert("Couldn't save the sheet: " + err.message);
                    return false;
                } finally {
                    this.saving = false;
                }
            },
            async saveThenSubmit(form) {
                const ok = await this.save();
                if (ok) { this.dirty = false; form.submit(); }
            },
        };
    };
})();
