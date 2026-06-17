"""EASI — Ecosystem Assessment Screening Index (Shiny for Python, Core).

A StreamStats-style workflow: zoom in until NHD stream vectors appear, click a
stream to snap a point, delineate the watershed + upstream reach, review the
basin, configure which of the 20 EASI functions to compute (and the data source
where alternatives exist), then open a polished report popup (STAF screening
summary + basin characteristics + cross-section). Field/low-confidence metrics
are overrideable; export PDF / CSV / GeoJSON.
"""
from __future__ import annotations

import html
import os
import tempfile
from pathlib import Path

# HyRiver cache -> writable temp dir (Connect Cloud FS is ephemeral). Set before
# any HyRiver import so the clients pick it up.
os.environ.setdefault("HYRIVER_CACHE_NAME",
                      os.path.join(tempfile.gettempdir(), "easi_hyriver.sqlite"))
os.environ.setdefault("HYRIVER_CACHE_EXPIRE", str(7 * 24 * 3600))

import anyio  # noqa: E402
from shiny import App, reactive, render, ui  # noqa: E402

from easi import assessment, config, delineation, pipeline, report, scoring  # noqa: E402
from easi.datasources import flowlines  # noqa: E402
from easi.datasources.geocode import geocode_address  # noqa: E402
from easi.pipeline import DEFAULT_REACH_FT  # noqa: E402

FT_PER_M = 3.28083989501312

try:
    from ipyleaflet import GeoJSON, LayersControl, Map, Marker, TileLayer
    from ipywidgets import Layout
    from shinywidgets import output_widget, reactive_read, render_widget
    _HAS_MAP = True
except Exception:  # pragma: no cover
    _HAS_MAP = False

WATERSHED_STYLE = {"color": "#caa700", "weight": 1, "fillColor": "#fdf24a", "fillOpacity": 0.40}
REACH_STYLE = {"color": "#d6453d", "weight": 4}
FLOWLINE_STYLE = {"color": "#1f6feb", "weight": 2, "opacity": 0.9}
RATING_COLOR = {"Good": "#c8d9f2", "Fair": "#f5e7a6", "Poor": "#f5b5b5"}
_DISC_ORDER = ["Hydrology", "Hydraulics", "Geomorphology", "Physicochemistry", "Biology"]

USGS_TOPO_URL = "https://basemap.nationalmap.gov/arcgis/rest/services/USGSTopo/MapServer/tile/{z}/{y}/{x}"
USGS_IMAGERY_URL = "https://basemap.nationalmap.gov/arcgis/rest/services/USGSImageryTopo/MapServer/tile/{z}/{y}/{x}"
USGS_HYDRO_URL = "https://hydro.nationalmap.gov/arcgis/rest/services/USGSHydroCached/MapServer/tile/{z}/{y}/{x}"
USGS_ATTR = "USGS The National Map"
FLOW_ZOOM = 14          # NHD vectors appear at/above this zoom
SNAP_TOL_FT = 150.0     # click must land within this distance of a flowline

OUTCOME_META = [
    ("physical", "Physical", "Hydrology · Hydraulics · Geomorphology"),
    ("chemical", "Chemical", "Thermal · Nutrients · Impairment"),
    ("biological", "Biological", "Habitat · Community"),
]
STEP_IDENTIFY, STEP_BASIN, STEP_CONFIGURE, STEP_REPORT = "identify", "basin", "configure", "report"
STEP_LABELS = [(STEP_IDENTIFY, "Identify"), (STEP_BASIN, "Basin"),
               (STEP_CONFIGURE, "Configure"), (STEP_REPORT, "Report")]

_METRICS = config.metrics_by_id()
ALL_MIDS = list(_METRICS)
SRC_INDEX = {i: mid for i, mid in enumerate(ALL_MIDS) if mid in config.SOURCE_OPTIONS}
OVERRIDEABLE = [mid for mid, info in config.METRIC_REGISTRY.items() if info.get("overrideable")]
OVERRIDEABLE_SET = set(OVERRIDEABLE)


# --------------------------------------------------------------------------- #
# UI helpers
# --------------------------------------------------------------------------- #
def _ds_label(mid: str) -> str:
    ds = config.METRIC_REGISTRY.get(mid, {}).get("datasource", "")
    primary = ds.split("|")[0].replace("proxy:", "").replace("streamcat:", "StreamCat ")
    return primary.replace("+", " + ").replace("_", " ").strip() or "—"


def _short(text: str, n: int = 46) -> str:
    text = (text or "").strip()
    return text if len(text) <= n else text[: n - 1] + "…"


def _info(text: str = None, *, html_tip: str = None):
    """A small circled-'i'; the custom tooltip (www/tooltip.js) shows the tip.

    Pass ``text`` for a plain-text tooltip (``data-tip``) or ``html_tip`` for a rich
    HTML card (``data-tip-html``). The onclick guard lets the icon sit inside a
    checkbox ``<label>`` without a click on it toggling the checkbox.
    """
    attrs = {"onclick": "event.preventDefault();event.stopPropagation();"}
    if html_tip:
        attrs["data-tip-html"] = html_tip
    elif text and text.strip():
        attrs["data-tip"] = text.strip()
    else:
        return None
    return ui.span("i", attrs, class_="easi-info")


def _metric_tip_html(name, definition, calc, note, crit, default):
    """Build the report ⓘ tooltip card: definition, calculation, then scoring criteria.

    All dynamic values are HTML-escaped; the surrounding markup is app-controlled.
    """
    e = html.escape
    parts = [f'<div class="easi-tip-title">{e(name or "")}</div>']
    if definition:
        parts.append('<div class="easi-tip-sec"><span class="easi-tip-lbl">Definition</span>'
                     f'{e(definition)}</div>')
    if calc:
        sub = f'<div class="easi-tip-sub">{e(note)}</div>' if note else ""
        parts.append('<div class="easi-tip-sec"><span class="easi-tip-lbl">Calculation</span>'
                     f'{e(calc)}{sub}</div>')
    rows = []
    for band in ("Good", "Fair", "Poor"):
        c = crit.get(band)
        if c:
            rows.append(f'<div class="easi-tip-crit"><span class="easi-tip-dot {band.lower()}">'
                        f'</span><b>{band}</b>&nbsp;{e(c)}</div>')
    if rows:
        dflt = f'<span class="easi-tip-default">default: {e(default)}</span>' if default else ""
        parts.append('<div class="easi-tip-sec"><span class="easi-tip-lbl">Scoring</span>'
                     f'{dflt}{"".join(rows)}</div>')
    return "".join(parts)


