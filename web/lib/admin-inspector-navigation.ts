export interface InspectorLocationOptions {
  selectionParam: string;
  panelParam: string;
  panels: readonly string[];
  defaultPanel: string;
  defaultSelection?: string | null;
}

export function inspectorLocation(href: string, options: InspectorLocationOptions) {
  const params = new URL(href).searchParams;
  const raw = params.get(options.selectionParam);
  const selection = raw && raw.length <= 512 && !/[\u0000-\u001f\u007f]/.test(raw)
    ? raw : options.defaultSelection ?? null;
  const panel = params.get(options.panelParam) ?? "";
  return { selection, panel: options.panels.includes(panel) ? panel : options.defaultPanel };
}

export function inspectorLocationUrl(
  href: string, options: InspectorLocationOptions, selection: string | null, panel: string,
): string {
  const url = new URL(href);
  if (selection) url.searchParams.set(options.selectionParam, selection);
  else url.searchParams.delete(options.selectionParam);
  if (panel !== options.defaultPanel && options.panels.includes(panel)) url.searchParams.set(options.panelParam, panel);
  else url.searchParams.delete(options.panelParam);
  return `${url.pathname}${url.search}${url.hash}`;
}
