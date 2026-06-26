"""Report exports (shiny-free): CSV, GeoJSON, and PDF from an analysis result.

A ``result`` is the dict returned by ``pipeline.run_analysis`` (with ``report``
possibly replaced by a re-scored report from ``assessment.rescore``):
``result["delineation"]``, ``result["report"]``, ``result["watershed_geojson"]``,
``result["reach_geojson"]``.
"""
from __future__ import annotations

import csv
import io
import json

from .scoring import index_band_color, index_band_label

RATING_COLOR = {"Good": "#c8d9f2", "Fair": "#f5e7a6", "Poor": "#f5b5b5"}
_DISCIPLINE_ORDER = ["Hydrology", "Hydraulics", "Geomorphology",
                     "Physicochemistry", "Biology"]


def _ordered_rows(rep: dict) -> list[dict]:
    rows = rep.get("metricRows", [])
    order = {d: i for i, d in enumerate(_DISCIPLINE_ORDER)}
    return sorted(rows, key=lambda r: (order.get(r["discipline"], 99), r["functionName"]))


def _summary_pairs(result: dict) -> list[tuple[str, str]]:
    d, rep = result["delineation"], result["report"]
    sub = rep["subIndices"]
    return [
        ("Stream", d.get("gnis_name", "")),
        ("COMID", d.get("comid", "")),
        ("HUC12", d.get("huc12") or ""),
        ("Drainage area (km2)", d.get("drainage_area_sqkm", "")),
        ("Watershed area (km2)", d.get("watershed_area_sqkm", "")),
        ("Reach length (ft)", d.get("reach_length_ft", "")),
        ("Snapped lat", d.get("snapped_lat", "")),
        ("Snapped lon", d.get("snapped_lon", "")),
        ("Ecosystem Condition Index", rep.get("ecosystemConditionIndex", "")),
        ("Physical sub-index", sub.get("physical", "")),
        ("Chemical sub-index", sub.get("chemical", "")),
        ("Biological sub-index", sub.get("biological", "")),
        ("Metrics computed", f"{rep.get('computedCount')}/{rep.get('totalCount')}"),
        ("Overrides applied", ", ".join(rep.get("overridesApplied", [])) or "none"),
    ] + [(label, val) for label, val in (rep.get("basin") or {}).get("rows", [])]


# --------------------------------------------------------------------------- #
# CSV
# --------------------------------------------------------------------------- #
def build_csv(result: dict) -> bytes:
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["EASI Screening Report"])
    for k, v in _summary_pairs(result):
        w.writerow([k, v])
    w.writerow([])
    rows = _ordered_rows(result["report"])
    has_notes = any(r.get("userNote") for r in rows)   # only add the column if used
    header = ["Discipline", "Function", "Metric", "Value", "Rating", "Index",
              "FunctionScore", "Confidence", "Source", "Status"]
    if has_notes:
        header.append("Notes")
    w.writerow(header)
    for r in rows:
        row = [r["discipline"], r["functionName"], r["name"], r["valueText"],
               r["rating"] or "", r["index"] if r["index"] is not None else "",
               r["functionScore"] if r["functionScore"] is not None else "",
               r["confidence"] or "", r["source"] or "", r["status"]]
        if has_notes:
            row.append(r.get("userNote", ""))
        w.writerow(row)
    return buf.getvalue().encode("utf-8")