app_ui = ui.page_fillable(
    ui.head_content(ui.tags.link(rel="stylesheet", href="styles.css"),
                    ui.tags.script(src="geocode-autocomplete.js", defer=""),
                    ui.tags.script(src="tooltip.js", defer=""),
                    ui.tags.script(src="report-edit.js", defer="")),
    ui.div(
        ui.div(
            ui.span("EASI", ui.tags.small("Ecosystem Assessment Screening Index"),
                    class_="easi-brand"),
            ui.div(
                ui.input_action_link("nav_new", "New analysis"),
                ui.input_action_link("nav_about", "About"),
                ui.input_action_link("nav_help", "Help"),
                class_="easi-nav",
            ),
            class_="easi-header",
        ),
        ui.div(
            output_widget("map", height="100%") if _HAS_MAP
            else ui.div("Map requires ipyleaflet + shinywidgets.", class_="text-muted p-3"),
            class_="easi-map-wrap",
        ),
        ui.div(ui.output_ui("leftpane"), class_="easi-leftpane"),
        ui.output_ui("readout"),
        ui.output_ui("flow_loading"),
        ui.output_ui("cursor_style"),
        class_="easi-shell",
    ),
    title="EASI — Automated Stream Screening",
    padding=0,
    fillable=True,
)


# --------------------------------------------------------------------------- #
# Report rendering helpers (shared by the modal output slots)
# --------------------------------------------------------------------------- #
def _chip(text, color):
    return ui.span(text, class_="easi-chip", style=f"background:{color};")


def _bar(label, value, color, vmax=1.0):
    pct = 0.0 if value is None else max(0.0, min(1.0, value / vmax)) * 100
    return ui.div(
        ui.div(label, style="width:210px;font-size:13px;"),
        ui.div(ui.div(class_="easi-bar-fill", style=f"width:{pct:.1f}%;background:{color};"),
               class_="easi-bar-track"),
        ui.div("—" if value is None else f"{value:.2f}",
               style="width:46px;text-align:right;font-size:13px;font-weight:600;"),
        class_="easi-bar-row",
    )


def _confidence_summary(rep):
    counts, na, ov = {}, 0, 0
    for r in rep["metricRows"]:
        if r["status"] == "override":
            ov += 1
        if r["rating"] in ("Good", "Fair", "Poor"):
            counts[r["confidence"]] = counts.get(r["confidence"], 0) + 1
        else:
            na += 1
    parts = [f"{k}: {counts[k]}" for k in ("H", "M", "M/L", "L") if counts.get(k)]
    txt = "Confidence — " + " · ".join(parts) + f" · n/a: {na}"
    if ov:
        txt += f" · {ov} override(s)"
    return txt


def _rate_select(mid, r):
    """Chip-styled native <select> that overrides an overrideable metric's rating.

    Looks like the colored rating chip; opening it shows Auto + Good/Fair/Poor with
    their thresholds. Plain HTML (no Shiny binding) — www/report-edit.js posts the
    choice via setInputValue, so it survives the table re-render.
    """
    eff = r.get("rating")                        # effective (override or generated)
    opts = []
    if eff not in ("Good", "Fair", "Poor"):      # no current rating -> non-pickable placeholder
        opts.append(ui.tags.option("—", value="", selected="selected", disabled="disabled"))
    for rt in ("Good", "Fair", "Poor"):
        opts.append(ui.tags.option(rt, value=rt, selected="selected") if rt == eff
                    else ui.tags.option(rt, value=rt))
    # criteria + computed value live in the adjacent ⓘ (see _metric_table), so the
    # chip stays narrow; just a short hint here.
    return ui.tags.select(*opts, {"class": f"easi-rate-sel rate-{eff or 'auto'}",
                                  "data-mid": mid, "title": "Click to override rating"})


def _metric_table(rows, notes=None):
    notes = notes or {}
    head = ui.tags.tr(
        *[ui.tags.th(h) for h in
          ["Function", "Metric", "Value", "Rating", "Index", "Fn", "Conf", "Source"]],
        ui.tags.th("Note", class_="easi-note-cell"))
    body = []
    order = {d: i for i, d in enumerate(_DISC_ORDER)}
    rows = sorted(rows, key=lambda r: (order.get(r["discipline"], 99), r["functionName"]))
    seen = []
    for r in rows:
        mid = r["metricId"]
        if r["discipline"] not in seen:
            seen.append(r["discipline"])
            body.append(ui.tags.tr(ui.tags.td(r["discipline"], colspan="9"), class_="easi-disc"))
        status = r.get("status")
        is_ovr = status == "override"          # manual dropdown override
        is_xs = status == "xs-derived"         # rating recomputed from an edited cross-section
        # Rating cell: override dropdown + an ⓘ whose hover shows the computed default
        # value and the Good/Fair/Poor criteria.
        crit = (_METRICS.get(mid, {}).get("criteria") or {})
        tip_html = _metric_tip_html(
            name=r.get("name"), definition=config.METRIC_DEFINITIONS.get(mid, ""),
            calc=r.get("source") or "", note=r.get("note") or "", crit=crit,
            default=r.get("generatedRating") or "n/a")
        rating_cell = ui.tags.td(ui.div(_rate_select(mid, r), _info(html_tip=tip_html),
                                        class_="easi-rate-cell"))
        if is_ovr:
            src_cell = ui.tags.td(ui.span("override", class_="easi-ovr-pill"))
        elif is_xs:
            src_cell = ui.tags.td(ui.span("cross-section", class_="easi-xs-pill"))
        else:
            src_cell = ui.tags.td(r["source"] or "", style="font-size:11px;color:#666;")
        note = notes.get(mid) or ""
        note_btn = ui.tags.button("✎", {
            "class": "easi-note-btn" + (" has-note" if note else ""), "data-mid": mid,
            "type": "button", "title": ("Edit note" if note else "Add note"),
            "aria-label": ("Edit note" if note else "Add note")})
        body.append(ui.tags.tr(
            ui.tags.td(r["functionName"]),
            ui.tags.td(r["name"]),
            ui.tags.td(r["valueText"]),
            rating_cell,
            ui.tags.td("" if r["index"] is None else f'{r["index"]:.2f}'),
            ui.tags.td("" if r["functionScore"] is None else str(r["functionScore"])),
            ui.tags.td(r["confidence"] or ""),
            src_cell,
            ui.tags.td(note_btn, class_="easi-note-cell"),
            {"data-mid": mid},
            class_=("easi-row-ovr" if (is_ovr or is_xs) else ""),
            style=("" if r["rating"] else "color:#aaa;"),
        ))
        body.append(ui.tags.tr(
            ui.tags.td(ui.tags.textarea(note, {
                "class": "easi-note-ta", "data-mid": mid, "rows": "2",
                "placeholder": "Add a note for this metric…"}), colspan="9"),
            {"data-mid": mid}, class_="easi-note-row"))
    return ui.tags.table(ui.tags.thead(head), ui.tags.tbody(*body), class_="easi-tbl")


def _outcome_table(outcomes):
    keys = ["physical", "chemical", "biological"]
    head = ui.tags.tr(ui.tags.th(""), *[ui.tags.th(k.capitalize()) for k in keys])

    def row(label, fn):
        return ui.tags.tr(ui.tags.th(label), *[ui.tags.td(fn(outcomes[k])) for k in keys])

    return ui.tags.table(
        ui.tags.thead(head),
        ui.tags.tbody(
            row("Direct functions", lambda o: str(o["direct"])),
            row("Indirect functions", lambda o: str(o["indirect"])),
            row("Weighted total", lambda o: f'{o["weighted"]:.2f}'),
            row("Max weighted", lambda o: f'{o["max"]:.2f}'),
            row("Sub-index", lambda o: f'{o["subIndex"]:.2f}'),
        ),
        class_="easi-tbl",
    )


