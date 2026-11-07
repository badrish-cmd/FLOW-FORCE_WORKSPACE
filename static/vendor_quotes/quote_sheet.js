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
            summary: window.VQ.summary,
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
            },
            fmt(n) { return Math.round(n || 0).toLocaleString("en-US"); },
            openDecision(kind) { this.decision = kind; },
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
                        body: JSON.stringify({ snapshot: snapshot }),
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