# --------------------------------------------------------------------------- #
# GeoJSON
# --------------------------------------------------------------------------- #
def build_geojson(result: dict) -> str:
    d, rep = result["delineation"], result["report"]
    features: list[dict] = []
    summary = {
        "gnis_name": d.get("gnis_name"), "comid": d.get("comid"),
        "huc12": d.get("huc12"), "drainage_area_sqkm": d.get("drainage_area_sqkm"),
        "ecosystem_condition_index": rep.get("ecosystemConditionIndex"),
        "sub_indices": rep.get("subIndices"),
        "metrics_computed": f"{rep.get('computedCount')}/{rep.get('totalCount')}",
        "basin_characteristics": {lbl: val for lbl, val in (rep.get("basin") or {}).get("rows", [])},
    }

    def _add(fc, props):
        for f in (fc or {}).get("features", []):
            features.append({"type": "Feature", "geometry": f.get("geometry"),
                             "properties": props})

    _add(result.get("watershed_geojson"), {"type": "watershed",
         "area_sqkm": d.get("watershed_area_sqkm"), **summary})
    _add(result.get("reach_geojson"), {"type": "reach",
         "length_ft": d.get("reach_length_ft"), **summary})
    if d.get("snapped_lat") is not None:
        features.append({"type": "Feature", "properties": {"type": "point", **summary},
                         "geometry": {"type": "Point",
                                      "coordinates": [d["snapped_lon"], d["snapped_lat"]]}})
    # per-metric ratings travel with the point/reach as a compact attribute table
    metrics = {}
    for r in rep.get("metricRows", []):
        m = {"rating": r["rating"], "value": r["valueText"],
             "confidence": r["confidence"], "source": r["source"]}
        if r.get("userNote"):
            m["note"] = r["userNote"]
        metrics[r["metricId"]] = m
    for f in features:
        if f["properties"].get("type") in ("reach", "point"):
            f["properties"]["metrics"] = metrics
    return json.dumps({"type": "FeatureCollection", "features": features}, indent=2)


# --------------------------------------------------------------------------- #
# PDF
# --------------------------------------------------------------------------- #
def _condition_png(rep: dict) -> bytes:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    sub = rep["subIndices"]
    labels = ["Ecosystem", "Physical", "Chemical", "Biological"]
    vals = [rep.get("ecosystemConditionIndex") or 0, sub["physical"],
            sub["chemical"], sub["biological"]]
    ylabels = [f"{lab} — {index_band_label(v)}" for lab, v in zip(labels, vals)]
    colors = [index_band_color(v) for v in vals]
    fig, ax = plt.subplots(figsize=(6.5, 1.9))
    ax.barh(ylabels[::-1], vals[::-1], color=colors[::-1], edgecolor="#888")
    ax.set_xlim(0, 1)
    for i, v in enumerate(vals[::-1]):
        ax.text(min(v + 0.02, 0.95), i, f"{v:.2f}", va="center", fontsize=9)
    ax.set_xlabel("Condition index (0–1): Poor · Fair · Good bands")
    fig.tight_layout()
    out = io.BytesIO()
    fig.savefig(out, format="png", dpi=130)
    plt.close(fig)
    return out.getvalue()