def _outcome_cards(sc):
    sub, out = sc["subIndices"], sc["outcomes"]
    eci = sc["ecosystemConditionIndex"]
    cards = []
    for key, label, _desc in OUTCOME_META:
        o = out[key]
        cards.append(ui.div(
            ui.div(label, class_="outcome-card-title"),
            ui.div(f'{sub[key]:.2f}', class_="outcome-card-value"),
            ui.div(f'{o["direct"]} direct · {o["indirect"]} indirect functions',
                   class_="outcome-card-sub"),
            class_=f"outcome-card {key}",
        ))
    eci_card = ui.div(
        ui.div("Ecosystem Condition Index", class_="outcome-card-title"),
        ui.div(f'{eci or 0:.2f}', class_="outcome-card-value"),
        ui.div("Mean of the three outcome sub-indices", class_="outcome-card-sub"),
        class_="ecosystem-condition-card",
    )
    return ui.TagList(ui.div(*cards, class_="outcome-cards"), eci_card)


def _summary_header(d):
    def fact(label, val):
        return ui.span(ui.tags.b(f"{label}: "), str(val), class_="easi-fact")

    lat, lon = d.get("snapped_lat"), d.get("snapped_lon")
    snapped = f"{lat:.4f}, {lon:.4f}" if lat is not None and lon is not None else "—"
    return ui.div(
        ui.h3(d.get("gnis_name") or "(unnamed reach)"),
        ui.div(
            fact("COMID", d.get("comid")),
            fact("HUC12", d.get("huc12") or "—"),
            fact("Drainage", f'{d.get("drainage_area_sqkm")} km²'),
            fact("Watershed", f'{d.get("watershed_area_sqkm")} km²'),
            fact("Reach", f'{d.get("reach_length_ft")} ft upstream'),
            fact("Snapped", snapped),
            class_="easi-facts",
        ),
        class_="easi-summary-head",
    )


def _basin_block(rep):
    rows = (rep or {}).get("basin", {}).get("rows") or []
    if not rows:
        return None
    body = [ui.tags.tr(ui.tags.th(lbl), ui.tags.td(val)) for lbl, val in rows]
    return ui.TagList(
        ui.div("Basin characteristics", class_="easi-section-title"),
        ui.tags.table(ui.tags.tbody(*body), class_="easi-tbl", style="max-width:560px;"),
    )


def _xsection_section(rep):
    """Editable cross-section: reactive image (output_ui 'xsection') + the
    bankfull/floodplain height inputs, unit toggle, and reset. Heights default to
    the Bieger regional bankfull and the DEM top-of-bank, in feet."""
    xs = (rep or {}).get("crossSection") or {}
    if not xs.get("png_b64"):
        return None
    block = xs.get("geom") or {}
    thalweg = block.get("thalweg")
    if thalweg is None:  # no editable geometry — render the static image only
        return ui.div(ui.tags.img(src=f"data:image/png;base64,{xs['png_b64']}"),
                      ui.p(xs.get("caption") or "", class_="easi-xsection-cap"),
                      class_="easi-xsection")

    def ft(stage):
        return round((stage - thalweg) * FT_PER_M, 2) if stage is not None else None

    bf_def = ft(block.get("bankfull_stage"))
    fp_def = ft(block.get("floodplain_stage"))
    return ui.div(
        ui.output_ui("xsection"),
        ui.div(
            ui.div("Edit channel geometry — heights above the channel bottom. "
                   "ER, BHR, and the floodplain-engagement rating recompute.",
                   class_="easi-xs-help"),
            ui.input_radio_buttons("xs_unit", None, {"ft": "Feet", "m": "Meters"},
                                   selected="ft", inline=True),
            ui.div(
                ui.input_numeric("xs_bankfull", "Bankfull height", value=bf_def,
                                 min=0, step=0.1),
                ui.input_numeric("xs_floodplain", "Floodplain height", value=fp_def,
                                 min=0, step=0.1),
                class_="easi-xs-fields",
            ),
            ui.input_action_button("xs_reset", "Reset to Bieger default",
                                   class_="btn-sm btn-outline-secondary"),
            class_="easi-xs-edit",
        ),
        class_="easi-xsection-wrap",
    )


def _dl_buttons():
    return ui.div(
        ui.download_button("dl_pdf", "PDF", class_="btn-sm btn-outline-secondary"),
        ui.download_button("dl_csv", "CSV", class_="btn-sm btn-outline-secondary"),
        ui.download_button("dl_geojson", "GeoJSON", class_="btn-sm btn-outline-secondary"),
        ui.input_action_button("close_modal", "Close", class_="btn-sm btn-primary"),
        class_="easi-modal-footer",
    )


def _report_modal(base):
    """Static modal skeleton: override-independent chrome + dynamic output slots."""
    d, rep = base["delineation"], base.get("report")
    return ui.modal(
        _summary_header(d),
        ui.output_ui("m_scores"),
        _basin_block(rep),
        _xsection_section(rep),
        ui.div("Outcome rollup", class_="easi-section-title"),
        ui.output_ui("m_outcomes"),
        ui.div("Metrics", class_="easi-section-title"),
        ui.div("Adjust a rating inline in the Rating column; click ✎ on any row to add a note. "
               "Edits flow into the report and exports.", class_="easi-instr"),
        ui.output_ui("m_metrics"),
        ui.p("Generated from national datasets — a desktop screening estimate with "
             "per-metric confidence, not a field-validated assessment. Adjust field "
             "metrics inline to incorporate local evidence.", class_="easi-disclaimer"),
        _dl_buttons(),
        # ✕ lives in the modal header (anchors to .modal-content) so it sits at the
        # popup's top-right corner and stays put when the body scrolls.
        title=ui.TagList(
            "EASI Screening Report",
            ui.input_action_button("close_modal_x", "✕", class_="easi-modal-x"),
        ),
        size="xl",
        easy_close=True,
        footer=None,
    )


def _stepper(active):
    done = True
    items = []
    for key, label in STEP_LABELS:
        cls = "easi-step"
        if key == active:
            cls += " active"; done = False
        elif done:
            cls += " done"
        items.append(ui.input_action_link(f"go_{key}", label, class_=cls))
    return ui.div(*items, class_="easi-steps")


