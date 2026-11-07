import { createUniver, LocaleType, mergeLocales } from '@univerjs/presets';
import { UniverSheetsCorePreset } from '@univerjs/preset-sheets-core';
import enUS from '@univerjs/preset-sheets-core/locales/en-US';
import '@univerjs/preset-sheets-core/lib/index.css';

// Mount an Excel-like workbook into `container` from a Univer workbook snapshot.
// Returns { getSnapshot(), getComputedValues(), dispose(), api }.
function mount(container, snapshot, opts) {
  opts = opts || {};
  const { univer, univerAPI } = createUniver({
    locale: LocaleType.EN_US,
    locales: { [LocaleType.EN_US]: mergeLocales(enUS) },
    presets: [UniverSheetsCorePreset({ container, header: true, toolbar: true, footer: { sheetBar: true } })],
  });
  univerAPI.createWorkbook(snapshot || {});
  const wb = univerAPI.getActiveWorkbook();
  if (opts.readOnly && wb && wb.setEditable) {
    // The permission must be applied after the workbook has rendered, otherwise it is reset.
    const lock = () => { try { univerAPI.getActiveWorkbook().setEditable(false); } catch (e) {} };
    try {
      univerAPI.addEvent(univerAPI.Event.LifeCycleChanged, (ev) => { if (ev && ev.stage >= 2) lock(); });
    } catch (e) {}
    [0, 300, 1000, 2500].forEach((ms) => setTimeout(lock, ms));
  }
  if (opts.onChange) {
    try {
      univerAPI.addEvent(univerAPI.Event.SheetValueChanged, (ev) => opts.onChange(ev));
    } catch (e) {}
  }
  return {
    api: univerAPI,
    univer,
    getSnapshot() { return univerAPI.getActiveWorkbook().save(); },
    dispose() { univer.dispose(); },
  };
}
window.FFSheet = { mount };
