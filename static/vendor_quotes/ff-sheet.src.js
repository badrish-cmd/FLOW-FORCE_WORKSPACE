// Source of static/vendor_quotes/ff-sheet.js (+ ff-sheet.css). Rebuild with:
//   npm i @univerjs/presets@1.4.0 @univerjs/preset-sheets-core@1.4.0 @univerjs/preset-sheets-filter@1.4.0 \
//         @univerjs/preset-sheets-sort@1.4.0 @univerjs/preset-sheets-find-replace@1.4.0 \
//         @univerjs/preset-sheets-conditional-formatting@1.4.0 @univerjs/preset-sheets-data-validation@1.4.0 esbuild
//   npx esbuild ff-sheet.src.js --bundle --minify --format=iife --outfile=ff-sheet.js \
//         --define:process.env.NODE_ENV='"production"' --loader:.woff=dataurl --loader:.woff2=dataurl
import { createUniver, LocaleType, mergeLocales } from '@univerjs/presets';
import { UniverSheetsCorePreset } from '@univerjs/preset-sheets-core';
import { UniverSheetsFilterPreset } from '@univerjs/preset-sheets-filter';
import { UniverSheetsSortPreset } from '@univerjs/preset-sheets-sort';
import { UniverSheetsFindReplacePreset } from '@univerjs/preset-sheets-find-replace';
import { UniverSheetsConditionalFormattingPreset } from '@univerjs/preset-sheets-conditional-formatting';
import { UniverSheetsDataValidationPreset } from '@univerjs/preset-sheets-data-validation';
import coreEnUS from '@univerjs/preset-sheets-core/locales/en-US';
import filterEnUS from '@univerjs/preset-sheets-filter/locales/en-US';
import sortEnUS from '@univerjs/preset-sheets-sort/locales/en-US';
import findReplaceEnUS from '@univerjs/preset-sheets-find-replace/locales/en-US';
import cfEnUS from '@univerjs/preset-sheets-conditional-formatting/locales/en-US';
import dvEnUS from '@univerjs/preset-sheets-data-validation/locales/en-US';
import '@univerjs/preset-sheets-core/lib/index.css';
import '@univerjs/preset-sheets-filter/lib/index.css';
import '@univerjs/preset-sheets-sort/lib/index.css';
import '@univerjs/preset-sheets-find-replace/lib/index.css';
import '@univerjs/preset-sheets-conditional-formatting/lib/index.css';
import '@univerjs/preset-sheets-data-validation/lib/index.css';

// Mount an Excel-like workbook into `container` from a Univer workbook snapshot.
// Excel features: formulas + AutoSum/function menu, fill handle, insert/delete rows & columns,
// copy/paste, undo/redo, number formats, filter, sort, find & replace (Ctrl+F / Ctrl+H),
// conditional formatting and data validation.
// Returns { getSnapshot(), dispose(), api, univer }.
function mount(container, snapshot, opts) {
  opts = opts || {};
  const { univer, univerAPI } = createUniver({
    locale: LocaleType.EN_US,
    locales: {
      [LocaleType.EN_US]: mergeLocales(coreEnUS, filterEnUS, sortEnUS, findReplaceEnUS, cfEnUS, dvEnUS),
    },
    presets: [
      UniverSheetsCorePreset({ container, header: true, toolbar: true, footer: { sheetBar: true } }),
      UniverSheetsFilterPreset(),
      UniverSheetsSortPreset(),
      UniverSheetsFindReplacePreset(),
      UniverSheetsConditionalFormattingPreset(),
      UniverSheetsDataValidationPreset(),
    ],
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