def build_pdf(result: dict) -> bytes:
    from reportlab.lib import colors as rc
    from reportlab.lib.pagesizes import letter
    from reportlab.lib.styles import getSampleStyleSheet
    from reportlab.lib.units import inch
    from reportlab.platypus import (Image, Paragraph, SimpleDocTemplate, Spacer,
                                    Table, TableStyle)

    d, rep = result["delineation"], result["report"]
    styles = getSampleStyleSheet()
    story = []
    story.append(Paragraph(f"EASI Screening Report — {d.get('gnis_name','')}",
                           styles["Title"]))
    meta = (f"COMID {d.get('comid')} · HUC12 {d.get('huc12') or '—'} · drainage "
            f"{d.get('drainage_area_sqkm')} km² · watershed {d.get('watershed_area_sqkm')} km² · "
            f"reach {d.get('reach_length_ft')} ft · "
            f"{rep.get('computedCount')}/{rep.get('totalCount')} metrics computed")
    story.append(Paragraph(meta, styles["Normal"]))
    story.append(Spacer(1, 8))

    basin_rows = (rep.get("basin") or {}).get("rows") or []
    if basin_rows:
        story.append(Paragraph("Basin characteristics", styles["Heading4"]))
        btbl = Table([[Paragraph(f"<b>{lbl}</b>", styles["BodyText"]),
                       Paragraph(str(val), styles["BodyText"])] for lbl, val in basin_rows],
                     colWidths=[2.4 * inch, 4.0 * inch])
        btbl.setStyle(TableStyle([
            ("FONTSIZE", (0, 0), (-1, -1), 8.5),
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ("GRID", (0, 0), (-1, -1), 0.25, rc.HexColor("#e5e8ee")),
            ("ROWBACKGROUNDS", (0, 0), (-1, -1), [rc.white, rc.HexColor("#fafbfd")])]))
        story.append(btbl)
        story.append(Spacer(1, 8))

    story.append(Image(io.BytesIO(_condition_png(rep)), width=6.0 * inch, height=1.75 * inch))
    story.append(Spacer(1, 8))

    xs = rep.get("crossSection") or {}
    if xs.get("png_b64"):
        import base64
        story.append(Paragraph("Representative cross-section", styles["Heading4"]))
        story.append(Image(io.BytesIO(base64.b64decode(xs["png_b64"])),
                           width=6.0 * inch, height=2.4 * inch))
        if xs.get("caption"):
            story.append(Paragraph(xs["caption"], styles["Italic"]))
        geom = xs.get("geom") or {}
        thal = geom.get("thalweg")
        ft = 3.28084  # metres -> feet for the report

        def _w(m):
            return f"{m * ft:.1f} ft" if m is not None else "n/a"

        def _h(stage):
            return (f"{(stage - thal) * ft:.2f} ft"
                    if stage is not None and thal is not None else "n/a")

        er, bhr = xs.get("entrenchment_ratio"), xs.get("bank_height_ratio")
        summary = (f"Bankfull width: {_w(geom.get('bankfull_width_m'))} &middot; "
                   f"Floodprone width: {_w(geom.get('flood_prone_width_m'))} &middot; "
                   f"Entrenchment ratio: {er if er is not None else 'n/a'} &middot; "
                   f"Bank-height ratio: {bhr if bhr is not None else 'n/a'} &middot; "
                   f"Bankfull height: {_h(geom.get('bankfull_stage'))} &middot; "
                   f"Low bank height: {_h(geom.get('floodplain_stage'))}")
        story.append(Paragraph(summary, styles["Normal"]))
        story.append(Spacer(1, 8))

    metric_rows = _ordered_rows(rep)
    has_notes = any(r.get("userNote") for r in metric_rows)   # add a Notes column only if used
    head = ["Function", "Metric", "Value", "Rating", "Idx", "Fn", "Conf"]
    if has_notes:
        head.append("Notes")
    data = [head]
    rating_bg = []
    for i, r in enumerate(metric_rows, start=1):
        row = [
            Paragraph(r["functionName"], styles["BodyText"]),
            Paragraph(r["name"], styles["BodyText"]),
            Paragraph(r["valueText"], styles["BodyText"]),
            r["rating"] or "—",
            "" if r["index"] is None else f'{r["index"]:.2f}',
            "" if r["functionScore"] is None else str(r["functionScore"]),
            r["confidence"] or "",
        ]
        if has_notes:
            row.append(Paragraph(r.get("userNote", ""), styles["BodyText"]))
        data.append(row)
        if r["rating"] in RATING_COLOR:
            rating_bg.append((i, rc.HexColor(RATING_COLOR[r["rating"]])))
    col_widths = ([1.3*inch, 1.5*inch, 1.9*inch, 0.5*inch, 0.35*inch, 0.3*inch, 0.4*inch]
                  if not has_notes else
                  [1.1*inch, 1.25*inch, 1.35*inch, 0.5*inch, 0.35*inch, 0.3*inch, 0.4*inch, 1.25*inch])
    tbl = Table(data, colWidths=col_widths, repeatRows=1)
    style = [("BACKGROUND", (0, 0), (-1, 0), rc.HexColor("#2f4b7c")),
             ("TEXTCOLOR", (0, 0), (-1, 0), rc.white),
             ("FONTSIZE", (0, 0), (-1, -1), 7.5),
             ("VALIGN", (0, 0), (-1, -1), "TOP"),
             ("GRID", (0, 0), (-1, -1), 0.25, rc.HexColor("#d7dce5"))]
    for row_i, color in rating_bg:
        style.append(("BACKGROUND", (3, row_i), (3, row_i), color))
    tbl.setStyle(TableStyle(style))
    story.append(tbl)
    story.append(Spacer(1, 8))
    story.append(Paragraph(
        "Generated from national datasets — a desktop screening estimate with "
        "per-metric confidence, not a field-validated assessment.",
        styles["Italic"]))

    out = io.BytesIO()
    SimpleDocTemplate(out, pagesize=letter, topMargin=0.6 * inch,
                      bottomMargin=0.6 * inch).build(story)
    return out.getvalue()