# --------------------------------------------------------------------------- #
# Server
# --------------------------------------------------------------------------- #
def server(input, output, session):
    current_step = reactive.value(STEP_IDENTIFY)
    snapped_point = reactive.value(None)   # (lat, lon, dist_ft) | None
    flow_geojson = reactive.value(None)    # current viewport flowlines FC | None
    delin = reactive.value(None)           # delineate_only result (+ ctx_inputs)
    base_result = reactive.value(None)     # merged delineation + report dict
    stage = reactive.value("")             # progress label
    _assess_prog = {"done": 0, "total": 0}  # shared metric-progress counter (poller reads)
    _overrides = reactive.value({})        # {metricId: "Good"/"Fair"/"Poor"} from the table
    _notes = reactive.value({})            # {metricId: note text} from the table
    _geom_owned = reactive.value(set())    # metricIds whose rating is currently derived from
    #                                        an edited cross-section (vs a manual dropdown pick)
    _geom_text = reactive.value({})        # {metricId: value text} for those edited rows
    modal_shown = reactive.value(False)
    view_bbox = reactive.value(None)       # rounded bbox at zoom >= FLOW_ZOOM | None
    last_view_change = reactive.value(0.0)
    fetched_bbox = reactive.value(None)
    step_clicks = reactive.value({k: 0 for k, _ in STEP_LABELS})  # stepper nav counters

    _layers: dict = {"flow": None, "marker": None, "ws": None, "reach": None}

    def _remove_layer(key):
        lyr = _layers.get(key)
        if lyr is not None:
            try:
                _MAP.remove(lyr)
            except Exception:  # noqa: BLE001
                pass
            _layers[key] = None

    def _add_layer(key, layer):
        _remove_layer(key)
        _MAP.add(layer)
        _layers[key] = layer

    # ---- persistent map (built once; mutated in place) ----
    if _HAS_MAP:
        def _on_map_interaction(**kwargs):
            if kwargs.get("type") == "click":
                c = kwargs.get("coordinates")
                if c:
                    clicked.set((float(c[0]), float(c[1])))  # (lat, lon)

        clicked = reactive.value(None)

        def _build_map():
            mp = Map(center=(39.5, -98.35), zoom=4, scroll_wheel_zoom=True,
                     layout=Layout(height="100%"))  # fill the wrapper (default is 400px)
            mp.clear_layers()  # drop default OSM
            # last-added base layer is the default -> USGS Topo on top (rivers + names)
            mp.add(TileLayer(url=USGS_IMAGERY_URL, name="USGS Imagery", base=True, attribution=USGS_ATTR))
            mp.add(TileLayer(url=USGS_TOPO_URL, name="USGS Topo", base=True, attribution=USGS_ATTR))
            mp.add(TileLayer(url=USGS_HYDRO_URL, name="NHD Hydrography", base=False,
                             opacity=0.85, attribution=USGS_ATTR))
            mp.add(LayersControl(position="topright"))
            mp.on_interaction(_on_map_interaction)
            return mp

        _MAP = _build_map()

        @render_widget
        def map():  # noqa: A001
            return _MAP  # same object every time -> pan/zoom persists

        @reactive.calc
        def _view():
            # Derive the fetch box from the map CENTER (always valid) + a zoom-scaled
            # radius — robust where viewport `bounds` are unreliable (e.g. a 0-width
            # container) and bounded in size for a fast fetch.
            return reactive_read(_MAP, "zoom"), reactive_read(_MAP, "center")

        # ---- vector flowlines on zoom (debounced trailing-edge) ----
        @reactive.effect
        def _track_view():
            import time
            z, c = _view()
            val = None
            if c and z is not None and z >= FLOW_ZOOM:
                lat, lon = float(c[0]), float(c[1])
                delta = min(0.08, 0.03 * (2 ** (15 - z)))  # half-box in degrees
                val = flowlines._round_bbox(lon - delta, lat - delta, lon + delta, lat + delta)
            view_bbox.set(val)
            last_view_change.set(time.monotonic())

        @reactive.extended_task
        async def flow_task(bbox: tuple) -> dict | None:
            return await anyio.to_thread.run_sync(lambda: flowlines.flowlines_in_bbox(*bbox))

        @reactive.effect
        def _settle_and_fetch():
            import time
            bbox = view_bbox()
            changed = last_view_change()
            if bbox is None:
                with reactive.isolate():
                    _remove_layer("flow"); flow_geojson.set(None); fetched_bbox.set(None)
                return
            elapsed = time.monotonic() - changed
            if elapsed < 0.5:                       # wait for panning to settle
                reactive.invalidate_later(0.5 - elapsed + 0.02)
                return
            with reactive.isolate():
                if fetched_bbox() == bbox:
                    return
                fetched_bbox.set(bbox)
            flow_task(bbox)

        @reactive.effect
        def _apply_flowlines():
            try:
                fc = flow_task.result()
            except Exception:
                return
            with reactive.isolate():
                if fc and fc.get("features"):
                    _add_layer("flow", GeoJSON(data=fc, style=FLOWLINE_STYLE, name="Stream lines"))
                    flow_geojson.set(fc)
                else:
                    _remove_layer("flow"); flow_geojson.set(None)

        # ---- click -> snap or reject (only during the identify step) ----
        @reactive.effect
        @reactive.event(clicked)
        def _handle_click():
            if current_step() != STEP_IDENTIFY:
                return
            lat, lon = clicked()
            fc = flow_geojson()
            hit = flowlines.nearest_point_on_lines(fc, lat, lon) if fc else None
            if hit and hit[2] <= SNAP_TOL_FT:
                _apply_snap(hit)                 # covered by the viewport vectors
            else:
                click_snap_task(lat, lon)        # fetch flowlines around the click + snap

        def _apply_snap(hit):
            slat, slon, dist, comid = hit
            _add_layer("marker", Marker(location=(slat, slon), draggable=False,
                                        title="Selected point"))
            snapped_point.set((slat, slon, dist, comid))
            ui.update_numeric("lat", value=round(slat, 5))
            ui.update_numeric("lon", value=round(slon, 5))

        @reactive.extended_task
        async def click_snap_task(lat: float, lon: float) -> dict:
            d = 0.012  # ~0.8 mi half-box around the click, so the snap uses the
            return {"hit": await anyio.to_thread.run_sync(  # line you actually clicked
                lambda: flowlines.nearest_point_on_lines(
                    flowlines.flowlines_in_bbox(lon - d, lat - d, lon + d, lat + d), lat, lon))}

        @reactive.effect
        def _apply_click_snap():
            try:
                res = click_snap_task.result()
            except Exception:
                return
            hit = res.get("hit")
            if hit and hit[2] <= SNAP_TOL_FT:
                _apply_snap(hit)
            else:
                ui.notification_show("You didn't click on a stream line — zoom in and click "
                                     "a blue stream line.", type="warning", duration=5)

    # ---- address geocode -> recenter the map so streams appear ----
    @reactive.effect
    @reactive.event(input.find_address)
    def _geocode():
        hit = geocode_address(input.address())
        if hit and _HAS_MAP:
            _MAP.center = (hit[0], hit[1])
            _MAP.zoom = 15
            ui.notification_show(f"Centered on {hit[0]:.4f}, {hit[1]:.4f}. Click a blue stream.",
                                 duration=4)
        elif not hit:
            ui.notification_show("Place not found — try a city, address, or stream name.",
                                 type="warning", duration=4)

    @reactive.effect
    @reactive.event(input.address_pick)
    def _geocode_pick():
        # A suggestion was chosen in the type-ahead dropdown (coords come from the
        # client-side Photon query); just recenter the map.
        if not _HAS_MAP:
            return
        pick = input.address_pick() or {}
        lat, lon = pick.get("lat"), pick.get("lon")
        if lat is None or lon is None:
            return
        _MAP.center = (float(lat), float(lon))
        _MAP.zoom = 15
        where = pick.get("label") or f"{float(lat):.4f}, {float(lon):.4f}"
        ui.notification_show(f"Centered on {where}. Click a blue stream.", duration=4)

    # ---- enable "Delineate" only once a point is picked on the map ----
    @reactive.effect
    def _toggle_delineate():
        ui.update_action_button("delineate", disabled=(snapped_point() is None))

    # ---- staged analysis tasks ----
    @reactive.extended_task
    async def delineate_task(lat: float, lon: float, reach_ft: float,
                             comid: "int | None" = None) -> dict:
        return await pipeline.delineate_only(lat, lon, reach_ft, comid=comid)

    @reactive.extended_task
    async def assess_task(ctx_inputs: dict, metric_ids: list, sources: dict,
                          progress: dict) -> dict:
        return await pipeline.assess_only(ctx_inputs, metric_ids=metric_ids,
                                          sources=sources, progress=progress)

    @reactive.effect
    @reactive.event(input.delineate)
    def _start_delineate():
        pt = snapped_point()
        try:
            lat = pt[0] if pt else float(input.lat())
            lon = pt[1] if pt else float(input.lon())
        except Exception:
            ui.notification_show("Set a point first.", type="warning", duration=3)
            return
        comid = pt[3] if pt else None
        stage.set("Delineating basin & reach…")
        ui.notification_show("Delineating basin & reach… please wait", id="stage",
                             type="message", duration=None)
        delineate_task(lat, lon, float(input.reach_ft()), comid)

    @reactive.effect
    def _delineate_done():
        status = delineate_task.status()
        if status in ("initial", "running"):
            return
        ui.notification_remove("stage"); stage.set("")
        if status == "error":
            ui.notification_show("Delineation failed — try another point or zoom in further.",
                                 type="error", duration=8)
            return  # keep the marker + stay on Identify so the user can retry
        try:
            res = delineate_task.result()
        except Exception:
            ui.notification_show("Delineation failed.", type="error", duration=8)
            return
        if res.get("status") != "ok":
            ui.notification_show(res.get("message", "Delineation error"), type="error", duration=8)
            return
        # Draw overlays defensively — a very large basin is display-simplified so it
        # renders without breaking the map (full geometry stays in `res` for area/export).
        try:
            if res.get("watershed_geojson"):
                _add_layer("ws", GeoJSON(data=delineation.display_simplify(res["watershed_geojson"]),
                                         style=WATERSHED_STYLE, name="Watershed"))
            if res.get("reach_geojson"):
                _add_layer("reach", GeoJSON(data=res["reach_geojson"], style=REACH_STYLE,
                                            name="Assessment reach"))
            d = res.get("delineation") or {}
            if _HAS_MAP:
                bounds = delineation.geojson_bounds(res.get("watershed_geojson"),
                                                    res.get("reach_geojson"))
                if bounds:
                    _MAP.fit_bounds(bounds)            # zoom to the full basin extent
                elif d.get("snapped_lat") is not None:
                    _MAP.center = (d["snapped_lat"], d["snapped_lon"])
        except Exception as exc:  # noqa: BLE001
            ui.notification_show(f"Could not draw the basin on the map: {exc}",
                                 type="error", duration=8)
            return  # keep the marker; don't advance half-rendered
        delin.set(res)
        current_step.set(STEP_BASIN)

    @reactive.effect
    @reactive.event(input.to_report)
    def _start_assess():
        d = delin()
        if not d:
            return
        modal_shown.set(False)
        n = len(selected_metric_ids())
        _assess_prog["done"], _assess_prog["total"] = 0, n
        stage.set(f"Computing metrics… 0/{n}")
        ui.notification_show(f"Computing metrics… 0/{n} — please wait", id="stage",
                             type="message", duration=None)
        assess_task(d["ctx_inputs"], selected_metric_ids(), source_choices(), _assess_prog)

    @reactive.effect
    def _assess_progress_poll():
        # While metrics compute, poll the shared counter ~3x/sec and update the
        # left-pane busy label + toast with a live "X/N" count.
        if assess_task.status() != "running":
            return  # stops rescheduling once the task settles
        reactive.invalidate_later(0.3)
        done, total = _assess_prog["done"], _assess_prog["total"]
        label = f"Computing metrics… {done}/{total}"
        stage.set(label)
        ui.notification_show(label + " — please wait", id="stage",
                             type="message", duration=None)

    @reactive.effect
    def _assess_done():
        status = assess_task.status()
        if status in ("initial", "running"):
            return
        ui.notification_remove("stage"); stage.set("")
        if status == "error":
            ui.notification_show("Metric computation failed — try again or deselect a "
                                 "function in Configure.", type="error", duration=8)
            return
        try:
            res = assess_task.result()
        except Exception:
            ui.notification_show("Metric computation failed.", type="error", duration=8)
            return
        if res.get("status") != "ok":
            ui.notification_show("Analysis error", type="error", duration=8)
            return
        with reactive.isolate():
            d = delin()
        if not d:
            return
        merged = {k: v for k, v in d.items() if k != "ctx_inputs"}
        merged["delineation"] = {**d["delineation"], "huc12": res.get("huc12")}
        merged["report"] = res["report"]
        base_result.set(merged)
        _overrides.set({}); _notes.set({})   # fresh report starts with no overrides/notes
        _geom_owned.set(set()); _geom_text.set({})
        current_step.set(STEP_REPORT)
        if not modal_shown():
            modal_shown.set(True)
            _xs_unit_prev.set("ft")
            ui.modal_show(_report_modal(merged))

    # ---- step navigation ----
    @reactive.effect
    @reactive.event(input.to_configure)
    def _go_configure():
        current_step.set(STEP_CONFIGURE)

    @reactive.effect
    @reactive.event(input.back_to_basin)
    def _go_basin():
        current_step.set(STEP_BASIN)

    @reactive.effect
    @reactive.event(input.back_to_configure)
    def _go_configure2():
        current_step.set(STEP_CONFIGURE)

    @reactive.effect
    def _stepper_nav():
        # Stepper links live in the (re-rendering) left pane, so compare click
        # counters and only navigate on a genuine increase (ignore reset-to-0).
        cur = {}
        for key, _ in STEP_LABELS:
            try:
                cur[key] = input[f"go_{key}"]() or 0
            except Exception:
                cur[key] = 0
        with reactive.isolate():
            prev = step_clicks()
            target = next((k for k, _ in STEP_LABELS if cur[k] > prev.get(k, 0)), None)
            step_clicks.set(cur)
            if target is None:
                return
            has_delin = delin() is not None
            has_report = base_result() is not None
            if target == STEP_IDENTIFY:
                current_step.set(STEP_IDENTIFY)
            elif target in (STEP_BASIN, STEP_CONFIGURE) and has_delin:
                current_step.set(target)
            elif target == STEP_REPORT and has_report:
                current_step.set(STEP_REPORT)
            else:
                ui.notification_show("Finish the earlier steps first.", type="message", duration=2)

    @reactive.effect
    @reactive.event(input.nav_new, input.clear_basin)
    def _reset():
        for k in ("ws", "reach", "marker"):
            _remove_layer(k)
        snapped_point.set(None); delin.set(None); base_result.set(None)
        modal_shown.set(False); stage.set("")
        current_step.set(STEP_IDENTIFY)
        try:
            ui.modal_remove()
        except Exception:  # noqa: BLE001
            pass

    @reactive.effect
    @reactive.event(input.close_modal, input.close_modal_x)
    def _close_modal():
        ui.modal_remove()

    @reactive.effect
    @reactive.event(input.nav_about)
    def _about():
        ui.modal_show(ui.modal(
            ui.markdown(
                "**EASI** automates the EASI Screening-tier assessment (from STAF). "
                "Click a stream, delineate the watershed and a reach upstream, and EASI "
                "computes the 20 EASI metrics from national, public GIS/hydrology data, "
                "scores them with the STAF rollup, and produces a read-only report. "
                "It is a desktop screening estimate, not a field-validated assessment."),
            title="About EASI", easy_close=True))

    @reactive.effect
    @reactive.event(input.nav_help)
    def _help():
        ui.modal_show(ui.modal(
            ui.markdown(
                "**How to use**\n\n"
                "1. **Zoom in** (to ~street level) until blue NHD stream lines appear.\n"
                "2. **Click a stream** to drop a point (it snaps to the line; clicking off a "
                "stream is rejected).\n"
                "3. **Delineate basin** — the watershed + upstream reach are traced.\n"
                "4. **Configure** which functions/sources to compute, then **open the report**.\n\n"
                "Switch basemaps and toggle the NHD overlay with the layers control (top-right)."),
            title="Help", easy_close=True))

    # ---- selection + source choices (configure step) ----
    @reactive.calc
    def selected_metric_ids():
        out = []
        for i, mid in enumerate(ALL_MIDS):
            try:
                v = input[f"inc_{i}"]()
            except Exception:
                v = True
            if v:
                out.append(mid)
        return out

    @reactive.calc
    def source_choices():
        out = {}
        for i, mid in SRC_INDEX.items():
            try:
                v = input[f"src_{i}"]()
            except Exception:
                v = None
            if v:
                out[mid] = v
        return out

    # ---- in-table overrides + notes (posted by www/report-edit.js) ----
    @reactive.calc
    def current_overrides():
        return dict(_overrides())

    @reactive.effect
    @reactive.event(input.override_set)
    def _apply_override():
        ev = input.override_set() or {}
        mid, rating = ev.get("mid"), ev.get("rating")
        if not mid:
            return
        gen = None                          # the computed (generated) rating for this metric
        for row in ((base_result() or {}).get("report") or {}).get("metricRows", []):
            if row.get("metricId") == mid:
                gen = row.get("generatedRating")
                break
        cur = dict(_overrides())
        if rating in ("Good", "Fair", "Poor") and rating != gen:
            cur[mid] = rating
        else:                               # picking the computed value (or clearing) reverts
            cur.pop(mid, None)
        _overrides.set(cur)
        if mid in _geom_owned():             # a manual pick takes ownership from the geometry
            _geom_owned.set(_geom_owned() - {mid})
            _geom_text.set({k: v for k, v in _geom_text().items() if k != mid})

    @reactive.effect
    @reactive.event(input.note_set)
    def _apply_note():
        ev = input.note_set() or {}
        mid, text = ev.get("mid"), (ev.get("text") or "").strip()
        if not mid:
            return
        cur = dict(_notes())
        if text:
            cur[mid] = text
        else:
            cur.pop(mid, None)
        _notes.set(cur)

    # ---- editable cross-section geometry (bankfull / floodplain heights) ----
    _xs_unit_prev = reactive.value("ft")  # tracks the unit for input conversion

    @reactive.calc
    def _xs_block():
        base = base_result()
        block = (((base or {}).get("report") or {}).get("crossSection") or {}).get("geom")
        return block if (block and block.get("thalweg") is not None) else None

    @reactive.calc
    def current_geometry():
        """Current bankfull/floodplain stages (metres) from the edit inputs, or None."""
        block = _xs_block()
        if not block:
            return None
        try:
            unit, bf_h, fp_h = input.xs_unit(), input.xs_bankfull(), input.xs_floodplain()
        except Exception:
            return None
        if bf_h is None or fp_h is None:
            return None
        f = FT_PER_M if unit == "m" else 1.0  # store inputs in unit; convert to metres
        per_m = FT_PER_M if unit == "ft" else 1.0
        thalweg = block["thalweg"]
        return {"block": block, "unit": unit,
                "bankfull_stage": thalweg + float(bf_h) / per_m,
                "floodplain_stage": thalweg + float(fp_h) / per_m}

    @reactive.calc
    def _geom_edited():
        """True only when the heights differ from the Bieger default. Compares in the
        *display* unit at display precision so the round-trip through the 2-dp inputs
        (feet by default) never reads as an edit on its own."""
        block = _xs_block()
        if not block:
            return False
        try:
            unit, bf_h, fp_h = input.xs_unit(), input.xs_bankfull(), input.xs_floodplain()
        except Exception:
            return False
        if bf_h is None or fp_h is None:
            return False
        per_m = FT_PER_M if unit == "ft" else 1.0
        thal = block["thalweg"]
        bf_def = round((block["bankfull_stage"] - thal) * per_m, 2)
        fp_def = round((block["floodplain_stage"] - thal) * per_m, 2)
        return abs(float(bf_h) - bf_def) > 0.005 or abs(float(fp_h) - fp_def) > 0.005

    @reactive.effect
    @reactive.event(input.xs_bankfull, input.xs_floodplain)
    def _xs_rerate():
        """Geometry edits drive the cross-section metrics (floodplain access/entrenchment
        + engagement frequency); manual picks (via the dropdown) win until the next
        geometry edit — last action wins. ``_geom_owned`` tracks which ratings are
        currently geometry-owned (released only by us on revert/reset); ``_geom_text``
        carries each edited row's metric-specific value text."""
        if not _xs_block():
            return
        g = current_geometry()
        cur = dict(_overrides())
        texts = dict(_geom_text())
        owned = set(_geom_owned())
        if g and _geom_edited():
            derived = assessment.rate_metrics_from_stages(
                g["block"], g["bankfull_stage"], g["floodplain_stage"])
            new_owned = set()
            for mid, info in derived.items():
                if info.get("rating"):
                    cur[mid] = info["rating"]
                    texts[mid] = info.get("valueText", "")
                    new_owned.add(mid)
            for mid in owned - new_owned:  # release any we previously owned but no longer derive
                cur.pop(mid, None)
                texts.pop(mid, None)
            _overrides.set(cur)
            _geom_text.set(texts)
            _geom_owned.set(new_owned)
            return
        if owned:  # back to default (or un-ratable): release the ones we own
            for mid in owned:
                cur.pop(mid, None)
                texts.pop(mid, None)
            _overrides.set(cur)
            _geom_text.set(texts)
            _geom_owned.set(set())

    @reactive.calc
    def scored():
        base = base_result()
        if not base:
            return None
        sc = assessment.rescore(base["report"], dict(current_overrides()))
        owned = _geom_owned()
        if owned:  # relabel so an edited cross-section doesn't read as a manual override
            texts = _geom_text()
            for row in sc["metricRows"]:
                mid = row["metricId"]
                if mid in owned:
                    row["status"] = "xs-derived"
                    row["source"] = "edited cross-section"
                    row["valueText"] = texts.get(mid) or f"from edited cross-section: {row['rating']}"
                    row["note"] = "recomputed from your bankfull/floodplain heights"
        return sc

    @reactive.calc
    def xs_render():
        """The cross-section to show/export: recomputed when edited (or on a unit
        switch), else the original render (which matches the metric table)."""
        base = base_result()
        if not base:
            return None
        base_xs = (base["report"].get("crossSection") or {})
        g = current_geometry()
        if not g or (not _geom_edited() and g["unit"] == "ft"):
            return base_xs
        try:
            if _geom_edited():
                return assessment.cross_section_from_stages(
                    g["block"], g["bankfull_stage"], g["floodplain_stage"], unit=g["unit"])
            return assessment.cross_section_from_stages(  # default stages, unit switch only
                g["block"], g["bankfull_stage"], g["floodplain_stage"], unit=g["unit"],
                er=base_xs.get("entrenchment_ratio"), bhr=base_xs.get("bank_height_ratio"),
                edited=False)
        except Exception:  # noqa: BLE001
            return base_xs

    @reactive.calc
    def export_result():
        base, sc = base_result(), scored()
        if not base or not sc:
            return None
        notes = _notes()
        rows = [{**r, "userNote": notes.get(r["metricId"], "")} for r in sc["metricRows"]]
        return {**base, "report": {**sc, "metricRows": rows,
                                   "crossSection": xs_render() or sc.get("crossSection")}}

    # ---- left pane (state machine) ----
    @render.ui
    def leftpane():
        step = current_step()
        if step == STEP_IDENTIFY:
            # initial disabled state from the current point, without making the pane
            # re-render on every snap (the toggle effect updates it live)
            with reactive.isolate():
                picked = snapped_point() is not None
            body = ui.TagList(
                ui.div("Zoom in until blue stream lines appear, then click a stream to "
                       "place a point. Or search an address.", class_="easi-instr"),
                ui.input_text("address", "Address, place, or stream",
                              placeholder="e.g. Atlanta, GA  ·  Utoy Creek"),
                ui.input_action_button("find_address", "Find on map",
                                       class_="btn-outline-secondary btn-sm"),
                ui.div("Type to search — suggestions from OpenStreetMap / Photon.",
                       class_="easi-ac-credit"),
                ui.hr(),
                ui.input_numeric("lat", "Latitude", value=40.0962, min=24.0, max=50.0, step=0.0001),
                ui.input_numeric("lon", "Longitude", value=-83.0203, min=-125.0, max=-66.0, step=0.0001),
                ui.input_numeric("reach_ft", "Assessment reach (ft)", value=int(DEFAULT_REACH_FT),
                                 min=100, max=5280, step=100),
                ui.output_ui("snap_status"),
                ui.div(ui.input_action_button("delineate", "Delineate Basin and Reach",
                                              class_="btn-primary", disabled=not picked),
                       class_="easi-pane-actions"),
                ui.output_ui("busy"),
            )
        elif step == STEP_BASIN:
            body = ui.TagList(ui.output_ui("basin_card"),
                              ui.div(ui.input_action_button("clear_basin", "Clear",
                                                            class_="btn-outline-secondary"),
                                     ui.input_action_button("to_configure", "Continue",
                                                            class_="btn-primary"),
                                     class_="easi-pane-actions"))
        elif step == STEP_CONFIGURE:
            body = ui.TagList(
                ui.div("Explore the metrics and data sources used for each function.",
                       class_="easi-instr"),
                _configure_rows(),
                ui.div(ui.input_action_button("back_to_basin", "Back", class_="btn-outline-secondary"),
                       ui.input_action_button("to_report", "Compute & report", class_="btn-primary"),
                       class_="easi-pane-actions"),
                ui.output_ui("busy"),
            )
        else:  # report
            body = ui.TagList(
                ui.div("Analysis complete. Open the report, adjust overrides, or export.",
                       class_="easi-instr"),
                ui.div(ui.input_action_button("show_report", "Open report", class_="btn-primary"),
                       class_="easi-pane-actions"),
                ui.div(ui.input_action_button("back_to_configure", "Back to configure",
                                              class_="btn-outline-secondary"),
                       class_="easi-pane-actions"),
            )
        active = current_step()
        head_label = dict(STEP_LABELS).get(active, "EASI")
        return ui.TagList(
            ui.div(f"EASI — {head_label}", class_="easi-pane-head"),
            ui.div(_stepper(active), body, class_="easi-pane-body"),
        )

    def _configure_rows():
        groups: dict[str, list] = {d: [] for d in _DISC_ORDER}
        for i, mid in enumerate(ALL_MIDS):
            meta = _METRICS[mid]
            groups.setdefault(meta["discipline"], []).append((i, mid, meta))
        out = []
        for disc in _DISC_ORDER:
            rows = groups.get(disc) or []
            if not rows:
                continue
            # within a discipline, group metrics under their STAF function (encounter order)
            fn_order: list[str] = []
            by_fn: dict[str, list] = {}
            for i, mid, meta in rows:
                fn = meta.get("functionName") or "—"
                if fn not in by_fn:
                    by_fn[fn] = []
                    fn_order.append(fn)
                by_fn[fn].append((i, mid, meta))
            blocks = []
            for fn in fn_order:
                members = by_fn[fn]
                stmt = (members[0][2].get("functionStatement") or "").strip()
                header = ui.div(ui.span(fn, class_="easi-fn-name"), _info(stmt),
                                class_="easi-fn-group")
                blocks.append(ui.div(
                    header,
                    *[_cfg_row(i, mid, meta) for i, mid, meta in members],
                    class_="easi-fn-block"))
            # collapsible discipline — no `open` attribute => collapsed by default
            out.append(ui.tags.details(
                ui.tags.summary(disc, class_="easi-cfg-group"),
                ui.div(*blocks, class_="easi-disc-body"),
                class_="easi-disc"))
        return ui.div(*out)

    def _cfg_row(i, mid, meta):
        # All per-metric detail lives in the ⓘ hover tooltip; the inline row is just
        # the checkbox + ⓘ (+ a source dropdown where alternatives exist).
        crit = meta.get("criteria") or {}
        tip = "\n".join(filter(None, [
            f"Source: {_ds_label(mid)}",
            (f"Alternative (planned): {config.PLANNED_ALT_SOURCE[mid]}"
             if mid in config.PLANNED_ALT_SOURCE else ""),
            (f"Good: {crit['Good']}" if crit.get("Good") else ""),
            (f"Fair: {crit['Fair']}" if crit.get("Fair") else ""),
            (f"Poor: {crit['Poor']}" if crit.get("Poor") else ""),
        ]))
        # ⓘ rides inside the checkbox label (right after the name) so it hugs the
        # text instead of being pushed to the row's right edge.
        label = ui.span(meta["name"], _info(tip)) if tip else meta["name"]
        children = [ui.input_checkbox(f"inc_{i}", label, value=True)]
        if mid in config.SOURCE_OPTIONS:            # interactive source choice stays visible
            opts = {v: lbl for v, lbl in config.SOURCE_OPTIONS[mid]}
            children.append(ui.input_select(f"src_{i}", None, choices=opts,
                                            selected=next(iter(opts))))
        return ui.div(*children, class_="easi-cfg-row")

    @render.ui
    def snap_status():
        pt = snapped_point()
        if not pt:
            return ui.p("No point yet — zoom in (≥14) and click a blue stream line.",
                        class_="easi-snap-note")
        return ui.p(f"✓ Snapped to stream ({pt[2]:.0f} ft away). Click “Delineate basin”.",
                    class_="easi-snap-note ok")

    @render.ui
    def basin_card():
        d = (delin() or {}).get("delineation") or {}
        if not d:
            return None
        def row(label, val):
            return ui.div(ui.span(label), ui.tags.b(str(val)), class_="b-row")
        return ui.div(
            ui.h5(d.get("gnis_name") or "(unnamed reach)"),
            row("Drainage area", f'{d.get("drainage_area_sqkm")} km²'),
            row("Watershed area", f'{d.get("watershed_area_sqkm")} km²'),
            row("Reach length", f'{d.get("reach_length_ft")} ft'),
            row("COMID", d.get("comid")),
            class_="easi-basin-card",
        )

    @render.ui
    def busy():
        s = stage()
        running = (delineate_task.status() == "running") or (assess_task.status() == "running")
        if s and running:
            return ui.div(ui.div(class_="easi-spinner"), ui.span(s), class_="easi-busy")
        return None

    @render.ui
    def readout():
        if not _HAS_MAP:
            return None
        z, c = _view()
        if not c:
            return ui.div("Zoom in and click a stream", class_="easi-readout")
        return ui.div(f"Zoom {int(z)}  ·  Lat {float(c[0]):.4f}, Lon {float(c[1]):.4f}",
                      class_="easi-readout")

    @render.ui
    def flow_loading():
        # Cue the user that the clickable blue stream vectors are being fetched —
        # only while in the identify step, zoomed in enough for them to appear, and
        # a fetch is actually in flight.
        if not _HAS_MAP or current_step() != STEP_IDENTIFY:
            return None
        z, _c = _view()
        if z is None or z < FLOW_ZOOM or flow_task.status() != "running":
            return None
        return ui.div(ui.div(class_="easi-spinner"), ui.span("Loading streams…"),
                      class_="easi-flow-loading")

    @render.ui
    def cursor_style():
        # When a point can be selected (identify step, zoomed in to the vectors),
        # show a crosshair; leaflet swaps to a grabbing hand while dragging.
        z, _c = _view()
        picking = (current_step() == STEP_IDENTIFY and z is not None and z >= FLOW_ZOOM)
        if not picking:
            return None
        # leaflet sets `cursor:grab` inline on the container, so override with !important
        return ui.tags.style(
            ".easi-map-wrap .leaflet-grab{cursor:crosshair !important;}"
            ".easi-map-wrap .leaflet-container.leaflet-dragging,"
            ".easi-map-wrap .leaflet-container.leaflet-dragging .leaflet-grab"
            "{cursor:grabbing !important;}")

    # ---- modal output slots (re-render in place on override change) ----
    @render.ui
    def m_scores():
        sc = scored()
        if not sc:
            return None
        eci, sub = sc["ecosystemConditionIndex"], sc["subIndices"]
        return ui.TagList(
            ui.p(f'{sc["computedCount"]}/{sc["totalCount"]} metrics computed · '
                 f'{_confidence_summary(sc)}',
                 style="font-size:12.5px;color:#556;margin:.2rem 0 .7rem;"),
            _outcome_cards(sc),
            ui.div(
                _bar("Ecosystem Condition Index", eci, scoring.index_band_color(eci or 0)),
                _bar("Physical", sub["physical"], scoring.index_band_color(sub["physical"])),
                _bar("Chemical", sub["chemical"], scoring.index_band_color(sub["chemical"])),
                _bar("Biological", sub["biological"], scoring.index_band_color(sub["biological"])),
                class_="easi-cond-bars",
            ),
        )

    @render.ui
    def m_outcomes():
        sc = scored()
        return _outcome_table(sc["outcomes"]) if sc else None

    @render.ui
    def m_metrics():
        sc = scored()
        if not sc:
            return None
        with reactive.isolate():          # read notes without re-rendering on every keystroke
            notes = dict(_notes())
        return _metric_table(sc["metricRows"], notes)

    @render.ui
    def xsection():
        xs = xs_render() or {}
        if not xs.get("png_b64"):
            return None
        return ui.div(
            ui.tags.img(src=f"data:image/png;base64,{xs['png_b64']}"),
            ui.p(xs.get("caption") or "", class_="easi-xsection-cap"),
            class_="easi-xsection",
        )

    @reactive.effect
    @reactive.event(input.xs_unit)
    def _xs_convert_units():
        new, old = input.xs_unit(), _xs_unit_prev()
        if new == old:
            return
        factor = (1.0 / FT_PER_M) if (old == "ft" and new == "m") else (
            FT_PER_M if (old == "m" and new == "ft") else 1.0)
        for fid in ("xs_bankfull", "xs_floodplain"):
            try:
                v = input[fid]()
            except Exception:
                v = None
            if v is not None:
                ui.update_numeric(fid, value=round(float(v) * factor, 2))
        _xs_unit_prev.set(new)

    @reactive.effect
    @reactive.event(input.xs_reset)
    def _xs_reset_defaults():
        block = _xs_block()
        if not block:
            return
        thalweg = block["thalweg"]
        per_m = FT_PER_M if input.xs_unit() == "ft" else 1.0
        ui.update_numeric("xs_bankfull",
                          value=round((block["bankfull_stage"] - thalweg) * per_m, 2))
        ui.update_numeric("xs_floodplain",
                          value=round((block["floodplain_stage"] - thalweg) * per_m, 2))

    @reactive.effect
    @reactive.event(input.show_report)
    def _reopen():
        base = base_result()
        if base is None:
            ui.notification_show("Run an analysis first.", type="message", duration=3)
            return
        _xs_unit_prev.set("ft")  # modal recreated with feet-default inputs
        _geom_owned.set(set()); _geom_text.set({})
        ui.modal_show(_report_modal(base))

    # ---- downloads (reflect current overrides) ----
    @render.download(filename="easi_report.pdf")
    def dl_pdf():
        res = export_result()
        if res:
            yield report.build_pdf(res)

    @render.download(filename="easi_report.csv")
    def dl_csv():
        res = export_result()
        if res:
            yield report.build_csv(res)

    @render.download(filename="easi_report.geojson")
    def dl_geojson():
        res = export_result()
        if res:
            yield report.build_geojson(res).encode("utf-8")


# Shiny for Python serves a static dir only when configured (no implicit www/).
app = App(app_ui, server, static_assets=Path(__file__).parent / "www")
